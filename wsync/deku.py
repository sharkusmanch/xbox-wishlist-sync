"""Deku Deals client: read the wishlist, search, read an item, add / update / remove an entry.

Protocol notes (verified live, 2026-11-01) are in docs/deku-protocol.md. In short:
  - add    = POST /items/<uuid>/watch `to=true`, then the full details form for format + platforms;
  - update = POST the *whole* details form with current values re-sent, changing only
             `desired_format` and the Xbox `platform_*` boxes;
  - remove = POST `to=false`.
Every write is read back from the item page, so a silently rejected write is an error.

Credentials stay in memory: they are never logged, put in an exception or put in argv.
"""

from __future__ import annotations

import html
import http.cookiejar
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Protocol

from wsync.plan import XBOX_PLATFORMS, Entry

BASE = "https://www.dekudeals.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
TIMEOUT = 60
PAGE_PAUSE = 0.5
MAX_PAGES = 50
MAX_REDIRECTS = 5
RATE_LIMIT_WAITS = (20, 60)  # seconds to wait before retrying a GET answered HTTP 429
FORMATS = ("", "either", "physical", "digital")
# What the share page's "Desired format" detail says, as our format keys.
_SHARED_FORMAT = {"physical": "physical", "digital": "digital", "physical or digital": "either"}
# Platform names on the item page's "Platforms:" line -> the keys used by `platform_<key>`.
_PLATFORM_NAMES = {"xbox series x|s": "xbox_series", "xbox one": "xbox_one"}


class DekuError(Exception):
    """Deku Deals answered with something we can't treat as success."""


class AuthError(DekuError):
    """Not logged in, and logging in again didn't help."""


@dataclass(frozen=True)
class Response:
    status: int
    url: str
    text: str = field(repr=False)
    location: str | None = None


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        data: list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response: ...


@dataclass
class DekuItem:
    slug: str
    uuid: str
    title: str
    on_wishlist: bool
    # Xbox platform boxes ticked on the wishlist entry (empty when not on the wishlist).
    platforms: frozenset[str]
    # Xbox platforms the game exists on, from the page's "Platforms:" line.
    offered: frozenset[str]
    # The wishlist entry's current details-form values (empty when not on the wishlist).
    settings: dict[str, str] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- transport


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class UrllibTransport:
    """Cookie-keeping HTTPS transport that never follows redirects (the client reads them)."""

    def __init__(self, cookie_path: Path | None = None):
        self._path = Path(cookie_path) if cookie_path else None
        self._jar = http.cookiejar.MozillaCookieJar(str(self._path) if self._path else None)
        if self._path and self._path.exists():
            try:
                self._jar.load(ignore_discard=True, ignore_expires=True)
            except http.cookiejar.LoadError:
                pass  # an unreadable cookie file just means logging in again
            except OSError as e:
                raise DekuError(f"cannot read the session file ({type(e).__name__})") from None
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar), _NoRedirect
        )

    def request(self, method, url, data=None, headers=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        hdrs = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
        if body is not None:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with self._opener.open(req, timeout=TIMEOUT) as resp:
                result = Response(resp.status, resp.geturl(), resp.read().decode("utf-8", "replace"),
                                  resp.headers.get("Location"))
        except urllib.error.HTTPError as e:
            result = Response(e.code, url, e.read().decode("utf-8", "replace"), e.headers.get("Location"))
        except (urllib.error.URLError, OSError) as e:
            raise DekuError(f"network error: {type(e).__name__}") from None
        self._save()
        return result

    def _save(self) -> None:
        if not self._path:
            return
        try:
            if not self._path.parent.exists():
                self._path.parent.mkdir(parents=True, mode=0o700)
            self._jar.save(ignore_discard=True, ignore_expires=True)
            os.chmod(self._path, 0o600)
        except OSError as e:
            raise DekuError(f"cannot save the session file ({type(e).__name__})") from None


# --------------------------------------------------------------------------- parsing


def _logged_in(text: str) -> bool:
    return "/logout" in text


class _ItemParser(HTMLParser):
    """Collects the title, every `/items/<uuid>/watch` uuid and the wishlist details form."""

    _WATCH = re.compile(r"^(?:https://www\.dekudeals\.com)?/items/([0-9a-f-]{36})/watch$")
    _ITEM_ACTION = re.compile(r"^(?:https://www\.dekudeals\.com)?/items/([0-9a-f-]{36})/(?:watch|hide|rating)$")
    _OWNED = re.compile(r"^(?:https://www\.dekudeals\.com)?/owned_items$")

    def __init__(self):
        super().__init__()
        self.title: str | None = None
        self.uuids: set[str] = set()  # every item uuid the page's forms and buttons name
        self.details_uuid: str | None = None
        self.form_found = False
        self._owned_form = False
        self.fields: list[tuple[str, str, str, bool]] = []  # name, type, value, checked
        self.note: str | None = None
        self._in_form = False
        self._in_note = False
        self._note: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name") == "og:title" and self.title is None:
            content = a.get("content") or ""
            self.title = content.removesuffix(" | Deku Deals")
        if a.get("data-item-id"):
            self.uuids.add(a["data-item-id"])
        if tag == "form":
            action = a.get("action") or ""
            self._owned_form = bool(self._OWNED.match(action))
            if m := self._ITEM_ACTION.match(action):
                self.uuids.add(m.group(1))
            if m := self._WATCH.match(action):
                if "watch-details-form" in (a.get("class") or "").split():
                    self.form_found = True
                    self._in_form = True
                    self.details_uuid = m.group(1)
        elif self._owned_form and tag == "input" and a.get("name") == "item_id" and a.get("value"):
            self.uuids.add(a["value"])
        elif self._in_form and tag == "input" and a.get("name"):
            self.fields.append((a["name"], a.get("type") or "text", a.get("value") or "", "checked" in a))
        elif self._in_form and tag == "textarea" and a.get("name") == "note":
            self._in_note = True
            self._note = []

    def handle_data(self, data):
        if self._in_note:
            self._note.append(data)

    def handle_endtag(self, tag):
        if tag == "textarea" and self._in_note:
            self._in_note = False
            self.note = "".join(self._note)
        elif tag == "form":
            self._in_form = False
            self._owned_form = False


def _settings(parser: _ItemParser) -> dict[str, str]:
    """The form as a browser would submit it, one value per field, in form order."""
    out: dict[str, str] = {}
    radios: dict[str, str] = {}
    for name, typ, value, checked in parser.fields:
        if typ == "radio":
            radios.setdefault(name, "")
            if checked:
                radios[name] = value
        elif typ == "checkbox":
            out[name] = "true" if checked else "false"
        elif typ == "hidden":
            out.setdefault(name, value)
    merged: dict[str, str] = {}
    for name in dict.fromkeys(n for n, *_ in parser.fields):
        if name in radios:
            merged[name] = radios[name]
        elif name in out:
            merged[name] = out[name]
    merged["note"] = parser.note or ""
    return merged


def _offered(page: str) -> frozenset[str]:
    m = re.search(r"<strong>Platforms:</strong>\s*(?:<a[^>]*>)?(.*?)(?:</a>|</li>)", page, re.S)
    if not m:
        return frozenset()
    names = [n.strip().lower() for n in html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).split(",")]
    return frozenset(_PLATFORM_NAMES[n] for n in names if n in _PLATFORM_NAMES)


def parse_item(slug: str, page: str) -> DekuItem:
    parser = _ItemParser()
    parser.feed(page)
    if not parser.uuids or not parser.title:
        raise DekuError(f"item page for {slug!r} has no uuid or title")
    if len(parser.uuids) > 1:
        raise DekuError(f"item page for {slug!r} names more than one item uuid")
    uuid = parser.details_uuid or next(iter(parser.uuids))
    settings = _settings(parser) if parser.form_found else {}
    ticked = frozenset(
        k.removeprefix("platform_")
        for k, v in settings.items()
        if k.startswith("platform_") and v == "true"
    ) & XBOX_PLATFORMS
    return DekuItem(
        slug=slug,
        uuid=uuid,
        title=html.unescape(parser.title),
        on_wishlist=parser.form_found,
        platforms=ticked,
        offered=_offered(page),
        settings=settings,
    )


_COUNT = re.compile(
    r"class='pagination_controls[^']*'>\s*(?:[\d,]+\s*-\s*[\d,]+\s+of\s+)?([\d,]+)(?:\s+items?)?\s*<"
)
_CARD = "<div class='d-block col'>"
_LINK = re.compile(r"<a class='main-link[^']*' href='/items/([^'/?]+)'>\s*<h6[^>]*>(.*?)</h6>", re.S)
_SHARED = re.compile(r"<small class='text-muted'>Desired format</small>\s*<br>\s*(.*?)\s*</div>", re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_wishlist_page(page: str) -> tuple[int | None, list[Entry]]:
    m = _COUNT.search(page)
    total = int(m.group(1).replace(",", "")) if m else None
    entries = []
    for card in page.split(_CARD)[1:]:
        link = _LINK.search(card)
        if not link:
            continue
        slug, title = link.group(1), _text(link.group(2))
        shared = _SHARED.search(card)
        fmt = ""
        if shared:
            label = _text(shared.group(1)).lower()
            if label not in _SHARED_FORMAT:
                raise DekuError(f"unknown desired format {label!r} on {slug!r}")
            fmt = _SHARED_FORMAT[label]
        entries.append(Entry(slug, title, fmt, frozenset()))
    return total, entries


# --------------------------------------------------------------------------- client


class DekuClient:
    def __init__(
        self,
        email: str,
        password: str,
        wishlist_id: str,
        session_path: Path | None = None,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        pause: float = PAGE_PAUSE,
    ):
        self._email = email
        self._password = password
        self._wishlist_id = wishlist_id
        self._http = transport or UrllibTransport(session_path)
        self._sleep = sleep
        self._pause = pause
        # Item pages read by the last `wishlist(with_platforms=True)`, keyed by slug, so callers
        # can reuse them (offered platforms, uuid) without fetching each page again.
        self.last_items: dict[str, DekuItem] = {}

    def __repr__(self) -> str:
        return "DekuClient(<credentials hidden>)"

    # ---- plumbing

    def _fetch(self, url: str) -> Response:
        """One GET, retried after a pause while the site answers 429 (rate limited)."""
        for wait in (*RATE_LIMIT_WAITS, None):
            resp = self._http.request("GET", url, None, None)
            if resp.status != 429 or wait is None:
                return resp
            self._sleep(wait)
        raise AssertionError("unreachable")

    def _get(self, url: str) -> Response:
        resp = self._fetch(url)
        for _ in range(MAX_REDIRECTS):
            if resp.status not in (301, 302, 303, 307, 308) or not resp.location:
                return resp
            resp = self._fetch(urllib.parse.urljoin(url, resp.location))
        raise DekuError("too many redirects")

    def _get_page(self, path: str, *, need_login: bool) -> str:
        """A page's HTML. With `need_login`, a page without the session is retried once after login."""
        return self._get_response(path, need_login=need_login).text

    def _get_response(self, path: str, *, need_login: bool) -> Response:
        """The final response for a page (after redirects); see `_get_page`."""
        where = path.split("?")[0].replace(self._wishlist_id, "<id>")
        resp = self._get(BASE + path)
        self._require_ok(resp, f"GET {where}")
        if need_login and not _logged_in(resp.text):
            self.login()
            resp = self._get(BASE + path)
            self._require_ok(resp, f"GET {where}")
            if not _logged_in(resp.text):
                raise AuthError("Deku Deals session not accepted after a fresh login")
        return resp

    @staticmethod
    def _require_ok(resp: Response, what: str) -> None:
        if resp.status >= 400:
            raise DekuError(f"{what} answered HTTP {resp.status}")

    @staticmethod
    def _needs_login(resp: Response) -> bool:
        if resp.status in (401, 403):
            return True
        return 300 <= resp.status < 400 and "/login" in (resp.location or "")

    def _post(self, path: str, fields: list[tuple[str, str]]) -> None:
        for attempt in (1, 2):
            resp = self._http.request("POST", BASE + path, fields, {"render": "true", "Origin": BASE})
            if self._needs_login(resp):
                if attempt == 2:
                    raise AuthError("Deku Deals rejected the session again after logging in")
                self.login()
                continue
            self._require_ok(resp, "POST " + re.sub(r"/items/[0-9a-f-]{36}/", "/items/<uuid>/", path))
            return

    # ---- session

    def login(self) -> None:
        resp = self._http.request(
            "POST", BASE + "/login",
            [("return_to", "/"), ("email", self._email), ("password", self._password)],
            {"Referer": BASE + "/login", "Origin": BASE},
        )
        self._require_ok(resp, "login")
        home = self._get(BASE + "/")
        self._require_ok(home, "GET /")
        if not _logged_in(home.text):
            raise AuthError("Deku Deals login failed (check the credentials)")

    # ---- reads

    def wishlist(self, *, with_platforms: bool = False) -> dict[str, Entry]:
        """The public share page, keyed by slug.

        The share page doesn't show which platform boxes are ticked, so `platforms` is empty unless
        `with_platforms` reads every entry's item page (one GET each, paced) for the platforms
        and the entry's real format.
        """
        entries: list[Entry] = []
        total = None
        for number in range(1, MAX_PAGES + 1):
            page = self._get_page(f"/wishlist/{self._wishlist_id}?page={number}", need_login=False)
            count, got = parse_wishlist_page(page)
            if count is None:
                raise DekuError("wishlist page has no item count (not the page we expected)")
            total = count if total is None else total
            if not got and len(entries) < total:
                raise DekuError(f"wishlist page {number} has no entries ({len(entries)} of {total} read)")
            entries += got
            if len(entries) >= total:
                break
            self._sleep(self._pause)
        else:
            raise DekuError("wishlist has too many pages")
        if len(entries) != total:
            raise DekuError(f"read {len(entries)} wishlist entries, the page says {total}")
        out = {e.target: e for e in entries}
        if len(out) != len(entries):
            raise DekuError("duplicate slugs on the wishlist pages")
        if with_platforms:
            self.last_items = {}
            for slug, entry in list(out.items()):
                self._sleep(self._pause)
                item = self.item(slug)
                self.last_items[slug] = item
                if not item.on_wishlist:
                    raise DekuError(f"{slug!r} is on the share page but its item page says it isn't")
                out[slug] = Entry(slug, entry.title, item.settings.get("desired_format", ""), item.platforms)
        return out

    def search(self, title: str) -> list[tuple[str, str]]:
        self._sleep(self._pause)  # searches come in bursts during matching; stay polite
        page = self._get_page("/search?" + urllib.parse.urlencode({"q": title}), need_login=False)
        seen: dict[str, str] = {}
        for slug, heading in _LINK.findall(page):
            seen.setdefault(slug, _text(heading))
        return [(t, s) for s, t in seen.items()]

    def item(self, slug: str) -> DekuItem:
        """An item page. A renamed item's old slug redirects: the result carries the current slug
        (the one the share page lists), taken from the final URL."""
        resp = self._get_response(f"/items/{urllib.parse.quote(slug)}", need_login=True)
        m = re.match(r"/items/([^/?#]+)/?$", urllib.parse.urlsplit(resp.url).path)
        return parse_item(urllib.parse.unquote(m.group(1)) if m else slug, resp.text)

    # ---- writes

    def add(self, item: DekuItem, fmt: str, platforms: frozenset[str]) -> None:
        """Add the game, then set its format and platforms.

        A new entry starts with every platform box ticked; we track only Xbox, so the follow-up
        save also unticks the non-Xbox boxes (unlike `update`, which leaves them alone).
        """
        if item.on_wishlist:
            raise DekuError(f"{item.slug!r} is already on the wishlist")
        _check_format(fmt)
        self._post(f"/items/{item.uuid}/watch", [("to", "true")])
        fresh = self.item(item.slug)
        if not fresh.on_wishlist:
            raise DekuError(f"{item.slug!r} did not appear on the wishlist after adding")
        self._save_settings(fresh, fmt, platforms, untick_other=True)

    def update(self, item: DekuItem, fmt: str, platforms: frozenset[str]) -> None:
        """Change the format and Xbox platforms; every other setting (and non-Xbox box) is re-sent as is."""
        self._save_settings(item, fmt, platforms, untick_other=False)

    def _save_settings(self, item: DekuItem, fmt: str, platforms: frozenset[str], *, untick_other: bool) -> None:
        _check_format(fmt)
        if not item.on_wishlist or not item.settings:
            raise DekuError(f"{item.slug!r} is not on the wishlist")
        have = {k.removeprefix("platform_") for k in item.settings if k.startswith("platform_")}
        want = platforms & XBOX_PLATFORMS
        missing = want - have
        if missing:
            raise DekuError(f"{item.slug!r} has no {sorted(missing)} platform box")
        expected = dict(item.settings)
        expected["desired_format"] = fmt
        fields: list[tuple[str, str]] = []
        for name, value in item.settings.items():
            if name == "desired_format":
                value = fmt
            elif name.startswith("platform_"):
                key = name.removeprefix("platform_")
                if key in XBOX_PLATFORMS:
                    value = "true" if key in want else "false"
                elif untick_other:
                    value = "false"
                expected[name] = value
                fields.append((name, "false"))
                if value == "true":
                    fields.append((name, "true"))
                continue
            elif name in ("note_private", "hide_from_public_view"):
                fields.append((name, "false"))
                if value == "true":
                    fields.append((name, "true"))
                continue
            fields.append((name, value))
        self._post(f"/items/{item.uuid}/watch", fields)
        after = self.item(item.slug)
        if not after.on_wishlist:
            raise DekuError(f"{item.slug!r}: the entry is gone after saving its settings")
        differing = sorted(k for k in expected.keys() | after.settings.keys() if expected.get(k) != after.settings.get(k))
        if differing:
            raise DekuError(f"{item.slug!r}: read back differs from what was saved in fields {differing}")

    def remove(self, item: DekuItem) -> None:
        self._post(f"/items/{item.uuid}/watch", [("to", "false")])
        if self.item(item.slug).on_wishlist:
            raise DekuError(f"{item.slug!r} is still on the wishlist after removing")


def _check_format(fmt: str) -> None:
    if fmt not in FORMATS:
        raise ValueError(f"unknown Deku format {fmt!r}")

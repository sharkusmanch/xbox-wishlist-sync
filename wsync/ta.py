"""TrueAchievements client: read the wishlist, search, read game and DLC pages, add / remove entries.

Protocol notes (verified live, 2026-11-01) are in docs/ta-protocol.md. In short:
  - TA is behind Cloudflare. Every TA request (the login, every page, the AJAX writes) goes out
    directly through curl_cffi with Chrome's TLS fingerprint, the session cookies and a
    Cloudflare clearance;
  - the clearance (`cf_clearance` cookie plus the matching user agent) comes from FlareSolverr,
    which is only ever sent an anonymous, cookie-less GET of a public TA page: FlareSolverr logs
    each request's cookies and body, so it never sees the password or a session cookie. A
    request that meets a Cloudflare challenge refreshes the clearance once, then fails;
  - logging in = GET /login, then POST its `#frm` fields with the gamertag and password;
    success means a `GamerToken` cookie;
  - the wishlist is `/gamer/<tag>/wishlist` (25 rows a page since 2026-11-06, `?page=N`); every
    page is read and the rows must add up to the filter bar's Games + DLC counts. A DLC row names
    only the DLC id and its game's slug, so the game id comes from the game page (cached in
    `game_ids`);
  - writes are JSON AJAX calls with the session cookies.

Credentials stay in memory and in the session file (outside the repo, mode 600): they are never
logged, put in an exception, put in argv or shown in a repr. Error messages name pages by path
with the gamertag replaced by `<gamer>`.
"""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlsplit

from wsync.errors import TAError
from wsync.flaresolverr import FlareSolverr
from wsync.plan import Entry
from wsync.ta_coll import OwnedRow, list_filters, list_url, parse_list, unknown_filters

BASE = "https://www.trueachievements.com"
AJAX = BASE + "/ajaxfunctions.aspx/"
HTTP_TIMEOUT = 60
MAX_WISHLIST_PAGES = 40  # 25 rows each; more is a page that keeps linking onwards, not a wishlist
MAX_COLLECTION = 20_000  # titles; a bigger claimed total is a misread page, not a collection
CLEARANCE_URL = BASE + "/"  # the public page FlareSolverr fetches, anonymously, for a clearance
MAX_REDIRECTS = 5
_REDIRECTS = {301, 302, 303, 307, 308}
SEARCH_PAGES = 4  # games-only result pages read per search (25 rows each)
SERIES = "Xbox Series X|S"
ONE = "Xbox One"
_LOGGED_OUT = "the gamer must be specified"
_ALREADY_LISTED = "already on your wishlist"
_SKIP_INPUT_TYPES = {"checkbox", "radio", "password"}


class AuthError(TAError):
    """Not logged in, and logging in again didn't help."""


class IncompleteRead(TAError):
    """The wishlist page couldn't be read completely; never treat it as the whole list."""


class AlreadyListed(TAError):
    """An add found the entry already on the wishlist: our read of the list was wrong or stale."""


@dataclass(frozen=True)
class TAGame:
    id: int
    title: str
    platform: str  # the page's own platform: the first one its Platform field lists
    slug: str = ""
    platforms: tuple[str, ...] = ()  # everything the Platform field lists


@dataclass(frozen=True)
class WishRow:
    kind: str  # "game" or "dlc"
    id: int  # the game id, or the DLC id for a DLC row
    title: str
    game_slug: str


@dataclass(frozen=True)
class WishPage:
    """One wishlist page: its rows, its number, whether it links the next page, and the list's total.

    `total` is the filter bar's Games + DLC count for the whole list; None on the old, unpaged
    layout, which had no counts.
    """

    rows: list[WishRow]
    number: int
    has_next: bool
    total: int | None


@dataclass(frozen=True)
class HttpResponse:
    """One HTTP answer (no redirect followed). `headers` are (lower-case name, value) pairs."""

    status: int
    text: str = field(default="", repr=False)
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def header(self, name: str) -> str:
        return next((v for k, v in self.headers if k == name), "")

    def header_list(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name]


@dataclass(frozen=True)
class Page:
    status: int
    html: str = field(repr=False)
    url: str = field(repr=False)  # the final URL, after redirects


class HttpTransport(Protocol):
    def request(self, method: str, url: str, body: str | None, headers: dict[str, str]) -> HttpResponse: ...


class CurlTransport:
    """curl_cffi with Chrome's TLS fingerprint: Cloudflare accepts it with a valid cf_clearance.

    Redirects are not followed here (the client follows them, so it sees every Set-Cookie and
    never sends the session to another host), and cookies travel only in the caller's `Cookie`
    header.
    """

    def request(self, method, url, body, headers):
        from curl_cffi import requests as cr

        try:
            r = cr.request(method, url, data=body, headers=headers, impersonate="chrome",
                           timeout=HTTP_TIMEOUT, allow_redirects=False)
        except OSError as e:  # curl_cffi's errors are OSErrors
            raise TAError(f"network error: {type(e).__name__}") from None
        return HttpResponse(r.status_code, r.text, tuple((k.lower(), v) for k, v in r.headers.multi_items()))


def challenged(r: HttpResponse) -> bool:
    """A Cloudflare challenge (or block) rather than TA's own answer."""
    if r.status == 403 or r.header("cf-mitigated").lower() == "challenge":
        return True
    return r.status in (429, 503) and ("Just a moment" in r.text or "challenge-platform" in r.text)


def _set_cookie(header: str) -> tuple[str, str | None] | None:
    """(name, value) from a Set-Cookie header; value None when the cookie is being deleted."""
    first, _, attrs = header.partition(";")
    name, eq, value = first.partition("=")
    name, value = name.strip(), value.strip()
    if not eq or not name:
        return None
    for attr in attrs.split(";"):
        key, _, val = attr.partition("=")
        key, val = key.strip().lower(), val.strip()
        if key == "max-age":
            try:
                if int(val) <= 0:
                    return name, None
            except ValueError:
                pass
        elif key == "expires":
            try:
                when = parsedate_to_datetime(val)
            except (TypeError, ValueError, IndexError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= datetime.now(timezone.utc):
                return name, None
    return name, (value or None)


# --------------------------------------------------------------------------- parsing


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


_ACCOUNT_LINK = re.compile(r'<a\b[^>]*\bclass="login hd"')
_LOGIN_MENU = re.compile(r'<div\b[^>]*\bclass="login-full hd"')


def logged_in(page: str) -> bool:
    """Whether the page was served to a logged-in gamer.

    As served, a logged-in page's header has the account link `<a ... class="login hd">` and no
    `<div class="login-full hd">` log-in menu. In a browser, TA's JavaScript also adds `g-li` to the
    body's class list (FlareSolverr-rendered pages and the test fixtures show that form).
    """
    m = re.search(r"<body[^>]*\bclass=\"([^\"]*)\"", page)
    if m and "g-li" in m.group(1).split():
        return True
    return bool(_ACCOUNT_LINK.search(page)) and not _LOGIN_MENU.search(page)


def gated(page: str, need_login: bool) -> bool:
    return "Please log in" in page or (need_login and not logged_in(page))


def login_form(page: str) -> dict[str, str]:
    """The `#frm` fields a browser would post, minus checkboxes, radios and the password box."""
    m = re.search(r'<form[^>]*\bid="frm"[^>]*>(.*?)</form>', page, re.S)
    if not m:
        raise TAError("login page has no form (layout changed?)")
    data: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", m.group(1)):
        name = re.search(r'\bname="([^"]+)"', tag)
        kind = re.search(r'\btype="([^"]+)"', tag)
        if not name or (kind and kind.group(1).lower() in _SKIP_INPUT_TYPES):
            continue
        value = re.search(r'\bvalue="([^"]*)"', tag)
        data[html.unescape(name.group(1))] = html.unescape(value.group(1)) if value else ""
    if "__VIEWSTATE" not in data:
        raise TAError("login form has no __VIEWSTATE (layout changed?)")
    return data


_ROW_LINK = re.compile(
    r'<a href="(?:https://www\.trueachievements\.com)?/game/([^/"?#]+)(/dlc/[^"?#]+)?(?:/achievements)?"[^>]*>(.*?)</a>',
    re.S,
)
_ROW_REMOVE = re.compile(r"Postback\('btnRemove(DLC)?FromWishList',\s*(\d+)\)")


_PAGINATION = re.compile(r'<ul class="pagination">(.*?)</ul>', re.S)
_CURRENT_PAGE = re.compile(r'<li class="current"><a [^>]*>\s*(\d+)\s*</a>')
_WL_WRAP = re.compile(r'<div class="[^"]*\bwl-wrap\b[^"]*"([^>]*)>')
_WL_FILTERS = ("data-show", "data-sale", "data-gp")
_WL_SEARCH = re.compile(r'<input type="search" name="q" value="([^"]*)"')


def _wishlist_count(body: str, label: str) -> int:
    m = re.findall(r'<a class="gr-pill"[^>]*>\s*' + label + r'\s*<span class="n">([\d,]+)</span>', body)
    if len(m) != 1:
        raise IncompleteRead(f"wishlist page has no single {label} count (layout changed?)")
    return int(m[0].replace(",", ""))


def _wishlist_paging(body: str) -> tuple[int, bool]:
    """(this page's number, whether it links the next page); (1, False) when unpaged."""
    blocks = _PAGINATION.findall(body)
    if not blocks:
        if re.search(r"[?&](?:amp;)?page=\d", body):
            raise IncompleteRead("wishlist page links other pages but has no pagination block")
        return 1, False
    current = {int(n) for b in blocks for n in _CURRENT_PAGE.findall(b)}
    if len(current) != 1:
        raise IncompleteRead("wishlist page's pagination names no single current page")
    number = current.pop()
    nxt = re.compile(r'href="[^"]*[?&](?:amp;)?page=%d"' % (number + 1))
    return number, any(nxt.search(b) for b in blocks)


def parse_wishlist_page(page: str) -> WishPage:
    """One page of the logged-in gamer's own wishlist: its rows in page order, and its paging.

    Raises IncompleteRead unless every row on the page parsed, the page is whole and unfiltered,
    and (when it is paged) the list's total is readable.
    """
    head = re.search(r'<h1 id="h1PageTitle"[^>]*>(.*?)</h1>', page, re.S)
    if not head:
        raise IncompleteRead("wishlist page has no heading (not the page we expected)")
    if _text(head.group(1)) != "My Wish List":
        raise TAError("wishlist page is not the logged-in gamer's own list")
    end = page.find("</main>", head.end())
    if end < 0:
        raise IncompleteRead("wishlist page is truncated")
    body = page[head.end():end]
    total = None
    wrap = _WL_WRAP.search(body)
    if wrap:  # the November 2026 layout: a filter bar with Games / DLC counts
        attrs = dict(re.findall(r'\s([\w-]+)="([^"]*)"', wrap.group(1)))
        search = _WL_SEARCH.search(body)
        if any(attrs.get(a) for a in _WL_FILTERS) or (search and search.group(1).strip()):
            raise IncompleteRead("wishlist page is filtered (search, show, sale or Game Pass)")
        total = _wishlist_count(body, "Games") + _wishlist_count(body, "DLC")
    number, has_next = _wishlist_paging(body)
    if total is None and (number != 1 or has_next):
        raise IncompleteRead("wishlist page is paged but shows no total to check the pages against")
    panels = body.split('<div class="ach-panel')[1:]
    if panels and 'id="divButtons"' not in body:
        raise IncompleteRead("wishlist page ends before its buttons (truncated)")
    rows: list[WishRow] = []
    for panel in panels:
        link, remove = _ROW_LINK.search(panel), _ROW_REMOVE.search(panel)
        if not link or not remove:
            continue
        is_dlc = bool(remove.group(1))
        if is_dlc != bool(link.group(2)):
            continue
        rows.append(WishRow("dlc" if is_dlc else "game", int(remove.group(2)), _text(link.group(3)), link.group(1)))
    if len(rows) != len(panels):
        raise IncompleteRead(f"read {len(rows)} of the {len(panels)} wishlist rows")
    # Cross-check against every remove button on the whole page, so a changed row markup can
    # never read as a shorter (or empty) list.
    buttons = len(_ROW_REMOVE.findall(page))
    if buttons != len(rows):
        raise IncompleteRead(f"read {len(rows)} wishlist rows but the page has {buttons} remove buttons")
    return WishPage(rows, number, has_next, total)


def parse_wishlist(page: str) -> list[WishRow]:
    """The rows of one wishlist page (see parse_wishlist_page)."""
    return parse_wishlist_page(page).rows


def page_title(page: str) -> str:
    """A game page's title without TA's "News and Videos" / "Achievements" / site suffix."""
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    if not m:
        return ""
    t = re.sub(r"\s*\|\s*TrueAchievements\s*$", "", _text(m.group(1)))
    return re.sub(r"\s+(?:News and Videos|Achievements)$", "", t)


_GAME_PATH = re.compile(r"^/game/([^/?#]+)")
_SEARCH_ROW = re.compile(
    r'<td class="gamerwide"><a href="/game/([^/"?#]+)[^"]*">(.*?)</a></td>\s*<td>([^<]*)</td>', re.S
)


_V2_ROW = re.compile(
    r'^ href="(?:https://www\.trueachievements\.com)?/game/([^/"?#]+)[^"]*">.*?'
    r'<span class="sst-game-name">(.*?)</span><span class="sst-(?:game-meta|game-platforms|matched-name)"',
    re.S,
)


_V2_COUNT = re.compile(r'<span class="count"[^>]*>\s*([\d,]+)\s+(?:results?|total)\b')


def _parse_search_v2(page: str) -> list[tuple[str, str]]:
    """The November 2026 layout: `<a class="sst-game-row">` rows under a results heading.

    The game count must be readable, and a non-zero count must come with game rows, so a
    changed layout can never read as "no results".
    """
    start = page.find('id="sr-games"')
    if start >= 0:  # the all-categories overview: only its Games section ("N total")
        nxt = re.compile(r'<div class="section(?: [^"]*)?" id=').search(page, start)
        page = page[start:nxt.start() if nxt else len(page)]
    else:  # the games-only view: the heading's "N results"
        head = re.search(r'<h2 class="results-heading">.*?</h2>', page, re.S)
        if not head:
            raise TAError("search page has no results heading (layout changed?)")
    count = _V2_COUNT.search(page if start >= 0 else head.group(0))
    if not count:
        raise TAError("search page has no result count (layout changed?)")
    total = int(count.group(1).replace(",", ""))
    out: list[tuple[str, str]] = []
    rows = [chunk.split("</a>", 1)[0] for chunk in page.split('<a class="sst-game-row"')[1:]]
    for row in rows:
        if not re.match(r'^ href="(?:https://www\.trueachievements\.com)?/game/', row):
            continue  # series and other non-game rows share the markup
        m = _V2_ROW.match(row)
        if not m:
            raise TAError("a search result row is unreadable (layout changed?)")
        if (t := _text(m.group(2)), m.group(1)) not in out:
            out.append((t, m.group(1)))
    if total > 0 and not out:
        raise TAError(f"search page says {total} results but no game rows were read (layout changed?)")
    return out


class SearchResults(list):
    """Search hits; `complete` is False when more result pages existed than were read."""

    complete: bool = True


def search_has_page(page: str, number: int) -> bool:
    """Whether a new-layout results page links to results page `number`."""
    return bool(re.search(r'href="[^"]*[?&](?:amp;)?page=%d"' % number, page))


def parse_search(page: str, url: str) -> list[tuple[str, str]]:
    """(title, slug) pairs for the Game rows. A single hit may redirect to the game itself.

    Raises TAError when the page is no recognisable results page, or its count and rows disagree.
    """
    m = _GAME_PATH.match(urlsplit(url).path)
    if m:
        title = page_title(page)
        if not title:
            raise TAError("search redirected to a game page without a title")
        return [(title, m.group(1))]
    if 'class="results-heading"' in page:
        return _parse_search_v2(page)
    if 'id="oSearchResultsHolder"' not in page:
        raise TAError("search page has no results area (layout changed?)")
    out: list[tuple[str, str]] = []
    for slug, title, kind in _SEARCH_ROW.findall(page):
        if kind.strip() == "Game" and (t := _text(title), slug) not in out:
            out.append((t, slug))
    return out


def _fields(page: str) -> dict[str, str]:
    return {_text(k): v for k, v in re.findall(r"<dt>([^<]+)</dt><dd>(.*?)</dd>", page, re.S)}


def parse_game(page: str, slug: str) -> TAGame:
    # Unreleased games have a negative placeholder id (verified 2026-11-01, Persona 6 = -7827).
    ids = set(re.findall(r'data-game="(-?\d+)"', page))
    if len(ids) != 1:
        raise TAError(f"game page {slug!r} has {len(ids)} game ids")
    title = page_title(page)
    if not title:
        raise TAError(f"game page {slug!r} has no title")
    raw = _text(_fields(page).get("Platform", ""))
    platforms = tuple(p for p in (re.sub(r"\s*\(.*?\)", "", part).strip() for part in raw.split(",")) if p)
    return TAGame(int(ids.pop()), title, platforms[0] if platforms else "", slug, platforms)


_DLC_HEAD = re.compile(r'<div class="pnl-hd([^"]*)">')


def parse_dlc(page: str) -> list[tuple[str, int]]:
    """(title, DLC id) for every pack on a game's /dlc page, in page order."""
    h1 = re.search(r'<h1 class="gh pagetitle">(.*?)</h1>', page, re.S)
    if not h1 or not _text(h1.group(1)).endswith("DLC Achievements"):
        raise TAError("not a DLC page")
    heads = list(_DLC_HEAD.finditer(page))
    out: list[tuple[str, int]] = []
    for i, head in enumerate(heads):
        if "dlc" not in head.group(1).split():
            continue
        chunk = page[head.end():heads[i + 1].start() if i + 1 < len(heads) else len(page)]
        title = re.search(r"<h2><a [^>]*>(.*?)</a></h2>", chunk, re.S)
        dlc_id = re.search(r'id="btnDLC_(\d+)"', chunk)
        if not title or not dlc_id:
            raise TAError("a DLC pack on the DLC page has no title or id (layout changed?)")
        out.append((_text(title.group(1)), int(dlc_id.group(1))))
    return out


_EXCLUDED_TITLE = re.compile(r"\((?:Windows|Xbox 360)\)\s*$", re.I)
_EXCLUDED_SLUG = re.compile(r"-(?:Windows|Xbox-360)$", re.I)


def excluded_page(page: TAGame) -> bool:
    """A Windows or Xbox 360 page by its title or slug suffix, whatever its Platform field says."""
    return bool(_EXCLUDED_TITLE.search(page.title) or _EXCLUDED_SLUG.search(page.slug))


def choose_ta_page(pages: list[TAGame]) -> TAGame | None:
    """The Xbox Series X|S page, else the Xbox One page; never a Windows or Xbox 360 page."""
    for wanted in (SERIES, ONE):
        for p in pages:
            if p.platform == wanted and not excluded_page(p):
                return p
    return None


def _check_id(value, *, zero_ok: bool) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if zero_ok else 1):
        raise ValueError("TrueAchievements ids are positive integers (DLC id 0 = the base game)")
    return value


# --------------------------------------------------------------------------- client


class TAClient:
    def __init__(
        self,
        gamertag: str,
        password: str,
        fs: FlareSolverr,
        session_path: Path | None = None,
        *,
        http: HttpTransport | None = None,
        game_ids: dict[str, int] | None = None,
    ):
        self._tag = gamertag
        self._password = password
        self._fs = fs
        self._path = Path(session_path) if session_path else None
        self._http = http or CurlTransport()
        self._gamer = "/gamer/" + quote(gamertag.replace(" ", "+"), safe="+")
        self._cookies: dict[str, str] = self._load()
        self._clearance: tuple[str, str] | None = None  # (cf_clearance or "", its user agent)
        # Game slug -> TA game id, for DLC rows. Callers may seed it from saved state.
        self.game_ids: dict[str, int] = dict(game_ids or {})

    def __repr__(self) -> str:
        return "TAClient(<credentials hidden>)"

    # ---- session file

    def _load(self) -> dict[str, str]:
        if not self._path or not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except ValueError:
            return {}  # an unreadable session just means logging in again
        except OSError as e:
            raise TAError(f"cannot read the session file ({type(e).__name__})") from None
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if k != "cf_clearance"}

    def _save(self) -> None:
        if not self._path:
            return
        try:
            if not self._path.parent.exists():
                self._path.parent.mkdir(parents=True, mode=0o700)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self._cookies, fh)
            os.chmod(self._path, 0o600)
        except OSError as e:
            raise TAError(f"cannot save the session file ({type(e).__name__})") from None

    # ---- plumbing

    def _label(self, method: str, url: str) -> str:
        return method + " " + urlsplit(url).path.replace(self._gamer, "/gamer/<gamer>")

    def _refresh_clearance(self) -> None:
        """A fresh cf_clearance and user agent from an anonymous FlareSolverr GET (no cookies, no body)."""
        s = self._fs.get(CLEARANCE_URL)
        if s.status >= 400:
            raise TAError(f"FlareSolverr's clearance fetch answered HTTP {s.status}")
        if not s.user_agent:
            raise TAError("FlareSolverr returned no user agent")
        self._clearance = (s.cookies.get("cf_clearance", ""), s.user_agent)

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers = dict(extra or {})
        cookies = dict(self._cookies)
        if self._clearance:
            clearance, user_agent = self._clearance
            headers["User-Agent"] = user_agent
            if clearance:
                cookies["cf_clearance"] = clearance
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return headers

    def _absorb(self, r: HttpResponse) -> None:
        """Keep the cookies TA set or cleared (a renewed cf_clearance renews the clearance)."""
        for header in r.header_list("set-cookie"):
            parsed = _set_cookie(header)
            if not parsed:
                continue
            name, value = parsed
            if name == "cf_clearance":
                if self._clearance and value:
                    self._clearance = (value, self._clearance[1])
            elif value is None:
                self._cookies.pop(name, None)
            else:
                self._cookies[name] = value

    def _once(self, method: str, url: str, body: str | None, headers: dict[str, str] | None,
              follow: bool) -> tuple[HttpResponse, str]:
        """One request, following same-site redirects (each hop's cookies are kept)."""
        for _ in range(MAX_REDIRECTS + 1):
            r = self._http.request(method, url, body, self._headers(headers))
            self._absorb(r)
            if not follow or r.status not in _REDIRECTS or not r.header("location"):
                return r, url
            nxt = urljoin(url, r.header("location"))
            if not nxt.startswith(BASE + "/"):
                raise TAError(f"{self._label(method, url)} redirected away from TrueAchievements")
            if r.status not in (307, 308):
                method, body = "GET", None
                headers = {k: v for k, v in (headers or {}).items() if k.lower() not in ("content-type", "origin")}
            url = nxt
        raise TAError(f"{self._label(method, url)} redirected too many times")

    def _send(self, method: str, url: str, body: str | None = None, headers: dict[str, str] | None = None,
              *, follow: bool = True) -> tuple[HttpResponse, str]:
        """A request through curl_cffi; a Cloudflare challenge refreshes the clearance once."""
        if not url.startswith(BASE + "/"):
            raise ValueError("only TrueAchievements URLs are requested with the session")
        r, final = self._once(method, url, body, headers, follow)
        if challenged(r):
            self._refresh_clearance()
            r, final = self._once(method, url, body, headers, follow)
            if challenged(r):
                raise TAError(f"{self._label(method, url)} answered HTTP {r.status} "
                              "(Cloudflare challenge, after a fresh clearance)")
        return r, final

    def _page(self, method: str, url: str, body: str | None = None, headers: dict[str, str] | None = None) -> Page:
        r, final = self._send(method, url, body, headers)
        if r.status >= 400:
            raise TAError(f"{self._label(method, url)} answered HTTP {r.status}")
        if r.status in _REDIRECTS:
            raise TAError(f"{self._label(method, url)} answered a redirect without a location")
        return Page(r.status, r.text, final)

    def _get(self, url: str, *, need_login: bool) -> Page:
        """A page. A gated one (or, with `need_login`, a logged-out one) is retried once after login."""
        for attempt in (1, 2):
            p = self._page("GET", url)
            if not gated(p.html, need_login):
                return p
            if attempt == 2:
                raise AuthError("TrueAchievements session not accepted after a fresh login")
            self.login()
        raise AssertionError("unreachable")

    # ---- session

    def login(self) -> None:
        self._cookies = {}
        page = self._page("GET", BASE + "/login")
        form = login_form(page.html)
        form.update({"txtGamerTag": self._tag, "txtPassword": self._password,
                     "Command": "btnLogin_click", "Argument": ""})
        self._page("POST", BASE + "/login", urlencode(form), {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE,
            "Referer": BASE + "/login",
        })
        if not self._cookies.get("GamerToken"):
            self._cookies = {}
            raise AuthError("TrueAchievements login failed (check the gamertag and password)")
        self._save()

    # ---- reads

    def get_html(self, url: str) -> str:
        """Any TA page's HTML (the D2D lists use this)."""
        return self._get(url, need_login=False).html

    def wishlist(self) -> dict[str, Entry]:
        """The logged-in gamer's wishlist, keyed by `"<game id>"` or `"<game id>:<dlc id>"`."""
        rows = self._wishlist_rows()
        out: dict[str, Entry] = {}
        for row in rows:
            key = str(row.id) if row.kind == "game" else f"{self._game_id(row.game_slug)}:{row.id}"
            if key in out:
                raise IncompleteRead(f"wishlist lists {key} twice")
            out[key] = Entry(key, row.title)
        return out

    def _wishlist_rows(self) -> list[WishRow]:
        """Every wishlist page's rows. The rows must add up to the list's total, and the total
        must hold on every page, so a list changing mid-read is never mistaken for the whole list."""
        base = BASE + self._gamer + "/wishlist"
        rows: list[WishRow] = []
        total: int | None = None
        number = 1
        while True:
            page = parse_wishlist_page(self._get(base + (f"?page={number}" if number > 1 else ""), need_login=True).html)
            if page.number != number:
                raise IncompleteRead(f"asked for wishlist page {number}, got page {page.number}")
            if number == 1:
                total = page.total
            elif page.total != total:
                raise IncompleteRead(f"wishlist total changed from {total} to {page.total} between pages")
            rows += page.rows
            if not page.has_next:
                break
            if number >= MAX_WISHLIST_PAGES:
                raise IncompleteRead(f"wishlist has more than {MAX_WISHLIST_PAGES} pages")
            number += 1
        if total is not None and len(rows) != total:
            raise IncompleteRead(f"read {len(rows)} of the {total} wishlist entries")
        return rows

    def collection(self) -> list[OwnedRow]:
        """The logged-in gamer's whole Game Collection (every ownership status, all platforms).

        Reads `/gamecollection` for the list's filters, then each AjaxList page by GET until the
        rows read match the footer's total. Raises IncompleteRead for anything short of that, so a
        partial list is never mistaken for the collection.
        """
        base = BASE + self._gamer + "/gamecollection"
        filters = list_filters(self._get(base, need_login=True).html)
        names = {k for k, _ in filters}
        if not {"txtGamerID", "oGameCollection_Order"} <= names:
            raise IncompleteRead("collection page lacks its list filters (not the page we expected)")
        unknown = unknown_filters(filters)
        if unknown:
            raise IncompleteRead("collection page has filters this client does not know: " + ", ".join(unknown))
        rows: list[OwnedRow] = []
        total: int | None = None
        page_no = 1
        while True:
            fragment = self._get(list_url(base, filters, page_no), need_login=False).html
            try:
                got, got_total = parse_list(fragment)
            except ValueError as e:
                raise IncompleteRead(f"collection page {page_no}: {e}") from None
            if got_total is None:
                raise IncompleteRead(f"collection page {page_no} has no total")
            if total is None:
                total = got_total
                if total > MAX_COLLECTION:
                    raise IncompleteRead(f"collection claims {total} titles (more than {MAX_COLLECTION})")
            elif got_total != total:
                raise IncompleteRead(f"collection total changed from {total} to {got_total} between pages")
            rows += got
            if len(rows) >= total or not got:
                break
            page_no += 1
        seen: set[int] = set()
        for r in rows:
            if r.ta_id in seen:
                raise IncompleteRead(f"collection lists game {r.ta_id} twice")
            seen.add(r.ta_id)
        if len(rows) != total:
            raise IncompleteRead(f"read {len(rows)} of {total} collection titles")
        return rows

    def _game_id(self, slug: str) -> int:
        if slug not in self.game_ids:
            self.game_ids[slug] = self.game(slug).id
        return self.game_ids[slug]

    def search(self, title: str) -> list[tuple[str, str]]:
        """Game results for `title`, following the games-only result pages up to SEARCH_PAGES."""
        base = BASE + "/search-results?search=" + quote_plus(title) + "&cat=games"
        out = SearchResults()
        for number in range(1, SEARCH_PAGES + 1):
            p = self._get(base + (f"&page={number}" if number > 1 else ""), need_login=False)
            out += [r for r in parse_search(p.html, p.url) if r not in out]
            if not search_has_page(p.html, number + 1):
                break
        else:
            out.complete = False  # more pages remain: never read the list as all there is
        return out

    def game(self, slug: str) -> TAGame:
        p = self._get(BASE + "/game/" + quote(slug, safe=""), need_login=False)
        return parse_game(p.html, slug)

    def dlc(self, game: int | str) -> list[tuple[str, int]]:
        """(title, DLC id) pairs from `/game/<slug>/dlc`; a game id is resolved to its slug first."""
        if isinstance(game, int) and not isinstance(game, bool):
            p = self._get(f"{BASE}/game.aspx?gameid={_check_id(game, zero_ok=False)}", need_login=False)
            m = _GAME_PATH.match(urlsplit(p.url).path)
            if not m:
                raise TAError(f"game id {game} did not lead to a game page")
            slug = m.group(1)
        else:
            slug = quote(str(game), safe="")
        return parse_dlc(self._get(f"{BASE}/game/{slug}/dlc", need_login=True).html)

    # ---- writes

    def add(self, game_id: int, dlc_id: int = 0) -> None:
        self._write("AddToWishlist", game_id, dlc_id)

    def remove(self, game_id: int, dlc_id: int = 0) -> None:
        self._write("RemoveFromWishlist", game_id, dlc_id)

    def _write(self, method: str, game_id: int, dlc_id: int) -> None:
        _check_id(game_id, zero_ok=False)
        _check_id(dlc_id, zero_ok=True)
        what = f"{method} {game_id}" + (f":{dlc_id}" if dlc_id else "")
        relogged = False
        while True:
            hash_key = self._cookies.get("HashKey")
            errors: list[str] = []
            if hash_key:
                r = self._post_ajax(method, game_id, dlc_id, hash_key)
                errors = self._answer_errors(what, r.status, r.text)
                if errors is None:
                    return
            if not hash_key or any(_LOGGED_OUT in e.lower() for e in errors):
                if relogged:
                    raise AuthError(f"{what}: TrueAchievements session not accepted after a fresh login")
                relogged = True
                self.login()
                continue
            if method == "AddToWishlist" and any(_ALREADY_LISTED in e.lower() for e in errors):
                raise AlreadyListed(f"{what}: already on the wishlist")
            raise TAError(f"{what} failed: {'; '.join(errors)[:200]}")

    def _post_ajax(self, method: str, game_id: int, dlc_id: int, hash_key: str) -> HttpResponse:
        body = json.dumps({"Wishlist": {"GameID": game_id, "DLCID": dlc_id, "HashKey": hash_key}})
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE,
            "Referer": BASE + self._gamer + "/wishlist",
        }
        # A write is never re-sent along a redirect; a challenge never reached TA, so it may be.
        r, _ = self._send("POST", AJAX + method, body, headers, follow=False)
        return r

    @staticmethod
    def _answer_errors(what: str, status: int, text: str) -> list[str] | None:
        """None on success, else the site's error messages."""
        if status >= 300:
            raise TAError(f"{what} answered HTTP {status}")
        try:
            d = json.loads(text)["d"]
        except (ValueError, KeyError, TypeError):
            raise TAError(f"{what} answered something that isn't the expected JSON") from None
        if not isinstance(d, dict):
            raise TAError(f"{what} answered something that isn't the expected JSON")
        errors = d.get("errors")
        if d.get("success") or ("errors" in d and errors is None) or (isinstance(errors, list) and not errors):
            return None
        if not isinstance(errors, list):
            errors = [errors]
        return [str(e) for e in errors]

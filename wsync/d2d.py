"""Disc to Digital verdicts: does an Xbox game's disc convert to a digital license?

Two community sources, neither official:
  - TrueAchievements' two Disc to Digital lists ("converts" and "unsupported"), a few
    pages each, fetched through the TA client and cached for 24 hours.
  - XCT.live, queried per wishlist game only (its terms allow per-query use, never bulk
    paging), cached for 7 days and capped per run.

A "yes" or "disputed" verdict makes the Deku Deals entry accept either format.
"""

from __future__ import annotations

import gzip
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .feed import XboxItem, is_dlc
from .match import base_title, norm, strip_edition_noise
from .state import State

TA_BASE = "https://www.trueachievements.com"
TA_LISTS = {"yes": "/disc-to-digital/games", "no": "/disc-to-digital-unsupported/games"}
TA_MIN_TITLES = 50
TA_MAX_PAGES = 30
TA_TTL = timedelta(hours=24)

XCT_API = "https://xct.live/api/v1/d2d"
XCT_UA = "xbox-wishlist-sync/1.0 (personal use)"
XCT_PAGE = 50
XCT_MAX_ROWS = 200
XCT_TTL = timedelta(days=7)
XCT_BUDGET = 10
XCT_MAX_ERRORS = 2
XCT_STATUSES = frozenset({"yes", "no", "unconfirmed", "retest"})

log = logging.getLogger(__name__)

# <tr class="even|odd" id="trN"> ... <td class="game"><a href="...">Title</a></td>
_TA_TITLE = re.compile(
    r'<tr class="(?:even|odd)" id="tr\d+">.*?<td class="game"><a [^>]*>([^<]+)</a>', re.S
)
# TA tags a platform onto some titles: "7 Days to Die (Xbox One)".
_TA_SUFFIX = re.compile(
    r"\s*\((?:Xbox One|Xbox Series X\|S|Xbox 360|Windows|PC|Xbox)\)\s*$", re.I
)
# XCT lists separate disc entries per print: "Battlefield 2042 (SX)", "(X1)", regional ones.
_XCT_PRINT = re.compile(r"\s*\((SX|X1)\)\s*$", re.I)
_XCT_REGION = re.compile(r"\s*\((China|Japan)\)\s*$", re.I)


class D2DError(Exception):
    """A source answered with something we can't read as a verdict."""


class XctIncomplete(D2DError):
    """XCT is rebuilding its index and had no match: not evidence the game is unlisted."""


class Fetcher(Protocol):
    def get_html(self, url: str) -> str: ...


# --------------------------------------------------------------------------- verdict


def verdict(ta_yes: bool, ta_no: bool, xct: str | None) -> str | None:
    """Combine the sources: yes / no / disputed, or None when nobody has data."""
    if xct in ("varies", "unconfirmed", "retest"):
        return "disputed"
    positive = ta_yes or xct == "yes"
    negative = ta_no or xct == "no"
    if positive and negative:
        return "disputed"
    if positive:
        return "yes"
    if negative:
        return "no"
    return None


# --------------------------------------------------------------------------- TrueAchievements


def parse_ta_list(page: str) -> list[str]:
    """Game titles on one list page, in page order."""
    return [html.unescape(t).strip() for t in _TA_TITLE.findall(page)]


def ta_last_page(page: str, path: str) -> int:
    """The highest page number linked from this list page (1 when unpaged)."""
    nums = re.findall(re.escape(path) + r"\?page=(\d+)", page)
    return max([1] + [int(n) for n in nums])


def _fetch_ta_list(fetch: Fetcher, path: str, pause: float) -> list[str]:
    titles: list[str] = []
    last = 1
    for n in range(1, TA_MAX_PAGES + 1):
        if n > 1 and pause:
            time.sleep(pause)
        page = fetch.get_html(f"{TA_BASE}{path}" + (f"?page={n}" if n > 1 else ""))
        if n == 1 and f'href="{TA_BASE}{path}"' not in page:
            raise D2DError(f"TrueAchievements did not return {path} (challenge not cleared?)")
        rows = parse_ta_list(page)
        last = max(last, ta_last_page(page, path))
        titles += rows
        if not rows or n >= last:
            break
    if len(titles) < TA_MIN_TITLES:
        raise D2DError(
            f"TrueAchievements {path} parsed to only {len(titles)} titles; page layout changed?"
        )
    return titles


def fetch_ta_lists(fetch: Fetcher, *, pause: float = 1.0) -> dict:
    """{"yes": [...], "no": [...]}: the converts and the unsupported lists, all pages."""
    return {
        status: _fetch_ta_list(fetch, path, pause)
        for status, path in TA_LISTS.items()
    }


# --------------------------------------------------------------------------- XCT.live


def default_http_get(url: str, params: dict) -> dict:
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": XCT_UA, "Accept": "application/json", "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


def _xct_query(q: str, http_get: Callable[[str, dict], dict]) -> tuple[list[dict], bool]:
    """(rows, rebuilding) for one literal query, following pages up to a sane cap."""
    rows: list[dict] = []
    rebuilding = False
    while len(rows) < XCT_MAX_ROWS:
        # An empty status means every status; "status=no" oddly hides the "no" rows.
        data = http_get(
            f"{XCT_API}/list",
            {"sort": "title", "dir": "asc", "limit": XCT_PAGE, "offset": len(rows),
             "status": "", "q": q},
        )
        if not isinstance(data, dict) or "error" in data or not isinstance(data.get("rows"), list):
            raise D2DError(f"unexpected XCT response: {str(data)[:200]}")
        rows += data["rows"]
        rebuilding = rebuilding or bool(data.get("rebuilding"))
        total = data.get("shown", data.get("total", 0))
        if not data["rows"] or len(rows) >= total:
            break
    return rows, rebuilding


def _row_status(row: dict) -> str:
    status = row.get("status")
    if status not in XCT_STATUSES:
        return "unconfirmed"
    # A row flagged "varies" means its prints differ, unless it is only unconfirmed.
    if row.get("varies") and status != "unconfirmed":
        return "varies"
    return status


def _match_rows(rows: list[dict], wanted: set[str]) -> list[dict]:
    """The title's own entry; else its SX/X1 prints; else regional prints (last resort)."""
    plain = [r for r in rows if norm(r.get("title") or "") in wanted]
    if plain:
        return plain
    for suffix in (_XCT_PRINT, _XCT_REGION):
        hits = [
            r for r in rows
            if suffix.search(r.get("title") or "") and norm(suffix.sub("", r["title"])) in wanted
        ]
        if hits:
            return hits
    return []


def xct_lookup(
    title: str, http_get: Callable[[str, dict], dict] | None = None
) -> str | None:
    """XCT.live's status for one title: yes/no/varies/unconfirmed/retest, None if unlisted."""
    http_get = http_get or default_http_get
    # (query text, titles its rows may carry): the title as typed, then, when it ends in
    # edition noise ("Alan Wake 2 Deluxe Edition"), the base game's title.
    attempts = [(title, {norm(title), base_title(title)} - {""})]
    bare = strip_edition_noise(title)
    if norm(bare) and norm(bare) != norm(title):
        attempts.append((bare, {norm(bare)}))
    rebuilding = False
    for text, wanted in attempts:
        query = " ".join(text.replace("®", " ").replace("™", " ").split())
        if not query:
            continue
        # XCT's search is literal and apostrophe-sensitive: try the other form if nothing matches.
        forms = [query]
        other = query.replace("'", "’") if "'" in query else query.replace("’", "'")
        if other != query:
            forms.append(other)
        for form in forms:
            rows, rebuild = _xct_query(form, http_get)
            rebuilding = rebuilding or rebuild
            hits = _match_rows(rows, wanted)
            if hits:
                statuses = {_row_status(r) for r in hits}
                return statuses.pop() if len(statuses) == 1 else "varies"
    if rebuilding:
        raise XctIncomplete(f"XCT index is rebuilding; no answer for {title!r}")
    return None


# --------------------------------------------------------------------------- orchestration


def _parse_time(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _age(value: str | None, now: datetime) -> timedelta | None:
    try:
        return now - _parse_time(value) if value else None
    except (TypeError, ValueError):
        return None


def _ta_lists(state: State, fetch: Fetcher, now: datetime, pause: float) -> dict:
    cached = state.ta_lists or {}
    have = isinstance(cached.get("yes"), list) and isinstance(cached.get("no"), list)
    age = _age(cached.get("fetched"), now)
    if have and age is not None and age < TA_TTL:
        return cached
    try:
        lists = fetch_ta_lists(fetch, pause=pause)
    except Exception:
        # A stale list beats none; with nothing cached the run can't judge, so fail loudly.
        if have:
            return cached
        raise
    state.ta_lists = {"fetched": now.isoformat(), **lists}
    return state.ta_lists


def _keys(titles) -> set[str]:
    return {norm(_TA_SUFFIX.sub("", t)) for t in titles} - {""}


def _when(value) -> datetime | None:
    try:
        return _parse_time(value) if value else None
    except (TypeError, ValueError):
        return None


def refresh_verdicts(
    items: list[XboxItem],
    state: State,
    fetch: Fetcher,
    http_get: Callable[[str, dict], dict] | None,
    now: datetime,
    *,
    pause: float = 1.0,
) -> dict[str, str | None]:
    """productId -> verdict (yes / no / disputed / None). Mutates state.ta_lists and state.d2d.

    The XCT answer is cached for 7 days, but the verdict is recombined with the current TA
    lists on every run. DLC gets None. Due lookups run oldest attempt first (never-attempted
    first), up to the per-run budget; each attempt is recorded (`attempted`), and an attempt
    that ends incomplete or in an error keeps any older answer. Items without a fresh answer
    fall back to the last cached XCT answer, else TA alone, and are retried on later runs.
    """
    lists = _ta_lists(state, fetch, now, pause)
    yes_keys, no_keys = _keys(lists["yes"]), _keys(lists["no"])

    def entry_of(pid: str) -> dict | None:
        e = state.d2d.get(pid)
        return e if isinstance(e, dict) else None

    def fresh(e: dict | None) -> bool:
        if not e or "xct" not in e:
            return False
        age = _age(e.get("checked"), now)
        return age is not None and age < XCT_TTL

    never = datetime.min.replace(tzinfo=timezone.utc)

    def last_try(i: XboxItem) -> datetime:
        e = entry_of(i.product_id) or {}
        return _when(e.get("attempted")) or _when(e.get("checked")) or never

    due = sorted((i for i in items if not is_dlc(i) and not fresh(entry_of(i.product_id))), key=last_try)
    budget = XCT_BUDGET
    errors = 0  # consecutive transport errors; two in a row stop XCT lookups for this run
    answers: dict[str, str | None] = {}
    counts = {"hit": 0, "unlisted": 0, "incomplete": 0, "error": 0, "deferred": 0}
    for item in due:
        pid = item.product_id
        if budget <= 0 or errors >= XCT_MAX_ERRORS:
            counts["deferred"] += 1
            continue
        budget -= 1
        try:
            xct = xct_lookup(item.title, http_get)
        except XctIncomplete:
            errors = 0  # XCT answered; it just can't be trusted yet. Retried next run.
            counts["incomplete"] += 1
        except (OSError, ValueError, D2DError):
            errors += 1  # keep any older answer; retried next run
            counts["error"] += 1
        else:
            errors = 0
            answers[pid] = xct
            counts["hit" if xct is not None else "unlisted"] += 1
            continue
        state.d2d[pid] = {**(entry_of(pid) or {}), "attempted": now.isoformat()}

    out: dict[str, str | None] = {}
    for item in items:
        pid = item.product_id
        if is_dlc(item):
            out[pid] = None
            continue
        keys = {norm(item.title), base_title(item.title)} - {""}
        ta_yes, ta_no = bool(keys & yes_keys), bool(keys & no_keys)
        entry = entry_of(pid)
        if pid in answers:
            result = verdict(ta_yes, ta_no, answers[pid])
            state.d2d[pid] = {"verdict": result, "checked": now.isoformat(), "xct": answers[pid],
                              "attempted": now.isoformat()}
        else:
            result = verdict(ta_yes, ta_no, entry["xct"] if entry and "xct" in entry else None)
            if fresh(entry):
                state.d2d[pid] = {**entry, "verdict": result}
        out[pid] = result
    log.info("d2d: XCT lookups this run: hit %d, unlisted %d, incomplete %d, error %d, deferred %d",
             counts["hit"], counts["unlisted"], counts["incomplete"], counts["error"], counts["deferred"])
    return out

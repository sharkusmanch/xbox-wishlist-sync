"""TrueAchievements Game Collection: filters, AjaxList URLs and list parsing.

Ported from the `xbox` plugin's `ta_collection.py`, without its FlareSolverr transport:
`TAClient.collection()` fetches these URLs through its own curl_cffi session. Protocol notes
(verified live 2026-11-02) are in docs/ta-protocol.md, "Game Collection".

In short: the logged-in `/gamer/<tag>/gamecollection` page carries the list's filters as form
controls inside `.ajaxlistfilter` containers; the list itself is an AjaxList fragment fetched with
a **GET** of `?executeformfunction&function=AjaxList&params=oGameCollection|&k=v...` (a POST of the
same URL answers "no matching titles"), at most 100 rows a page, with the total in the footer row.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote

PER_PAGE = 100  # TrueAchievements caps an AjaxList page at 100 rows
# Columns switched on, on top of the gamer's saved column choices.
EXTRA_COLS = ("Platform", "Ownershipstatus", "Media")
# Filters set to fixed values whatever view the gamer saved on the site, so a narrowed view can't
# shorten the read with a footer total that still agrees. Each "all" value is the control's
# `data-default` in the live markup (2026-11-02). The exceptions are on purpose: the title type is
# games only (the page's blank default would include apps), and the sort is by title, a stable
# order for paging (the saved one, time played, can tie and shift between page fetches).
PINNED = {
    "ddlStartedStatus": "0",
    "ddlDifficulty": "0",
    "ddlCompletionStatus": " ",
    "sddGameMediaID": " ",
    "ddlGameNotes": " ",
    "ddlMythicStatus": " ",
    "asdGamePropertyID": " ",
    "ddlTitleType": "Game",
    "ddlSortBy": "TitleName",
    "oGameCollection_Order": "TitleName",
}
# Dropped from the request: a search term narrows the list, and "exclude not owned" hides rows.
DROPPED = ("txtSearchString", "chkExcludeNotOwned")
# Filter names known not to narrow the list: view, DLC inclusion, columns, paging, ids.
_HARMLESS = re.compile(r"^(GameView|MultiEditMode|ddlDLCInclusionSetting|chkCol\w+|oGameCollection_\w+|"
                       r"txt\w*ID|txtUseAchievementsForProgress)$")


@dataclass(frozen=True)
class OwnedRow:
    ta_id: int
    title: str
    platform: str  # TA's platform icon slug: xbox-series-x-s, xbox-one, xbox-360, windows, ...
    ownership: str  # Owned, Xbox Game Pass (deprecated), EA Play, No longer have, ...
    media: str  # Disc, Digital, Disc & Digital, Disc-to-Digital, or blank


class _FilterParser(HTMLParser):
    """name=value pairs from inputs/selects inside .ajaxlistfilter containers, as the page's
    AJAXList.GetListFilters builds them.

    A `multiple` select (ownership, play status, platform) contributes nothing: with no option
    chosen it means "any", but its first option is not a default; sending it narrowed the list to
    "Owned" and "Playing now" (a fraction of the titles, live 2026-11-02). The sync wants the whole
    collection whatever view the gamer saved.
    """

    VOID = {"input", "img", "br", "meta", "link", "hr", "source", "option"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[bool] = []
        self.depth = 0
        self.sel: list | None = None  # name, selected value, first value
        self.out: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        is_filter = "ajaxlistfilter" in (a.get("class") or "").split()
        if tag not in self.VOID:
            self.stack.append(is_filter)
            self.depth += is_filter
        if self.depth == 0 and not is_filter:
            return
        if tag == "select" and a.get("name"):
            self.sel = [a["name"], None, None] if "multiple" not in a else None
        elif tag == "option" and self.sel is not None:
            v = a.get("value") or ""
            if self.sel[2] is None:
                self.sel[2] = v
            if "selected" in a:
                self.sel[1] = v
        elif tag == "input" and a.get("name") and a.get("value"):
            if a.get("type", "text") in ("radio", "checkbox"):
                if "checked" in a:
                    self.out.append((a["name"], a["value"]))
            else:
                self.out.append((a["name"], a["value"]))

    def handle_endtag(self, tag):
        if tag == "select" and self.sel is not None:
            v = self.sel[1] if self.sel[1] is not None else self.sel[2]
            if v:
                self.out.append((self.sel[0], v))
            self.sel = None
        if tag not in self.VOID and self.stack:
            self.depth -= self.stack.pop()


def list_filters(page: str) -> list[tuple[str, str]]:
    p = _FilterParser()
    p.feed(page)
    return p.out


def unknown_filters(filters) -> list[str]:
    """Names of filters the page sends that are neither pinned/dropped nor known to be harmless.

    A new or renamed control on the site could narrow the list silently, so `collection()` refuses
    to read while there is one (names only: values are the gamer's)."""
    known = set(PINNED) | set(DROPPED)
    return sorted({k for k, _ in filters if k not in known and not _HARMLESS.match(k)})


def list_url(base_url: str, filters, page_no: int) -> str:
    """The GET form of the page's `ff.Call('AjaxList', ...)` request for page `page_no`.

    `base_url` is the gamer's `/gamecollection` URL. Drops the search term and "exclude not owned",
    pins every narrowing filter (see PINNED), asks for 100 rows and switches on the Platform,
    Ownership and Media columns.
    """
    kv = [(k, v) for k, v in filters if k not in DROPPED]
    have = {k for k, _ in kv}
    kv += [("chkCol" + c, "True") for c in EXTRA_COLS if "chkCol" + c not in have]
    over = dict(PINNED, oGameCollection_ItemsPerPage=str(PER_PAGE), oGameCollection_Page=str(page_no),
                oGameCollection_ShowAll="False")
    kv = [(k, over.pop(k, v)) for k, v in kv] + list(over.items())
    f = "".join("&%s=%s" % (k, v.replace("&", "*amp*", 1)) for k, v in kv)  # AJAXList.ReplaceCharacters
    params = "oGameCollection|" + f.replace("|", "/\\pipe\\/")  # ff.SafeParam
    return (base_url + "?executeformfunction&function=AjaxList&params="
            + quote(params, safe="-_.!~*'()"))


def _cell_text(cell: str) -> str:
    if "<img" in cell and not re.sub(r"<[^>]+>", "", cell).strip():  # icon-only, e.g. platform
        titles = re.findall(r'title="([^"]+)"', cell)
        if titles:
            return ", ".join(html.unescape(t) for t in titles)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


_NEEDED = ("Title", "Platform", "Ownership", "Media")


def parse_list(fragment: str) -> tuple[list[OwnedRow], int | None]:
    """(rows, footer total) from one AjaxList page.

    A fragment without a table gives ([], None or the total). A row layout it does not understand
    (a needed column missing, no `EditDetails(<id>` in a game row) raises ValueError; callers treat
    that as an incomplete read.
    """
    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", fragment, re.S)
    m = re.search(r"([\d,]+) Titles", _cell_text(fragment))
    total = int(m.group(1).replace(",", "")) if m else None
    if not trs:
        return [], total
    head = [_cell_text(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", trs[0], re.S)]
    missing = [n for n in _NEEDED if n not in head]
    game_rows = [tr for tr in trs[1:] if 'href="/game/' in tr]  # the rest is the totals footer
    if missing and game_rows:
        raise ValueError("collection list lacks the " + ", ".join(missing) + " column(s)")
    rows = []
    for tr in game_rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        rec = {name: _cell_text(c) for name, c in zip(head, cells) if name}
        ident = re.search(r"EditDetails\((\d+)", tr)
        if not ident or not rec.get("Title"):
            raise ValueError("a collection row has no title or game id")
        rows.append(OwnedRow(int(ident.group(1)), rec["Title"], rec["Platform"],
                             rec["Ownership"], rec["Media"]))
    return rows, total

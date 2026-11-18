"""Library step: keep the TrueAchievements (TA) Game Collection in line with what Microsoft says the owner owns.

Two parts: the plan (pure, no I/O, below) and `run_library_step` (the daily pass `wsync.run` calls between the
wishlist and Deku collection steps: Microsoft and TA reads, paced page lookups, write-ahead TA writes).

The plan is ported from the games repo's scripts/xbox-library.py (`reconcile`) and scripts/ta-library-fix.py
(`ta_slug`, `slug_candidates`, `accept_page`, `make_plan`):

- `reconcile`: each owned game against the TA collection: ok (in TA as Owned or Games with Gold, or a free
  game in TA at all), wrong (in TA with another status) or missing (with a possible-match hint);
- `plan_library`: the TA writes and the items to check by hand. A wrong ownership becomes Owned (media Digital
  when TA has none): No longer have or Renting on any licence, a subscription status only with a paid order or a
  claim; any other status (Loaned out, Trial, ...) is the owner's and left alone. A missing game's own TA page is
  added as Owned (Games with Gold for a free claim on TA's Games with Gold list), media Digital. A possible
  match, an uncertain version or ownership, or no page: a Review, never a write. The step looks up a missing
  game's pages (`candidate_slugs`, keyed by `lookup_key`); a game not looked up yet is a lookup.

Generations: the TA page's own Platform field decides; the Store catalog's console generation only chooses
among the pages that fit.
- Only an orders-only console game without ConsoleGen9 can be an Xbox 360 / original Xbox game (backward
  compatible 360 products report ConsoleGen8 only). A Collections licence, a ConsoleGen9 or Smart Delivery
  purchase and a PC-only one never match an Xbox 360 / Xbox page or entry. They take the Xbox Series X|S page for
  ConsoleGen9, the Xbox One page for ConsoleGen8, both for both (one page listing both is one add); a PC-only
  licence takes the Windows page.
- An orders-only game that can be a 360 game takes the Xbox 360 / Xbox page or entry when only those fit, the
  modern ones (as above) when only those fit, and is a review when both kinds do. One with the key of a
  Collections game in the same plan (a bundled copy of a modern game) never writes an Xbox 360 / Xbox entry: a
  review instead.
- A console licence never fixes TA's separate "(Windows)" entry (its console page is added instead); a PC-only
  licence fixes only a Windows entry.
- A `library` override slug is the game's page, without the title and generation checks; "skip" drops it. An
  override whose page the lookup didn't find is a review of its own kind, "override", never "no-page" (which
  the live check task leaves out), so a mistyped override doesn't stay silent.

Never planned: a removal, a change to an Owned or Games with Gold entry or to an ownership other than a
subscription, No longer have or Renting, any ownership but Owned on a fix, or a media change on an entry that has
media.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .errors import TAError
from . import library_report
from .flaresolverr import FlareSolverrError
from .overrides import SKIP
from .owned import (BARE_PLATFORM, PLATFORM_SUFFIX, PLATFORM_TAG, TIER_PLAIN, OwnedGame, game_key, hint, match_keys,
                    owned_games, same_game)
from .ta import ONE, SERIES, AlreadyListed, AuthError, ChangedOnTA, TAGame, Unreachable
from .ta_coll import OwnedRow
from .xbox import SignInNeeded, TokenStore, XboxError, catalog_lookup, fetch_collections, fetch_orders, open_session

OWNED, GWG, DIGITAL = "Owned", "Games with Gold", "Digital"
OWNED_TA = frozenset({OWNED, GWG})
SUBSCRIPTIONS = frozenset({"Xbox Game Pass (deprecated)", "Xbox Game Pass Ultimate", "Xbox Game Pass Premium",
                           "Xbox Game Pass Essential", "PC Game Pass", "EA Play", "Ubisoft+"})
PAID = frozenset({"paid", "claim"})  # the evidence that may turn a subscription status into Owned
FIXABLE = frozenset({"No longer have", "Renting"})  # turned into Owned on any active licence
WINDOWS = "Windows"
LAST_GEN = frozenset({"Xbox 360", "Xbox"})  # TA platform names of Xbox 360 and original Xbox games
MODERN = frozenset({SERIES, ONE, WINDOWS})
ROW_LAST_GEN = frozenset({"xbox-360", "xbox"})  # the same as the collection list's platform icons
ROW_MODERN = frozenset({"xbox-series-x-s", "xbox-one", "windows"})
GEN8, GEN9 = "ConsoleGen8", "ConsoleGen9"
GENS = ((GEN8, ONE), (GEN9, SERIES))  # Store catalog generation -> its TA page's platform
MAX_CANDIDATES = 6
LAST_GEN_VARIANTS = ("Xbox-360", "Xbox-One")  # for a game that can be a 360 game
MODERN_VARIANTS = ("Xbox-One", "Xbox-Series-XS")  # for any other, plus -Windows when it's PC-only
EA_REASON = "pre-2016 licence for an EA Play game: could be an EA Access vault licence rather than a purchase"
TWIN_REASON = "only Xbox 360 / Xbox {} fit, and a Collections licence has the same game: "
_X360_TITLE = re.compile(r"\(Xbox 360\)\s*$", re.I)
_X360_SLUG = re.compile(r"-Xbox-360$", re.I)
_WINDOWS_TITLE = re.compile(r"\(Windows\)\s*$", re.I)
_SEP = re.compile(r"\s+[-–—]\s+|\s*:\s*")
_EDITION_END = re.compile(r"(?i)\b(?:edition|bundle)\s*$")
# owned.py's title-key patterns (written for lowercased text), case-insensitive for Store titles
_PLATFORM_TAG, _PLATFORM_SUFFIX, _BARE_PLATFORM, _TIER_PLAIN = (
    re.compile(rx.pattern, re.I) for rx in (PLATFORM_TAG, PLATFORM_SUFFIX, BARE_PLATFORM, TIER_PLAIN))


@dataclass(frozen=True)
class Action:
    kind: str  # "add" | "fix"
    game: OwnedGame
    ta_id: int
    slug: str  # the TA page's slug (an add goes through its page); "" for a fix found in the collection list
    title: str  # TA's title
    sets: tuple[tuple[str, str], ...]  # ("ownership", label), then ("media", label) when media is set


@dataclass(frozen=True)
class Review:
    pid: str  # the game's first Store product ID (`lookup_key`): what an override or the seen set is keyed by
    title: str  # the Store title
    kind: str  # "possible-match" | "versions" | "subscription" | "no-page" | "generation" | "override"
    reason: str


@dataclass
class LibraryPlan:
    actions: list[Action] = field(default_factory=list)  # one per TA game
    review: list[Review] = field(default_factory=list)  # one per game at most
    lookups: list[OwnedGame] = field(default_factory=list)  # games whose TA pages aren't looked up yet
    ok: int = 0  # games TA already has right


@dataclass(frozen=True)
class Match:
    game: OwnedGame
    status: str  # "ok" | "wrong" | "missing"
    rows: tuple[OwnedRow, ...] = ()  # the TA entries the game matches, in collection order
    hint: str | None = None  # missing: a TA title that may be the same game


# ---------------------------------------------------------------- TA pages

def ta_slug(title: str) -> str:
    """TA's URL form of a title: '&' is 'and', punctuation and hyphens go, words are joined with hyphens."""
    return "-".join(re.sub(r"[^\w\s]", "", title.replace("&", "and").replace("-", " ")).split())


def _trim(title: str) -> tuple[str, str]:
    """The title without ®™©, and the same without a trailing tag or edition segment."""
    t = re.sub(r"[®™©]", "", title or "").strip()
    b = re.sub(r"\s*\([^)]*\)\s*$", "", t)
    seps = list(_SEP.finditer(b))
    if seps and _EDITION_END.search(b[seps[-1].end():]):
        b = b[:seps[-1].start()]
    return t, b


def slug_candidates(title: str) -> list[str]:
    """TA page addresses to try for a Store title: as it is, then without a trailing tag or edition segment."""
    out: list[str] = []
    for s in map(ta_slug, _trim(title)):
        if s and s not in out:
            out.append(s)
    return out


def lookup_key(game: OwnedGame) -> str:
    """The key of a game's page lookup and of its review item: its first Store product ID."""
    return min(game.pids)


def lookup_lifetime(key: str, kind: str) -> timedelta:
    """How long a lookup result of `kind` ("found", "seen", "no-page") for the lookup key `key` is reused: its base
    days plus an offset of 0..spread days from a SHA-256 of the key, the same in every run (never Python's
    per-process hash())."""
    base, spread = LIFETIMES[kind]
    offset = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big") % (spread + 1)
    return timedelta(days=base + offset)


def _no_platform(title: str) -> str:
    """The title without a platform tag or a trailing platform phrase ("for Xbox Series X|S", "Xbox One & Xbox
    Series X|S"), as the title keys drop them, its case kept."""
    t = _PLATFORM_TAG.sub(" ", title.replace("&", " and ")).strip()
    return _PLATFORM_SUFFIX.sub("", _BARE_PLATFORM.sub("", t)).rstrip(" -–—:")


def candidate_slugs(game: OwnedGame, override: str | None = None) -> list[str]:
    """The TA pages to look up for a game, at most MAX_CANDIDATES, most specific first: an override's page alone;
    otherwise the guessed addresses of the title without its platform phrase, the root (the plainest form that is
    still the game: without a purchase tier, even one without a separator, but keeping a version TA lists as its
    own game), then the root's platform variants: -Xbox-360 and -Xbox-One for a game that can be a 360 game,
    else -Xbox-One and -Xbox-Series-XS (and -Windows when it's PC-only)."""
    if override:
        return [] if override == SKIP else [override]
    title = _no_platform(game.title)
    t, b = _trim(title)
    key = game_key(game.title)
    root = ta_slug(next((x for x in (_TIER_PLAIN.sub("", b), b) if ta_slug(x) and game_key(x) == key), t))
    if _last_gen_ok(game):
        variants = LAST_GEN_VARIANTS
    else:
        variants = MODERN_VARIANTS + (("Windows",) if game.pc_only else ())
    out: list[str] = []
    for s in slug_candidates(title) + [root] + [f"{root}-{v}" for v in variants if root]:
        if s and s not in out:
            out.append(s)
    return out[:MAX_CANDIDATES]


def accept_page(store_title: str, page: TAGame) -> bool:
    """The page is the Store title's game by the title keys (which drop a platform tag like "(Xbox One)")."""
    return bool(page.title) and same_game(store_title, page.title)


def _platforms(page: TAGame) -> tuple[str, ...]:
    return page.platforms or ((page.platform,) if page.platform else ())


def _last_gen(page: TAGame) -> bool:
    """An Xbox 360 / original Xbox page: its Platform field lists one, or its title or slug says Xbox 360."""
    return bool(LAST_GEN & set(_platforms(page)) or _X360_TITLE.search(page.title) or _X360_SLUG.search(page.slug))


def _modern(page: TAGame) -> bool:
    return bool(MODERN & set(_platforms(page)))


def _pages(pages) -> str:
    return "; ".join(f"{p.title} [{', '.join(_platforms(p)) or 'no platform'}]" for p in pages)


# ---------------------------------------------------------------- the collection

def override_for(game: OwnedGame, overrides: dict[str, dict]) -> str | None:
    """A game's `library` override, keyed on any of its products: "skip" when one says so, else the slug of its
    first product that has one; None without one."""
    return _override(game.pids, overrides)


def _override(pids, overrides: dict[str, dict]) -> str | None:
    slugs = [o["ta"] for pid in sorted(pids) if (o := overrides.get(pid)) and o.get("ta")]
    return SKIP if SKIP in slugs else (slugs[0] if slugs else None)


def _last_gen_ok(game: OwnedGame) -> bool:
    """The licence can be an Xbox 360 / original Xbox game: an orders-only console purchase without ConsoleGen9
    (backward compatible 360 products report ConsoleGen8 only)."""
    return game.source == "orders" and not game.pc_only and GEN9 not in game.gen


def _row_last_gen(row: OwnedRow) -> bool:
    return row.platform in ROW_LAST_GEN or bool(_X360_TITLE.search(row.title))


def _row_fits(game: OwnedGame, row: OwnedRow) -> bool:
    """The TA entry can be this licence's: an Xbox entry of a generation it can be (only `_last_gen_ok` licences
    can be an Xbox 360 / original Xbox game), and TA's separate Windows entry only for a PC-only licence."""
    if _row_last_gen(row):
        return _last_gen_ok(game)
    if row.platform not in ROW_MODERN:
        return False  # not an Xbox entry: mobile, Switch, ...
    windows = bool(_WINDOWS_TITLE.search(row.title))
    return (windows or row.platform == "windows") if game.pc_only else not windows


def _fitting(game: OwnedGame, rows: list[OwnedRow], found: set[int]) -> tuple[OwnedRow, ...]:
    """The rows at positions `found` that fit the game, in collection order, one per TA game."""
    out: list[OwnedRow] = []
    seen: set[int] = set()
    for i in sorted(found):
        if rows[i].ta_id not in seen and _row_fits(game, rows[i]):
            seen.add(rows[i].ta_id)
            out.append(rows[i])
    return tuple(out)


def reconcile(games: list[OwnedGame], ta_rows: list[OwnedRow]) -> list[Match]:
    """Each owned game against the TA collection, in order: ok (in TA as Owned or Games with Gold, or a free game
    in TA at all), wrong (in TA with another status) or missing (not in TA; `hint` names a TA title that may be
    the same game). Only the entries the licence can be count."""
    rows = list(ta_rows)
    by_key: dict[str, set[int]] = {}
    by_word: dict[str, set[int]] = {}  # a hint shares a key's first word: only those titles need checking
    for i, r in enumerate(rows):
        for k in match_keys(r.title):
            by_key.setdefault(k, set()).add(i)
            by_word.setdefault(k.split()[0], set()).add(i)
    out = []
    for g in games:
        keys = match_keys(g.title)
        hits = _fitting(g, rows, {i for k in keys for i in by_key.get(k, ())})
        if not hits:
            near = _fitting(g, rows, {i for k in keys for i in by_word.get(k.split()[0], ())})
            out.append(Match(g, "missing", hint=hint(keys, [r.title for r in near])))
        elif g.acquisition == "free" or any(r.ownership in OWNED_TA for r in hits):
            out.append(Match(g, "ok", hits))
        else:
            out.append(Match(g, "wrong", hits))
    return out


def _rows(rows) -> str:
    return "; ".join(f"{r.title} [{r.platform}]" for r in rows)


def _subscription(status: str) -> bool:
    return status in SUBSCRIPTIONS or "game pass" in status.lower()


def _fixable(status: str) -> bool:
    """A TA ownership a licence may turn into Owned: a subscription status (only with a paid order or a claim) or
    No longer have / Renting. Any other one (Loaned out, Looking to trade/sell, Ordered, Trial, a blank or unknown
    one) is the owner's own: never fixed, never a review item."""
    return _subscription(status) or status in FIXABLE


# ---------------------------------------------------------------- the plan

def _review(game: OwnedGame, kind: str, reason: str) -> Review:
    return Review(lookup_key(game), game.title, kind, reason)


def _choose(game: OwnedGame, accepted: list[TAGame], twin: bool) -> list[TAGame] | Review:
    """The pages a missing game takes, by generation; a Review when none fits or the choice is unclear. `twin`:
    a Collections game in the plan has this orders-only game's key, so it never takes a 360 / Xbox page."""
    last = [p for p in accepted if _last_gen(p)]
    if _last_gen_ok(game) and last:
        if any(_modern(p) for p in accepted):
            return _review(game, "versions", "TA has Xbox 360 / Xbox and newer pages: " + _pages(accepted))
        if twin:
            return _review(game, "versions", TWIN_REASON.format("pages") + _pages(last))
        if len(last) > 1:
            return _review(game, "versions", "several TA game pages: " + _pages(last))
        return last
    pool = [p for p in accepted if _modern(p) and not _last_gen(p)]
    wants = [WINDOWS] if game.pc_only else [plat for gen, plat in GENS if gen in game.gen]
    if not wants:
        return _review(game, "generation", "no console generation in the Store catalog; TA pages: " + _pages(accepted))
    chosen: list[TAGame] = []
    for want in wants:  # the page that is that platform's own, else one that lists it
        fit = [p for p in pool if p.platform == want] or [p for p in pool if want in _platforms(p)]
        if len(fit) > 1:
            return _review(game, "versions", f"several TA game pages for {want}: " + _pages(fit))
        if fit and fit[0] not in chosen:
            chosen.append(fit[0])
    return chosen or _review(game, "generation", f"no TA page for {' or '.join(wants)}: " + _pages(accepted))


def _strength(a: Action) -> tuple[bool, bool]:
    return a.kind == "add", dict(a.sets).get("ownership") == OWNED


class _Planner:
    def __init__(self, ta_rows: list[OwnedRow], pages: dict[str, list[TAGame] | None], gwg: set[int],
                 ea_play: set[int], collections: set[str]):
        self.by_id = {r.ta_id: r for r in ta_rows}
        self.pages, self.gwg, self.ea_play = pages, gwg, ea_play
        self.collections = collections  # the keys of the Collections games in the plan
        self.plan = LibraryPlan()
        self.actions: dict[int, Action] = {}  # TA game id -> its one action

    def matched(self, m: Match) -> None:
        g = m.game
        if m.status == "ok":
            self.plan.ok += 1
        elif m.status == "wrong":
            if not any(_fixable(r.ownership) for r in m.rows):
                self._settle([])  # only statuses the owner set: left alone
                return
            old = [_row_last_gen(r) for r in m.rows]  # only a `_last_gen_ok` game matches a 360 / Xbox entry
            if any(old) and not all(old):
                self._settle([_review(g, "versions", "TA has Xbox 360 / Xbox and newer entries: " + _rows(m.rows))])
            elif all(old) and self._twin(g):
                self._settle([_review(g, "versions", TWIN_REASON.format("entries") + _rows(m.rows))])
            elif len(m.rows) > 1:
                self._settle([_review(g, "versions", "several TA entries: " + _rows(m.rows))])
            else:
                self._settle([self._fix(g, m.rows[0])])
        elif m.hint:
            self._settle([_review(g, "possible-match", f"possible match in TA: {m.hint}")])
        elif (found := self._found(g)) is not None:
            accepted = [p for p in found if accept_page(g.title, p)]
            if not accepted:
                other = f" (found: {'; '.join(p.title for p in found)})" if found else ""
                self._settle([_review(g, "no-page", "no TA page with this title" + other)])
            else:
                chosen = _choose(g, accepted, self._twin(g))
                self._settle([chosen] if isinstance(chosen, Review) else self._land(g, chosen))

    def pinned(self, g: OwnedGame, slug: str, holder: str) -> None:
        """A game with a `library` override slug; `holder` is the product whose override gave it."""
        found = self._found(g)
        if found is None:
            return
        page = next((p for p in found if p.slug.lower() == slug.lower()), None)
        if page is None:  # looked up: nothing at the override's address, or not its page
            other = f" (found: {'; '.join(p.title for p in found)})" if found else ""
            reason = f"the library override's TA page {slug} was not found" + other
            self._settle([Review(holder, g.title, "override", reason)])
        else:
            self._settle(self._land(g, [page]))

    def _twin(self, g: OwnedGame) -> bool:
        return g.source == "orders" and g.key in self.collections

    def _found(self, g: OwnedGame) -> list[TAGame] | None:
        """The game's looked-up pages, one per TA id, unreleased placeholders (id < 1) left out; None (and a
        lookup) when they aren't looked up yet."""
        found = self.pages.get(lookup_key(g))
        if found is None:
            self.plan.lookups.append(g)
            return None
        out: list[TAGame] = []
        seen: set[int] = set()
        for p in found:
            if p.id > 0 and p.id not in seen:
                seen.add(p.id)
                out.append(p)
        return out

    def _land(self, g: OwnedGame, chosen: list[TAGame]) -> list[Action | Review | None]:
        """A page already in the collection is a fix (or nothing), any other an add."""
        if g.evidence == "legacy" and any(p.id in self.ea_play and p.id not in self.by_id for p in chosen):
            return [_review(g, "subscription", EA_REASON)]
        return [self._fix(g, self.by_id[p.id], p.slug) if p.id in self.by_id else self._add(g, p) for p in chosen]

    def _fix(self, g: OwnedGame, row: OwnedRow, slug: str = "") -> Action | Review | None:
        if row.ownership in OWNED_TA or g.acquisition == "free" or not _fixable(row.ownership):
            return None
        if _subscription(row.ownership) and g.evidence not in PAID:
            return _review(g, "subscription", f"no paid order on record: TA says {row.ownership}; Microsoft has an "
                                              f"active licence ({g.evidence}), which could be a subscription grant")
        media = () if row.media.strip() else (("media", DIGITAL),)
        return Action("fix", g, row.ta_id, slug, row.title, (("ownership", OWNED),) + media)

    def _add(self, g: OwnedGame, page: TAGame) -> Action:
        owner = GWG if g.acquisition == "free" and page.id in self.gwg else OWNED
        return Action("add", g, page.id, page.slug, page.title, (("ownership", owner), ("media", DIGITAL)))

    def _settle(self, outcomes: list[Action | Review | None]) -> None:
        """One game's outcomes: its review if it has one (and then no write), else its writes, else it's ok."""
        review = next((o for o in outcomes if isinstance(o, Review)), None)
        if review is not None:
            self.plan.review.append(review)
            return
        acts = [o for o in outcomes if o is not None]
        if not acts:
            self.plan.ok += 1
        for a in acts:  # one action per TA game: an add over a fix, Owned over Games with Gold
            if a.ta_id not in self.actions or _strength(a) > _strength(self.actions[a.ta_id]):
                self.actions[a.ta_id] = a


def plan_library(games: list[OwnedGame], ta_rows: list[OwnedRow], pages: dict[str, list[TAGame] | None],
                 overrides: dict[str, dict], gwg: set[int], ea_play: set[int]) -> LibraryPlan:
    """TA adds, fixes and hand checks for the owned games (pure).

    `pages[lookup_key(game)]` holds a game's looked-up candidate pages (`candidate_slugs`); a game without
    them (no key, or None) is not looked up yet and goes to `lookups`, an empty list means no TA page.
    `overrides` is the `library` section; `gwg` and `ea_play` are the TA game ids on TA's Games with Gold and
    EA Play lists."""
    games = list(games)
    p = _Planner(ta_rows, pages, gwg, ea_play, {g.key for g in games if g.source == "collections"})
    todo = [(g, ov) for g in games if (ov := override_for(g, overrides)) != SKIP]
    matches = iter(reconcile([g for g, ov in todo if not ov], ta_rows))
    for g, ov in todo:
        if ov:
            p.pinned(g, ov, next(pid for pid in g.pids if (overrides.get(pid) or {}).get("ta") == ov))
        else:
            p.matched(next(matches))
    p.plan.actions = list(p.actions.values())
    return p.plan


# ================================================================ the step

PACE = 4.0  # seconds before each TrueAchievements request the step makes
READ_RETRY = timedelta(hours=6)  # after a failed Microsoft or TA read, no new read for this long
# How long a game's looked-up pages are reused, in days: a base plus a stable per-product offset of 0..spread days
# (`lookup_lifetime`), so that results looked up together don't all expire together.
LIFETIMES = {"found": (7, 3),  # pages found for a product not seen at go-live (every product, in a dry run)
             "seen": (30, 14),  # pages found for a product seen at go-live (decision A)
             "no-page": (90, 30)}  # no TA page at any address
PAGES_KEEP = timedelta(days=180)  # a cached lookup older than this is dropped (its game is gone, or its key changed)
LISTS_FRESH = timedelta(days=7)  # TA's Games with Gold and EA Play lists are read at most weekly
MAX_WRITE_ERRORS = 3  # TA write errors without a successful write between them: the pass's writes stop
MAX_LOOKUP_ERRORS = 3  # failed page lookups without a successful one between them: the pass's lookups stop
GWG_LIST, EA_LIST = "games-with-gold", "ea-play"


@dataclass
class LibraryResult:
    dry_run: bool = True
    due: bool = False  # the day's pass ran, or went on, this run
    plan: LibraryPlan | None = None  # after this run's lookups; None when nothing was planned
    written: list[str] = field(default_factory=list)  # TA titles written this run (live)
    looked_up: int = 0  # games whose TA pages were looked up this run
    pending: int = 0  # games still waiting for a page lookup
    left: int = 0  # live: planned writes left for the next run
    failed: list[str] = field(default_factory=list)  # titles whose TA write failed this run (each an issue)
    lookup_failed: list[str] = field(default_factory=list)  # titles whose page lookup failed this run (each an issue)
    stopped: str | None = None  # why the pass ended early this run
    complete: bool = False  # the day's pass is done: state.library["last_pass"] is today


class _ReadFailed(Exception):
    """A Microsoft or TA read failed: an issue, nothing planned, and no new read for READ_RETRY."""


def _err(e: BaseException) -> str:
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _when(stamp) -> datetime | None:
    """An ISO timestamp from state (UTC when it has no offset); None when there is none or it can't be read."""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if stamp else None
    except ValueError:
        return None
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _within(stamp, now: datetime, span: timedelta) -> bool:
    """`stamp` (ISO text) is at most `span` before `now` (never in the future)."""
    when = _when(stamp)
    return when is not None and timedelta(0) <= now - when < span


def _unreachable(e: BaseException) -> bool:
    """Cloudflare or the network stood between the step and TA: a later run may get through."""
    return isinstance(e, (Unreachable, FlareSolverrError))


def _everywhere(e: BaseException) -> bool:
    """A lookup failure every TA request would meet: the network, Cloudflare, TA unavailable or throttling
    (`Unreachable`), FlareSolverr, or the TA login."""
    return _unreachable(e) or isinstance(e, AuthError)


def run_library_step(run, ta, http, state, overrides: dict[str, dict], now: datetime) -> LibraryResult:
    """One run of the library step. `run` is the wishlist run (`wsync.run._Run`): its `cfg`, `say`, `issue`,
    `scrub`, `sleep`, `past_deadline()`, `checkpoint()` (save the state now) and `library_dry`. `ta` is the
    TAClient, `http` the Microsoft transport, `overrides` the `library` section.

    Once a day, at the first run past `cfg.library_after` (UTC), a pass: sign in; read the licences, orders and
    catalog; TA's subscription lists (weekly) and the TA collection; plan; look up pages; plan again; write
    (live): unfinished writes first, then the plan's. A pass that doesn't finish (lookups or writes left, a stop, a
    failed write) goes on at the next run, with the TA collection its first run read and without the writes it has
    made. A failed Microsoft or TA read is an issue; the runs within 6 h of it make no new read and only warn. A
    Microsoft sign-in that needs a new token is not an issue: it isn't tried again before the next day's pass unless
    XBOX_REFRESH_TOKEN changes. A TA write or page-lookup error is an issue about that game only; Cloudflare or
    network trouble and the write deadline stop the lookups and writes quietly. Other exceptions propagate.
    """
    if now.tzinfo is None:
        raise ValueError("run_library_step needs a timezone-aware `now`")
    lib = state.library
    res = LibraryResult(dry_run=run.library_dry)
    utc = now.astimezone(timezone.utc)
    today = utc.date().isoformat()
    after = run.cfg.library_after
    if utc < utc.replace(hour=after.hour, minute=after.minute, second=0, microsecond=0):
        run.say(f"library: the daily pass starts after {after:%H:%M} UTC; not due yet")
        return res
    if lib.get("last_pass") == today:
        run.say(f"library: today's pass is complete; the next starts after {after:%H:%M} UTC tomorrow")
        return res
    res.due = True
    if run.past_deadline():
        res.stopped = "write deadline"
        run.say("library: pass not started, past the write deadline; next run", logging.WARNING)
        return res
    if _backing_off(run, lib, now, res):
        return res
    had_signin = "signin_failed" in lib  # one ping per failure episode: only when this run starts it
    seed = hashlib.sha256(run.cfg.xbox_refresh_token.encode()).hexdigest()  # never the value itself
    signin = lib.get("signin_failed") if isinstance(lib.get("signin_failed"), dict) else {}
    if signin.get("day") == today and signin.get("seed") == seed:
        res.stopped = "Microsoft sign-in needed"
        run.say("library: Microsoft sign-in needed (it failed earlier today); next try at tomorrow's pass, or at the "
                "next run once XBOX_REFRESH_TOKEN changes", logging.WARNING)
        library_report.signin_failed(run, lib, new=False)
        return res
    try:
        games = _owned(run, http, lib)
        gwg, ea_play = _lists(run, ta, lib, now)
        rows = _pass_rows(run, ta, lib, today)
    except SignInNeeded as e:
        lib["signin_failed"] = {**signin, "day": today, "seed": seed}
        res.stopped = "Microsoft sign-in needed"
        library_report.signin_failed(run, lib, new=not had_signin)
        run.say(f"library: {e}; next try at tomorrow's pass, or at the next run once XBOX_REFRESH_TOKEN changes",
                logging.WARNING)
        return res
    except _ReadFailed as e:
        text = run.scrub(str(e))
        lib["failed"] = {"at": now.isoformat(), "issue": text}
        res.stopped = "read failed"
        run.issue(text)
        return res
    lib.pop("failed", None)
    done = set(lib["pass"].get("done") or ())
    plan = _without(done, plan_library(games, rows, _cached_pages(lib, games, overrides, now), overrides, gwg,
                                       ea_play))
    _look_up(run, ta, lib, plan, overrides, now, res)
    plan = _without(done, plan_library(games, rows, _cached_pages(lib, games, overrides, now), overrides, gwg,
                                       ea_play))
    failed = set(lib["pass"].get("lookup_failed") or ())
    res.plan, res.pending = plan, sum(lookup_key(g) not in failed for g in plan.lookups)
    _log_plan(run, res, len(games), len(rows), plan)
    library_report.baseline(lib, plan, dry=res.dry_run)  # decision A: before any write, so a stop can't skip it
    if not res.dry_run:
        _Writer(run, ta, lib, overrides, now, res).write_phase(plan.actions, {r.ta_id: r for r in rows})
    elif lib.get("writing"):
        run.say(f"library: {len(lib['writing'])} unfinished TA writes wait for a live run")
    library_report.report_plan(run, res, lib, plan, rows)
    return _end(run, res, lib, today)


def _backing_off(run, lib: dict, now: datetime, res: LibraryResult) -> bool:
    """Within READ_RETRY of a failed read: no read, and only a warning, as the Deku collection step's back-off. The
    failing run was the issue; repeating it would fail every run of the back-off and trip the stale-sync alert."""
    failed = lib.get("failed") if isinstance(lib.get("failed"), dict) else {}
    if not _within(failed.get("at"), now, READ_RETRY):
        return False
    res.stopped = "read back-off"
    hours = (now - _when(failed["at"])).total_seconds() / 3600
    run.say(f"library: no new read within {READ_RETRY.total_seconds() / 3600:.0f} h of the failed one "
            f"({hours:.1f} h ago: {failed.get('issue') or 'a Microsoft or TrueAchievements read failed'})",
            logging.WARNING)
    return True


def _end(run, res: LibraryResult, lib: dict, today: str) -> LibraryResult:
    """The run's summary; the day's pass is complete when nothing stopped or failed and no lookup or write is left.
    Its stored TA collection read goes with it."""
    res.complete = (res.stopped is None and not res.failed and not res.lookup_failed and not res.pending
                    and not res.left)
    if res.complete:
        lib["last_pass"] = today
        lib.pop("pass", None)
    run.say(f"library: this run: looked up {res.looked_up}, written {len(res.written)}"
            + (f", {res.left} writes left" if res.left else "")
            + (f", {len(res.failed)} failed" if res.failed else "")
            + (f", {len(res.lookup_failed)} lookups failed" if res.lookup_failed else "")
            + (f", {res.pending} games waiting for a lookup" if res.pending else "")
            + (f"; stopped: {res.stopped}" if res.stopped else "")
            + ("; today's pass is complete" if res.complete else "; the pass goes on next run"))
    return res


def _owned(run, http, lib: dict) -> list[OwnedGame]:
    """Microsoft sign-in, licences, orders and the Store catalog -> the owned games. Every token, the XUID and the
    gamertag go to the scrubber the moment they exist; a working sign-in clears a recorded failed one. SignInNeeded passes
    through; any other failure is a _ReadFailed: nothing is planned from a partial read. Data that can't be
    classified is one too, named by its exception type only: the message could quote a price or an order."""
    try:
        s = open_session(http, TokenStore(run.cfg.xbox_token, run.cfg.xbox_refresh_token), run.scrub.add)
        run.scrub.add(s.xuid, s.gamertag)
        lib.pop("signin_failed", None)
        library_report.signin_ok(run, lib)  # the sign-in works: its task closes now, not after the reads
        items = fetch_collections(http, s)
        orders = fetch_orders(http, s)
        catalog = catalog_lookup(http, _product_ids(items, orders))
    except SignInNeeded:
        raise
    except XboxError as e:
        raise _ReadFailed(f"library step: Microsoft read failed, nothing planned or written: {_err(e)}; "
                          "retried after 6 h") from None
    try:
        games = owned_games(items, orders, catalog)
    except Exception as e:  # noqa: BLE001 - malformed licences or orders
        raise _ReadFailed(f"library step: Microsoft licences and orders could not be read ({type(e).__name__}), "
                          "nothing planned or written; retried after 6 h") from None
    run.say(f"library: Microsoft: {len(items)} licences, {len(orders)} orders, {len(catalog)} catalog products; "
            f"{len(games)} games owned")
    return games


def _product_ids(items: list, orders: list) -> list[str]:
    """The product ids of the licences and order lines, whatever else their entries hold."""
    out = [i["productId"] for i in items if isinstance(i, dict) and isinstance(i.get("productId"), str)]
    for o in orders:
        lines = o.get("orderLineItems") if isinstance(o, dict) else None
        out += [li["productId"] for li in (lines if isinstance(lines, list) else [])
                if isinstance(li, dict) and isinstance(li.get("productId"), str)]
    return out


def _lists(run, ta, lib: dict, now: datetime) -> tuple[set[int], set[int]]:
    """TA's Games with Gold and EA Play lists (TA game ids), read at most weekly into state.library["lists"]. A
    failed refresh keeps the copy in state (a warning; the next try waits READ_RETRY); with no copy, _ReadFailed."""
    lists = lib.setdefault("lists", {})
    out: list[set[int]] = []
    for slug in (GWG_LIST, EA_LIST):
        entry = lists.get(slug) if isinstance(lists.get(slug), dict) else {}
        ids = entry.get("ids") if isinstance(entry.get("ids"), list) else None
        if ids is not None and (_within(entry.get("fetched"), now, LISTS_FRESH)
                                or _within(entry.get("tried"), now, READ_RETRY)):
            out.append(set(ids))
            continue
        try:
            run.sleep(PACE)
            got = ta.property_list(slug)
        except TAError as e:
            if ids is None:
                raise _ReadFailed(f"library step: TrueAchievements {slug} list read failed, nothing planned or "
                                  f"written: {_err(e)}; retried after 6 h") from None
            lists[slug] = {**entry, "tried": now.isoformat()}
            run.say(f"library: TrueAchievements {slug} list refresh failed ({_err(e)}); using the copy read "
                    f"{str(entry.get('fetched') or '?')[:10]}, next try in 6 h", logging.WARNING)
            out.append(set(ids))
            continue
        lists[slug] = {"fetched": now.isoformat(), "ids": list(got)}
        run.say(f"library: read TrueAchievements' {slug} list ({len(got)} games)")
        out.append(set(got))
    return out[0], out[1]


def _pass_rows(run, ta, lib: dict, today: str) -> list[OwnedRow]:
    """The TA collection for today's pass. A day's pass reads it once, in its first run, never from the Deku
    collection step's cache, and keeps it in state.library["pass"] = {"day", "rows", "done"}, where "done" lists
    the TA ids the pass has since written, found already written or changed, or failed to write: never planned
    again in the pass. Its later runs reuse it; a new day's pass reads it again. Any failed read, an incomplete one
    included, is a _ReadFailed, so nothing is planned or written from it."""
    stored = lib.get("pass")
    if isinstance(stored, dict) and stored.get("day") == today:
        try:
            rows = [OwnedRow(**r) for r in stored["rows"]]
        except (KeyError, TypeError):
            rows = None  # unreadable: read again
        if rows is not None:
            run.say(f"library: the TrueAchievements collection as this pass read it ({len(rows)} titles)")
            return rows
    try:
        run.sleep(PACE)
        rows = ta.collection()
    except TAError as e:
        raise _ReadFailed(f"library step: TrueAchievements collection read failed, no library writes this run: "
                          f"{_err(e)}; retried after 6 h") from None
    lib["pass"] = {"day": today, "rows": [asdict(r) for r in rows], "done": []}
    run.say(f"library: read the TrueAchievements collection ({len(rows)} titles)")
    return rows


def _without(done: set[int], plan: LibraryPlan) -> LibraryPlan:
    """The plan without the writes the pass has made since it read the TA collection: a game whose every write is
    done counts as already right."""
    if done:
        kept = [a for a in plan.actions if a.ta_id not in done]
        plan.ok += len({a.game for a in plan.actions} - {a.game for a in kept})
        plan.actions = kept
    return plan


def _cached_pages(lib: dict, games: list[OwnedGame], overrides: dict[str, dict],
                  now: datetime) -> dict[str, list[TAGame]]:
    """The cached lookups that still hold for the games as they are now, by lookup key: within their lifetime
    (`lookup_lifetime`: "no-page" without a page, "seen" for a product seen at go-live, else "found") and made at
    the addresses the game has now, so a new or changed override is looked up again. Entries older than PAGES_KEEP
    are dropped."""
    cache = lib.setdefault("pages", {})
    for key in [k for k, e in cache.items()
                if not (isinstance(e, dict) and _within(e.get("checked"), now, PAGES_KEEP))]:
        del cache[key]
    seen = set(lib.get("seen") or ())
    out: dict[str, list[TAGame]] = {}
    for g in games:
        key = lookup_key(g)
        entry = cache.get(key)
        if not entry or entry.get("slugs") != candidate_slugs(g, override_for(g, overrides)):
            continue
        try:
            pages = [TAGame(**{**p, "platforms": tuple(p.get("platforms") or ())}) for p in entry.get("pages") or ()]
        except (TypeError, AttributeError):
            continue  # unreadable: looked up again
        kind = "no-page" if not pages else "seen" if seen & set(g.pids) else "found"
        if _within(entry.get("checked"), now, lookup_lifetime(key, kind)):
            out[key] = pages
    return out


def _queue(lib: dict, lookups: list[OwnedGame], overrides: dict[str, dict], skip) -> list[OwnedGame]:
    """The games to look up, in order: those never looked up at the addresses they have now first (in title order),
    then the expired re-lookups, the oldest first; none whose key is in `skip`."""
    cache = lib.get("pages") or {}

    def order(g: OwnedGame) -> tuple[int, float]:
        entry = cache.get(lookup_key(g))
        same = isinstance(entry, dict) and entry.get("slugs") == candidate_slugs(g, override_for(g, overrides))
        when = _when(entry.get("checked")) if same else None
        return (0, 0.0) if when is None else (1, when.timestamp())
    return sorted((g for g in lookups if lookup_key(g) not in skip), key=order)


def _look_up(run, ta, lib: dict, plan: LibraryPlan, overrides: dict[str, dict], now: datetime,
             res: LibraryResult) -> None:
    """The TA pages of the games the plan waits for, at most cfg.library_lookups games, every candidate address of
    each; each game's result goes into the cache. No page there (HTTP 404, or TA's page-not-found page) goes on to
    the next address.

    Failures: what every request would meet (`_everywhere`: the network, Cloudflare, TA unavailable or throttling,
    the login) stops the lookups quietly for this run, the game not looked up. Anything else (an HTTP error of one
    address, a page that doesn't parse, any other exception) fails that game's lookup for the pass: an issue naming
    it and its product ids, the game out of the pass's lookups (`pass["lookup_failed"]`, tried again in the next
    day's pass), and the next game goes on, until MAX_LOOKUP_ERRORS failures come without a successful lookup between
    them (`pass["lookup_errors"]`): then the pass looks nothing more up.

    Order and limits: games never looked up first, then the expired re-lookups, the oldest first (`_queue`); at
    most cfg.library_lookups games a run and cfg.library_lookups_per_day a UTC day across its runs (every lookup
    started counts, kept in the pass's "lookups")."""
    stored = lib["pass"]  # today's pass: read or made by _pass_rows before any lookup
    failed = stored.setdefault("lookup_failed", [])
    if stored.get("lookup_errors", 0) >= MAX_LOOKUP_ERRORS:
        _lookups_stopped(run, res)
        return
    cache = lib.setdefault("pages", {})
    queue = _queue(lib, plan.lookups, overrides, set(failed))
    per_day = max(run.cfg.library_lookups_per_day, 0)
    for g in queue[:max(run.cfg.library_lookups, 0)]:
        if stored.get("lookups", 0) >= per_day:
            waiting = len(queue) - res.looked_up - len(res.lookup_failed)
            run.say(f"library: today's lookup limit ({per_day} games) is reached; {waiting} games wait for the next "
                    "day's pass")
            return
        if run.past_deadline():
            res.stopped = "write deadline"
            run.say("library: lookups stopped at the write deadline; the rest next run", logging.WARNING)
            return
        stored["lookups"] = stored.get("lookups", 0) + 1
        found: list[TAGame] = []
        try:
            slugs = candidate_slugs(g, override_for(g, overrides))
            for slug in slugs:
                run.sleep(PACE)
                page = ta.game_page_or_none(slug)
                if page is not None:
                    found.append(page)
        except Exception as e:  # noqa: BLE001 - whatever stops one game's lookup is that game's, unless it's TA's
            if _everywhere(e):
                res.stopped = f"TrueAchievements lookup failed ({type(e).__name__})"
                run.say(f"library: looking up {g.title} failed ({_err(e)}); lookups stopped, the rest next run",
                        logging.WARNING)
                return
            _lookup_failed(run, res, stored, g, e)
            if stored["lookup_errors"] >= MAX_LOOKUP_ERRORS:
                _lookups_stopped(run, res)
                return
            continue
        stored["lookup_errors"] = 0
        cache[lookup_key(g)] = {"checked": now.isoformat(), "slugs": slugs, "pages": [asdict(p) for p in found]}
        res.looked_up += 1
        run.say(f"library: looked up {g.title}: "
                + ("; ".join(f"{p.title} [{p.platform or 'no platform'}]" for p in found) or "no TA page"))


def _lookup_failed(run, res: LibraryResult, stored: dict, g: OwnedGame, e: Exception) -> None:
    """One game's failed lookup: an issue naming it and its products, with the message of a TA error (the job's own
    text) or the exception type alone; the game waits for the next day's pass."""
    stored.setdefault("lookup_failed", []).append(lookup_key(g))
    stored["lookup_errors"] = stored.get("lookup_errors", 0) + 1
    res.lookup_failed.append(g.title)
    detail = _err(e) if isinstance(e, TAError) else type(e).__name__
    run.issue(f"library step: TrueAchievements page lookup failed for {g.title}: {detail}; tried again in the "
              f"next day's pass. To leave the game out, add a library override \"skip\" for product "
              f"{', '.join(g.pids)}")


def _lookups_stopped(run, res: LibraryResult) -> None:
    res.stopped = f"lookups stopped for today's pass ({MAX_LOOKUP_ERRORS} lookup errors in a row)"
    run.say(f"library: lookups stopped for today's pass after {MAX_LOOKUP_ERRORS} TrueAchievements lookup errors "
            "in a row; the next day's pass tries again", logging.WARNING)


def _log_plan(run, res: LibraryResult, owned: int, titles: int, plan: LibraryPlan) -> None:
    """Counts and titles only: never a product ID, order, price or token."""
    adds = sum(a.kind == "add" for a in plan.actions)
    verb = "would" if res.dry_run else "to"
    failed = len(plan.lookups) - res.pending  # games whose lookup failed in this pass: the next day's pass
    run.say(f"library: {'DRY RUN' if res.dry_run else 'APPLY'}; {owned} games owned on Microsoft, {titles} titles on "
            f"TrueAchievements; already right {plan.ok}; {verb} add {adds}; {verb} fix {len(plan.actions) - adds}; "
            f"check by hand {len(plan.review)}; looked up this run {res.looked_up}; waiting for a lookup "
            f"{res.pending}" + (f"; lookup failed today {failed}" if failed else ""))
    if res.dry_run:
        for a in plan.actions:
            run.say(f"library: would {a.kind} {a.title} ({', '.join(label for _, label in a.sets)})")
        for r in plan.review:
            run.say(f"library: check by hand ({r.kind}): {r.title}: {r.reason}")
    elif plan.review:
        kinds: dict[str, int] = {}
        for r in plan.review:
            kinds[r.kind] = kinds.get(r.kind, 0) + 1
        run.say("library: check by hand: " + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())))


def _reached(entry: dict) -> bool:
    """Part of the write reached TA: the add, or a field set."""
    return bool(entry.get("added") or entry.get("sets_done"))


class _Writer:
    """The write phase (live): unfinished writes first, then the plan's.

    - At most cfg.library_budget games a run, PACE seconds before each TA request, none started past the write
      deadline; Cloudflare or network trouble stops the writes quietly (what was started stays for the next run).
    - Write-ahead: a write goes into state.library["writing"] with what the plan saw of the entry ("expect"), saved
      before its first request; its progress ("added", "sets_done") is saved after each request; it leaves after
      its last. One left there (a crash, a stop, a partial failure) is completed at most once a pass, from where it
      got to. A `library` override that skips its game drops it, as does one that names another page while nothing
      of it reached TA.
    - Compare-and-set: a field already right isn't posted, and an entry changed on TA since the read (ChangedOnTA)
      is left as it is (a warning). A new add whose game TA already lists is never written either (a warning): the
      plan was made without that entry, so its ownership may be one only a paid order may change. An unfinished add
      that finds its game listed (its post reached TA before it was recorded) goes on to its field sets, which
      compare against TA's defaults for a new entry.
    - Any failure of one game's write but Cloudflare or network trouble is that game's write error: an issue naming
      the game and its product ids (for a `skip` override), and the exception type only unless it is a TA error.
      Other writes go on, until MAX_WRITE_ERRORS errors come without a successful write between them (counted in
      the pass's state, "errors"): then the pass writes nothing more before the next day's pass. A failed write
      that reached nothing on TA is dropped; a partial one stays for the next pass.
    - Every write made, failed or found already made goes into the pass's "done": the pass never plans it again.
    """

    def __init__(self, run, ta, lib: dict, overrides: dict[str, dict], now: datetime, res: LibraryResult):
        self.run, self.ta, self.lib, self.overrides, self.now, self.res = run, ta, lib, overrides, now, res
        self.today = now.astimezone(timezone.utc).date().isoformat()

    @property
    def writing(self) -> dict:
        return self.lib.setdefault("writing", {})

    def _pass(self) -> dict:
        """Today's pass state (the write phase only runs after it is read or made); a throwaway one otherwise."""
        stored = self.lib.get("pass")
        return stored if isinstance(stored, dict) and stored.get("day") == self.today else {}

    def _done_ids(self) -> list:
        return self._pass().setdefault("done", [])

    def _done(self, game_id: int) -> None:
        """The pass never plans this TA game again (its stored collection read predates the write)."""
        done = self._done_ids()
        if game_id not in done:
            done.append(game_id)

    def write_phase(self, actions: list[Action], rows: dict[int, OwnedRow]) -> None:
        if self._pass().get("errors", 0) >= MAX_WRITE_ERRORS:
            self.res.stopped = f"writes stopped for today's pass ({MAX_WRITE_ERRORS} write errors in a row)"
            self.res.left = len(actions) + len(self.writing)
            self.run.say(f"library: writes stopped for today's pass after {MAX_WRITE_ERRORS} TrueAchievements write "
                         "errors in a row; the next day's pass tries again", logging.WARNING)
            return
        todo = [(key, entry, False) for key, entry in self._leftovers()]
        todo += [(str(a.ta_id), self._entry(a, rows), True) for a in actions]
        for n, (key, entry, ahead) in enumerate(todo):
            if ahead and entry["ta_id"] in self._done_ids():
                continue  # an unfinished write of the same game was just completed
            if not self._write(key, entry, ahead=ahead):
                self.res.left = len(todo) - n
                return

    def _leftovers(self) -> list[tuple[str, dict]]:
        """The unfinished writes to complete this pass (each at most once a pass)."""
        out = []
        for key, entry in sorted(self.writing.items()):
            if not isinstance(entry, dict):
                self.writing.pop(key)
                self.run.say("library: dropped an unreadable unfinished TA write", logging.WARNING)
                continue
            if entry.get("tried") == self.today:
                continue  # it failed earlier in this pass: the next pass tries it again
            pids = entry.get("pids") or ()
            override = _override(pids, self.overrides)
            if override == SKIP:
                why = "a library override skips its game"
            elif override and override.lower() != str(entry.get("slug") or "").lower() and not _reached(entry):
                why = "its library override now names another page"
            else:
                out.append((key, entry))
                continue
            self.writing.pop(key)
            self.run.say(f"library: dropped the unfinished TA write of {entry.get('title')}: {why}")
        return out

    def _entry(self, a: Action, rows: dict[int, OwnedRow]) -> dict:
        """The write-ahead of an action, with what the plan saw of each field it sets: TA's defaults for a new
        entry (Owned, no media), else the entry as the collection read showed it."""
        if a.kind == "add":
            expect = {"ownership": [OWNED], "media": [""]}
        else:
            row = rows[a.ta_id]  # a fix is always of an entry the read listed
            expect = {"ownership": [row.ownership], "media": [row.media]}
        return {"kind": a.kind, "slug": a.slug, "ta_id": a.ta_id, "title": a.title, "sets": [list(s) for s in a.sets],
                "expect": expect, "pids": list(a.game.pids), "since": self.now.isoformat()}

    def _save(self, entry: dict, *, before: bool = False) -> bool:
        try:
            self.run.checkpoint()
            return True
        except Exception as e:  # noqa: BLE001
            self.res.stopped = "state not saved"
            when = "before writing" if before else "while writing"
            self.run.issue(f"library step: state not saved {when} {entry['title']} on TrueAchievements "
                           f"({type(e).__name__}); " + ("not written, " if before else "") + "library writes stopped")
            return False

    def _write(self, key: str, entry: dict, *, ahead: bool) -> bool:
        """One game's write; False when the run's writes stop. Terminated (SIGTERM) passes through."""
        run, res = self.run, self.res
        if len(res.written) >= run.cfg.library_budget:
            res.stopped = "write budget"
            return False
        if run.past_deadline():
            res.stopped = "write deadline"
            run.say("library: writes stopped at the write deadline; the rest next run", logging.WARNING)
            return False
        if ahead:
            self.writing[key] = entry
            if not self._save(entry, before=True):
                self.writing.pop(key, None)
                return False
        else:
            run.say(f"library: completing the unfinished TA write of {entry.get('title')}")
        game_id = entry.get("ta_id")
        try:
            game_id = int(game_id)
            expect = entry.get("expect") or {}
            if entry["kind"] == "add" and not entry.get("added"):
                run.sleep(PACE)
                # a fresh add refuses a game TA already lists (AlreadyListed); an unfinished one may have reached TA
                # before it was recorded (a stop during its post): found listed, it goes on to its field sets
                self.ta.collection_add(entry["slug"], game_id=game_id, new=ahead)
                entry["added"] = True
                if not self._save(entry):
                    return False
            for field_name, label in entry["sets"]:
                if field_name in (entry.get("sets_done") or ()):
                    continue
                run.sleep(PACE)
                seen = expect.get(field_name)
                self.ta.collection_set(game_id, field_name, label, expect=set(seen) if seen is not None else None)
                entry.setdefault("sets_done", []).append(field_name)
                if not self._save(entry):
                    return False
        except AlreadyListed:
            self._left_alone(key, game_id, f"TrueAchievements already lists {entry['title']}, though the collection "
                                           "read didn't; left as it is until the next day's pass")
            return True
        except ChangedOnTA:
            self._left_alone(key, game_id, f"{entry['title']} changed on TrueAchievements since the read; left as is")
            return True
        except Exception as e:  # noqa: BLE001 - whatever stops one game's write is that game's
            if _unreachable(e):
                res.stopped = f"TrueAchievements unreachable ({type(e).__name__})"
                run.say(f"library: writing {entry['title']} stopped ({_err(e)}); retried next run", logging.WARNING)
                return False
            return self._failed(key, entry, game_id, e)
        self.writing.pop(key, None)
        self._done(game_id)
        self._pass()["errors"] = 0
        res.written.append(entry["title"])
        run.say(f"library: {'added' if entry['kind'] == 'add' else 'fixed'} {entry['title']} "
                f"({', '.join(label for _, label in entry['sets'])})")
        return True

    def _left_alone(self, key: str, game_id: int, why: str) -> None:
        self.writing.pop(key, None)
        self._done(game_id)
        self.run.say(f"library: {why}", logging.WARNING)

    def _failed(self, key: str, entry: dict, game_id, e: Exception) -> bool:
        """A write error: an issue naming the game and its products, with the message of a TA error (the job's own
        text) or the exception type alone. Other writes go on, unless it is the MAX_WRITE_ERRORS-th error since the
        last successful write: then nothing more is written in this pass."""
        run, res = self.run, self.res
        self._done(game_id)
        res.failed.append(str(entry.get("title")))
        if _reached(entry):
            entry["tried"] = self.today
            what = "partly written: the next day's pass tries the rest again"
        else:
            self.writing.pop(key, None)
            what = "nothing written: the next day's pass plans it again"
        products = ", ".join(str(p) for p in entry.get("pids") or ()) or "unknown"
        detail = _err(e) if isinstance(e, TAError) else type(e).__name__
        run.issue(f"library step: TrueAchievements write failed for {entry.get('title')}: {detail}; {what}. To "
                  f"leave the game out, add a library override \"skip\" for product {products}")
        stored = self._pass()
        stored["errors"] = stored.get("errors", 0) + 1
        if stored["errors"] < MAX_WRITE_ERRORS:
            return True
        res.stopped = f"writes stopped for today's pass ({MAX_WRITE_ERRORS} write errors in a row)"
        run.say(f"library: writes stopped for today's pass after {MAX_WRITE_ERRORS} TrueAchievements write errors "
                "in a row; the next day's pass tries again", logging.WARNING)
        return False

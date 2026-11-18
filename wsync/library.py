"""Library step: keep the TrueAchievements (TA) Game Collection in line with what Microsoft says the owner owns.

Two parts: the plan (pure, no I/O, below) and `run_library_step` (the daily pass `wsync.run` calls between the
wishlist and Deku collection steps: Microsoft and TA reads, paced page lookups, write-ahead TA writes).

The plan is ported from the games repo's scripts/xbox-library.py (`reconcile`) and scripts/ta-library-fix.py
(`ta_slug`, `slug_candidates`, `accept_page`, `make_plan`):

- `reconcile`: each owned game against the TA collection: ok (in TA as Owned or Games with Gold, or a free
  game in TA at all), wrong (in TA with another status) or missing (with a possible-match hint);
- `plan_library`: the TA writes and the items to check by hand. A wrong ownership becomes Owned (media Digital
  when TA has none); a subscription status only with a paid order or a claim. A missing game's own TA page is
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

Never planned: a removal, a change to an Owned or Games with Gold entry, any ownership but Owned on a fix, or
a media change on an entry that has media.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .errors import TAError
from .flaresolverr import FlareSolverrError
from .overrides import SKIP
from .owned import (BARE_PLATFORM, PLATFORM_SUFFIX, PLATFORM_TAG, TIER_PLAIN, OwnedGame, game_key, hint, match_keys,
                    owned_games, same_game)
from .ta import ONE, SERIES, AlreadyListed, TAGame, Unreachable
from .ta_coll import OwnedRow
from .xbox import SignInNeeded, TokenStore, XboxError, catalog_lookup, fetch_collections, fetch_orders, open_session

OWNED, GWG, DIGITAL = "Owned", "Games with Gold", "Digital"
OWNED_TA = frozenset({OWNED, GWG})
SUBSCRIPTIONS = frozenset({"Xbox Game Pass (deprecated)", "Xbox Game Pass Ultimate", "Xbox Game Pass Premium",
                           "Xbox Game Pass Essential", "PC Game Pass", "EA Play", "Ubisoft+"})
PAID = frozenset({"paid", "claim"})  # the evidence that may turn a subscription status into Owned
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
    slugs = [o["ta"] for pid in game.pids if (o := overrides.get(pid)) and o.get("ta")]
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
        if row.ownership in OWNED_TA or g.acquisition == "free":
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
PAGES_FRESH = timedelta(days=7)  # a game's looked-up pages are reused this long,
PAGES_FRESH_LONG = timedelta(days=30)  # or this long for a game with no page or a seen product
LISTS_FRESH = timedelta(days=7)  # TA's Games with Gold and EA Play lists are read at most weekly
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
    stopped: str | None = None  # why the pass ended early this run
    complete: bool = False  # the day's pass is done: state.library["last_pass"] is today


class _ReadFailed(Exception):
    """A Microsoft or TA read failed: an issue, nothing planned, and no new read for READ_RETRY."""


def _err(e: BaseException) -> str:
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _within(stamp, now: datetime, span: timedelta) -> bool:
    """`stamp` (ISO text) is at most `span` before `now` (never in the future)."""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if stamp else None
    except ValueError:
        return False
    if when is None:
        return False
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return timedelta(0) <= now - when < span


def _unreachable(e: BaseException) -> bool:
    """Cloudflare or the network stood between the step and TA: a later run may get through."""
    return isinstance(e, (Unreachable, FlareSolverrError))


def run_library_step(run, ta, http, state, overrides: dict[str, dict], now: datetime) -> LibraryResult:
    """One run of the library step. `run` is the wishlist run (`wsync.run._Run`): its `cfg`, `say`, `issue`,
    `scrub`, `sleep`, `past_deadline()`, `checkpoint()` (save the state now) and `library_dry`. `ta` is the
    TAClient, `http` the Microsoft transport, `overrides` the `library` section.

    Once a day, at the first run past `cfg.library_after` (UTC), a pass: sign in; read the licences, orders and
    catalog; TA's subscription lists (weekly) and a fresh TA collection; plan; look up pages; plan again; write
    (live). A pass that doesn't finish (lookups or writes left, a stop) goes on at the next run. A failed
    Microsoft or TA read is an issue, repeated without a new read for 6 h; a Microsoft sign-in that needs a new
    token stops the step quietly for this run. TA write errors are issues; Cloudflare or network trouble and the
    write deadline stop the step quietly. Exceptions other than these propagate.
    """
    if now.tzinfo is None:
        raise ValueError("run_library_step needs a timezone-aware `now`")
    lib = state.library
    res = LibraryResult(dry_run=run.library_dry)
    utc = now.astimezone(timezone.utc)
    after = run.cfg.library_after
    if utc < utc.replace(hour=after.hour, minute=after.minute, second=0, microsecond=0):
        run.say(f"library: the daily pass starts after {after:%H:%M} UTC; not due yet")
        return res
    if lib.get("last_pass") == utc.date().isoformat():
        run.say(f"library: today's pass is complete; the next starts after {after:%H:%M} UTC tomorrow")
        return res
    res.due = True
    if run.past_deadline():
        res.stopped = "write deadline"
        run.say("library: pass not started, past the write deadline; next run", logging.WARNING)
        return res
    if _backing_off(run, lib, now, res):
        return res
    writer = _Writer(run, ta, lib, overrides, now, res)
    if lib.get("writing"):
        if res.dry_run:
            run.say(f"library: {len(lib['writing'])} unfinished TA writes wait for a live run")
        elif not writer.leftovers():
            return _end(run, res, lib, utc)
    try:
        games = _owned(run, http)
        gwg, ea_play = _lists(run, ta, lib, now)
        rows = _ta_rows(run, ta)
    except SignInNeeded as e:
        res.stopped = "Microsoft sign-in needed"
        run.say(f"library: {e}; step stopped for this run", logging.WARNING)
        return res
    except _ReadFailed as e:
        text = run.scrub(str(e))
        lib["failed"] = {"at": now.isoformat(), "issue": text}
        res.stopped = "read failed"
        run.issue(text)
        return res
    lib.pop("failed", None)
    plan = plan_library(games, rows, _cached_pages(lib, games, overrides, now), overrides, gwg, ea_play)
    _look_up(run, ta, lib, plan, overrides, now, res)
    plan = plan_library(games, rows, _cached_pages(lib, games, overrides, now), overrides, gwg, ea_play)
    res.plan, res.pending = plan, len(plan.lookups)
    _log_plan(run, res, len(games), len(rows), plan)
    if not res.dry_run:
        writer.write_all(plan.actions)
    return _end(run, res, lib, utc)


def _backing_off(run, lib: dict, now: datetime, res: LibraryResult) -> bool:
    """Within READ_RETRY of a failed read: its issue again, without a read (the failure ping is deduplicated)."""
    failed = lib.get("failed") if isinstance(lib.get("failed"), dict) else {}
    if not _within(failed.get("at"), now, READ_RETRY):
        return False
    res.stopped = "read back-off"
    run.say(f"library: no new read within {READ_RETRY.total_seconds() / 3600:.0f} h of the failed one",
            logging.WARNING)
    run.issue(failed.get("issue") or "library step: a Microsoft or TrueAchievements read failed; retried after 6 h")
    return True


def _end(run, res: LibraryResult, lib: dict, utc: datetime) -> LibraryResult:
    """The run's summary; the day's pass is complete when nothing stopped it and no lookup or write is left."""
    res.complete = res.stopped is None and not res.pending and not res.left
    if res.complete:
        lib["last_pass"] = utc.date().isoformat()
    run.say(f"library: this run: looked up {res.looked_up}, written {len(res.written)}"
            + (f", {res.left} writes left" if res.left else "")
            + (f", {res.pending} games waiting for a lookup" if res.pending else "")
            + (f"; stopped: {res.stopped}" if res.stopped else "")
            + ("; today's pass is complete" if res.complete else "; the pass goes on next run"))
    return res


def _owned(run, http) -> list[OwnedGame]:
    """Microsoft sign-in, licences, orders and the Store catalog -> the owned games. Every token and the XUID go to
    the scrubber the moment they exist. SignInNeeded passes through; any other failure is a _ReadFailed: nothing
    is planned from a partial read."""
    try:
        s = open_session(http, TokenStore(run.cfg.xbox_token, run.cfg.xbox_refresh_token), run.scrub.add)
        run.scrub.add(s.xuid)
        items = fetch_collections(http, s)
        orders = fetch_orders(http, s)
        pids = [i.get("productId") for i in items if i.get("productId")]
        pids += [li.get("productId") for o in orders for li in o.get("orderLineItems") or [] if li.get("productId")]
        catalog = catalog_lookup(http, pids)
    except SignInNeeded:
        raise
    except XboxError as e:
        raise _ReadFailed(f"library step: Microsoft read failed, nothing planned or written: {_err(e)}; "
                          "retried after 6 h") from None
    games = owned_games(items, orders, catalog)
    run.say(f"library: Microsoft: {len(items)} licences, {len(orders)} orders, {len(catalog)} catalog products; "
            f"{len(games)} games owned")
    return games


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


def _ta_rows(run, ta) -> list[OwnedRow]:
    """A fresh read of the TA collection (never the Deku collection step's cache): any failure, an incomplete read
    included, is a _ReadFailed, so nothing is planned or written from it."""
    try:
        run.sleep(PACE)
        rows = ta.collection()
    except TAError as e:
        raise _ReadFailed(f"library step: TrueAchievements collection read failed, no library writes this run: "
                          f"{_err(e)}; retried after 6 h") from None
    run.say(f"library: read the TrueAchievements collection ({len(rows)} titles)")
    return rows


def _cached_pages(lib: dict, games: list[OwnedGame], overrides: dict[str, dict],
                  now: datetime) -> dict[str, list[TAGame]]:
    """The cached lookups that still hold for the games as they are now, by lookup key: fresh (PAGES_FRESH, or
    PAGES_FRESH_LONG with no page or a seen product) and made at the addresses the game has now, so a new or
    changed override is looked up again. Entries too old to be used again are dropped."""
    cache = lib.setdefault("pages", {})
    for key in [k for k, e in cache.items()
                if not (isinstance(e, dict) and _within(e.get("checked"), now, PAGES_FRESH_LONG))]:
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
        if _within(entry.get("checked"), now, PAGES_FRESH_LONG if not pages or seen & set(g.pids) else PAGES_FRESH):
            out[key] = pages
    return out


def _look_up(run, ta, lib: dict, plan: LibraryPlan, overrides: dict[str, dict], now: datetime,
             res: LibraryResult) -> None:
    """The TA pages of the games the plan waits for, at most cfg.library_lookups games, every candidate address of
    each. No page there (HTTP 404, or not a game page) goes on to the next address; any other failure stops the
    lookups quietly for this run, the game not looked up. Each game's result goes into the cache."""
    cache = lib.setdefault("pages", {})
    for g in plan.lookups[:max(run.cfg.library_lookups, 0)]:
        if run.past_deadline():
            res.stopped = "write deadline"
            run.say("library: lookups stopped at the write deadline; the rest next run", logging.WARNING)
            return
        slugs = candidate_slugs(g, override_for(g, overrides))
        found: list[TAGame] = []
        try:
            for slug in slugs:
                run.sleep(PACE)
                page = ta.game_page_or_none(slug)
                if page is not None:
                    found.append(page)
        except TAError as e:
            res.stopped = f"TrueAchievements lookup failed ({type(e).__name__})"
            run.say(f"library: looking up {g.title} failed ({_err(e)}); lookups stopped, the rest next run",
                    logging.WARNING)
            return
        cache[lookup_key(g)] = {"checked": now.isoformat(), "slugs": slugs, "pages": [asdict(p) for p in found]}
        res.looked_up += 1
        run.say(f"library: looked up {g.title}: "
                + ("; ".join(f"{p.title} [{p.platform or 'no platform'}]" for p in found) or "no TA page"))


def _log_plan(run, res: LibraryResult, owned: int, titles: int, plan: LibraryPlan) -> None:
    """Counts and titles only: never a product ID, order, price or token."""
    adds = sum(a.kind == "add" for a in plan.actions)
    verb = "would" if res.dry_run else "to"
    run.say(f"library: {'DRY RUN' if res.dry_run else 'APPLY'}; {owned} games owned on Microsoft, {titles} titles on "
            f"TrueAchievements; already right {plan.ok}; {verb} add {adds}; {verb} fix {len(plan.actions) - adds}; "
            f"check by hand {len(plan.review)}; looked up this run {res.looked_up}; waiting for a lookup "
            f"{len(plan.lookups)}")
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


class _Writer:
    """Live TA writes: at most cfg.library_budget games a run, PACE seconds before each request, none started past
    the write deadline. Write-ahead: a write goes into state.library["writing"], saved, before its first request,
    and leaves it after its last; one a crash left there is completed first on the next run (adding a game the
    collection already lists posts nothing). A planned add whose game TA already lists is never written: the plan
    was made without that entry, so its ownership may be one only a paid order may change."""

    def __init__(self, run, ta, lib: dict, overrides: dict[str, dict], now: datetime, res: LibraryResult):
        self.run, self.ta, self.lib, self.overrides, self.now, self.res = run, ta, lib, overrides, now, res

    @property
    def writing(self) -> dict:
        return self.lib.setdefault("writing", {})

    def leftovers(self) -> bool:
        """Complete the writes an earlier run left unfinished; False when one couldn't be (the step stops). A game a
        `library` override now skips is dropped instead."""
        for key, entry in sorted(self.writing.items()):
            if any((self.overrides.get(p) or {}).get("ta") == SKIP for p in entry.get("pids") or ()):
                self.writing.pop(key)
                self.run.say(f"library: dropped the unfinished TA write of {entry.get('title')}: "
                             "a library override skips it")
                continue
            self.run.say(f"library: completing the unfinished TA write of {entry.get('title')}")
            if not self._write(key, entry):
                return False
        return True

    def write_all(self, actions: list[Action]) -> None:
        for n, a in enumerate(actions):
            entry = {"kind": a.kind, "slug": a.slug, "ta_id": a.ta_id, "title": a.title,
                     "sets": [list(s) for s in a.sets], "pids": list(a.game.pids), "since": self.now.isoformat()}
            if not self._write(str(a.ta_id), entry, ahead=True):
                self.res.left = len(actions) - n
                return

    def _write(self, key: str, entry: dict, *, ahead: bool = False) -> bool:
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
            try:
                run.checkpoint()
            except Exception as e:  # noqa: BLE001
                self.writing.pop(key, None)
                res.stopped = "state not saved"
                run.issue(f"library step: state not saved before writing {entry['title']} on TrueAchievements "
                          f"({type(e).__name__}); not written, library writes stopped")
                return False
        try:
            game_id = int(entry["ta_id"])
            if entry["kind"] == "add":
                run.sleep(PACE)
                self.ta.collection_add(entry["slug"], game_id=game_id, new=ahead)
            for field_name, label in entry["sets"]:
                run.sleep(PACE)
                self.ta.collection_set(game_id, field_name, label)
        except AlreadyListed:
            self.writing.pop(key, None)  # nothing written: the next pass plans it from a fresh collection read
            res.stopped = "TrueAchievements collection read out of date"
            run.issue(f"library step: TrueAchievements already has {entry['title']} in the collection, though the "
                      "collection read didn't list it: left as it is, library writes stopped")
            return False
        except TAError as e:
            if _unreachable(e):
                res.stopped = f"TrueAchievements unreachable ({type(e).__name__})"
                run.say(f"library: writing {entry['title']} stopped ({_err(e)}); retried next run", logging.WARNING)
            else:
                res.stopped = "TrueAchievements write failed"
                run.issue(f"library step: TrueAchievements write failed for {entry['title']}: {_err(e)}; library "
                          "writes stopped, retried next run (a library override \"skip\" on its product drops it)")
            return False
        self.writing.pop(key, None)
        res.written.append(entry["title"])
        run.say(f"library: {'added' if entry['kind'] == 'add' else 'fixed'} {entry['title']} "
                f"({', '.join(label for _, label in entry['sets'])})")
        return True

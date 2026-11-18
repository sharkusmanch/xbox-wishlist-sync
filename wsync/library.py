"""Library step: keep the TrueAchievements (TA) Game Collection in line with what Microsoft says the owner owns.

This part is the plan: pure, no I/O. Ported from the games repo's scripts/xbox-library.py (`reconcile`) and
scripts/ta-library-fix.py (`ta_slug`, `slug_candidates`, `accept_page`, `make_plan`):

- `reconcile`: each owned game against the TA collection: ok (in TA as Owned or Games with Gold, or a free
  game in TA at all), wrong (in TA with another status) or missing (with a possible-match hint);
- `plan_library`: the TA writes and the items to check by hand. A wrong ownership becomes Owned (media Digital
  when TA has none); a subscription status only with a paid order or a claim. A missing game's own TA page is
  added as Owned (Games with Gold for a free claim on TA's Games with Gold list), media Digital. A possible
  match, an uncertain version or ownership, or no page: a Review, never a write. The step looks up a missing
  game's pages (`candidate_slugs`, keyed by `lookup_key`); a game not looked up yet is a lookup.

Generations: the TA page's own Platform field decides; the Store catalog's console generation only chooses
among the pages that fit.
- A Collections licence never matches an Xbox 360 / original Xbox page or entry. It takes the Xbox Series X|S
  page for ConsoleGen9, the Xbox One page for ConsoleGen8, both for both (one page listing both is one add);
  a PC-only licence takes the Windows page.
- An orders-only game takes the Xbox 360 / Xbox page or entry when only those fit, the modern ones (as above)
  when only those fit, and is a review when both kinds do.
- A console licence never fixes TA's separate "(Windows)" entry (its console page is added instead); a PC-only
  licence fixes only a Windows entry.
- A `library` override slug is the game's page, without the title and generation checks; "skip" drops it.

Never planned: a removal, a change to an Owned or Games with Gold entry, any ownership but Owned on a fix, or
a media change on an entry that has media.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .overrides import SKIP
from .owned import OwnedGame, game_key, hint, match_keys, same_game
from .ta import ONE, SERIES, TAGame
from .ta_coll import OwnedRow

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
GENS = (("ConsoleGen8", ONE), ("ConsoleGen9", SERIES))  # Store catalog generation -> its TA page's platform
MAX_CANDIDATES = 6
VARIANTS = {"collections": ("Xbox-One", "Xbox-Series-XS"), "orders": ("Xbox-360", "Xbox-One")}
EA_REASON = "pre-2016 licence for an EA Play game: could be an EA Access vault licence rather than a purchase"
_X360_TITLE = re.compile(r"\(Xbox 360\)\s*$", re.I)
_X360_SLUG = re.compile(r"-Xbox-360$", re.I)
_WINDOWS_TITLE = re.compile(r"\(Windows\)\s*$", re.I)
_SEP = re.compile(r"\s+[-–—]\s+|\s*:\s*")
_EDITION_END = re.compile(r"(?i)\b(?:edition|bundle)\s*$")


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
    kind: str  # "possible-match" | "versions" | "subscription" | "no-page" | "generation"
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


def candidate_slugs(game: OwnedGame, override: str | None = None) -> list[str]:
    """The TA pages to look up for a game, at most MAX_CANDIDATES: an override's page alone; otherwise the
    guessed addresses, then platform variants of the one that names the game (the base title for a purchase
    tier, the full title for a version TA lists as its own game): -Xbox-One and -Xbox-Series-XS for a
    Collections licence (and -Windows when it's PC-only), -Xbox-360 and -Xbox-One for an orders-only game."""
    if override:
        return [] if override == SKIP else [override]
    t, b = _trim(game.title)
    out = slug_candidates(game.title)
    root = ta_slug(b) if b != t and ta_slug(b) and game_key(b) == game_key(t) else ta_slug(t)
    variants = VARIANTS[game.source] + (("Windows",) if game.source == "collections" and game.pc_only else ())
    for v in variants if root else ():
        if f"{root}-{v}" not in out:
            out.append(f"{root}-{v}")
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


def _row_last_gen(row: OwnedRow) -> bool:
    return row.platform in ROW_LAST_GEN or bool(_X360_TITLE.search(row.title))


def _row_fits(game: OwnedGame, row: OwnedRow) -> bool:
    """The TA entry can be this licence's: an Xbox entry of a generation it can be (a Collections licence is
    never an Xbox 360 / original Xbox game), and TA's separate Windows entry only for a PC-only licence."""
    if _row_last_gen(row):
        return game.source == "orders" and not game.pc_only
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


def _choose(game: OwnedGame, accepted: list[TAGame]) -> list[TAGame] | Review:
    """The pages a missing game takes, by generation; a Review when none fits or the choice is unclear."""
    last = [p for p in accepted if _last_gen(p)]
    if game.source == "orders" and last:
        if any(_modern(p) for p in accepted):
            return _review(game, "versions", "TA has Xbox 360 / Xbox and newer pages: " + _pages(accepted))
        if len(last) > 1 and not game.pc_only:
            return _review(game, "versions", "several TA game pages: " + _pages(last))
        if not game.pc_only:  # a PC-only licence is never an Xbox 360 / Xbox game
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
                 ea_play: set[int]):
        self.by_id = {r.ta_id: r for r in ta_rows}
        self.pages, self.gwg, self.ea_play = pages, gwg, ea_play
        self.plan = LibraryPlan()
        self.actions: dict[int, Action] = {}  # TA game id -> its one action

    def matched(self, m: Match) -> None:
        g = m.game
        if m.status == "ok":
            self.plan.ok += 1
        elif m.status == "wrong":
            old = [_row_last_gen(r) for r in m.rows]
            if g.source == "orders" and any(old) and not all(old):
                self._settle([_review(g, "versions", "TA has Xbox 360 / Xbox and newer entries: " + _rows(m.rows))])
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
                chosen = _choose(g, accepted)
                self._settle([chosen] if isinstance(chosen, Review) else self._land(g, chosen))

    def pinned(self, g: OwnedGame, slug: str) -> None:
        found = self._found(g)
        if found is None:
            return
        if not found:
            self._settle([_review(g, "no-page", f"no TA page at the library override's address {slug}")])
            return
        page = next((p for p in found if p.slug.lower() == slug.lower()), None)
        if page is None:  # pages looked up before the override: look up the override's page
            self.plan.lookups.append(g)
        else:
            self._settle(self._land(g, [page]))

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
    p = _Planner(ta_rows, pages, gwg, ea_play)
    todo = [(g, ov) for g in games if (ov := override_for(g, overrides)) != SKIP]
    matches = iter(reconcile([g for g, ov in todo if not ov], ta_rows))
    for g, ov in todo:
        if ov:
            p.pinned(g, ov)
        else:
            p.matched(next(matches))
    p.plan.actions = list(p.actions.values())
    return p.plan

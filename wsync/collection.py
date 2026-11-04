"""Deku collection step: add the Xbox games TrueAchievements says are Owned to the Deku collection.

Two parts:
  - `plan_collection` (pure, no I/O): from the owned rows, the Deku collection as it stands and the
    per-game cache, which rows still need a Deku lookup this run;
  - `run_collection_step` (the orchestration `wsync.run` calls after the wishlist step): reads both
    collections, plans, looks each game up on Deku (paced, budgeted), adds it (live) or lists it
    (dry run), caches every outcome and reports.

The step is add-only. It never names an existing Deku entry for change: a title that only has
Switch / PlayStation entries still gets a lookup, and the step adds a separate Xbox entry beside
them. The item page read just before the add is the authority: an Xbox entry, or one with no
platform set, means the game is already in the collection.

Per-game cache (`State.collection`, keyed by TA game id as text):
  matched  the Deku item already has an Xbox / unset-platform entry;
  added    added by this step (live);
  found    dry run: a confident match that a live run would add (re-read and added by the next
           live run without a new search; dry runs leave it alone for 7 days);
  none     no confident match / no Xbox listing: retried after 7 days;
  adding   written (and saved) just before an add; replaced by `added` once it is read back;
  partial  the add failed after the entry was created (DekuPartialOwn): a hand check is needed.
An entry made through a `collection` override also stores that slug as `override`: an unchanged
override cools for 7 days like any other game.
These row statuses only plan and report. The safety guard is keyed by Deku item:
`State.collection_unconfirmed` lists every item slug (after Deku's redirects) whose add was started
and not confirmed. It is written and saved before the create request and cleared only once the
item page, or the public share page read at the start of the step, shows an Xbox or unset entry.
The step never calls own() on a listed item, whichever TA row, override, redirect or cache entry
leads to it: it re-reads the page and asks for a hand check.
A matched / added / found game whose slug is not on the public share page (hidden, or deleted by
hand) cools for 7 days like `none` (`found` only in dry runs: in a live run it is an add to make).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone

from .deku import DekuError, DekuItem, DekuNetworkError, DekuPartialOwn, DekuRateLimited, OwnedEntry
from .errors import TAError
from .match import base_title, norm, pick, strip_edition_noise
from .overrides import SKIP
from .ta_coll import OwnedRow

# TA platform slug -> Deku platform key. Every other TA platform is skipped without a Deku request.
# TA's platform is the console the game was played on (an Xbox One game played on a Series X shows
# Series X|S), so it is a preference: see `choose_platform`.
TARGET = {"xbox-series-x-s": "xbox_series", "xbox-one": "xbox_one"}
_XBOX = frozenset(TARGET.values())
_OTHER = {"xbox_series": "xbox_one", "xbox_one": "xbox_series"}
COOLDOWN = timedelta(days=7)
DEFAULT_BUDGET = 25
PACE = 4.0  # seconds between Deku requests in this step
TA_MAX_AGE = timedelta(hours=24)  # the TA collection is read at most once a day
TA_RETRY = timedelta(hours=6)  # after a failed TA collection read, the next try waits this long
TA_READ_MARGIN = 10 * 60.0  # seconds: no fresh TA collection read (nor the step) closer to the write deadline
MATCHED, ADDED, FOUND, NONE = "matched", "added", "found", "none"
# Row-level write-ahead for an add: ADDING is stored (and saved) before `own()`; PARTIAL after
# DekuPartialOwn. The planner sends such a game to a read-only re-check of its item first; the
# item-keyed `State.collection_unconfirmed` is what keeps own() away from the item.
ADDING, PARTIAL = "adding", "partial"
UNSETTLED = frozenset({ADDING, PARTIAL})


def choose_platform(ta_platform: str, offered) -> str | None:
    """The Deku platform for a new entry: TA's, when the Deku item lists it, else the other Xbox
    platform the item lists; None when the item lists no Xbox platform."""
    want = TARGET[ta_platform]
    if want in offered:
        return want
    return _OTHER[want] if _OTHER[want] in offered else None


def deku_format(media: str) -> str | None:
    """TA media -> Deku format; None (leave unset) for blank or anything unrecognised."""
    m = (media or "").strip().lower()
    if m == "disc":
        return "physical"
    if m in {"digital", "disc-to-digital", "disc & digital"}:
        return "digital"
    return None


@dataclass
class CollectionPlan:
    already: list[OwnedRow] = field(default_factory=list)
    skipped_platform: list[OwnedRow] = field(default_factory=list)
    skipped_override: list[OwnedRow] = field(default_factory=list)
    cooling: list[OwnedRow] = field(default_factory=list)  # unmatched less than 7 days ago
    duplicates: list[OwnedRow] = field(default_factory=list)  # same game as a row in `lookups`
    lookups: list[OwnedRow] = field(default_factory=list)  # at most `budget`, in lookup order
    # TA id of a lookup row -> the override slug to resolve it with (taken from any row of the game)
    slugs: dict[int, str] = field(default_factory=dict)
    # TA id of a lookup row -> the `adding` / `partial` cache entry of its game (from any row of it):
    # an earlier add may have left an entry, so the step only re-checks it, never adds
    unsettled: dict[int, dict] = field(default_factory=dict)
    backlog: int = 0  # owned rows needing a lookup beyond the budget


def _parse(stamp) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def plan_collection(
    rows: list[OwnedRow],
    deku_entries: list[OwnedEntry],
    overrides: dict[int, dict],
    cache: dict[str, dict],
    now: datetime,
    budget: int = DEFAULT_BUDGET,
    *,
    dry_run: bool = False,
) -> CollectionPlan:
    if now.tzinfo is None:
        raise ValueError("plan_collection needs a timezone-aware `now`")
    plan = CollectionPlan()

    # Entries that count as owning the game: an Xbox entry, or one with no platform set.
    # Switch / PlayStation entries never do (Deku allows several entries per item).
    counted = [e for e in deku_entries if e.platform is None or e.platform in _XBOX]
    counted_slugs = {e.slug for e in counted}
    counted_titles = {norm(e.title) for e in counted} | {base_title(e.title) for e in counted}

    candidates: list[OwnedRow] = []
    for r in rows:
        if r.ownership != "Owned":
            continue
        if r.platform not in TARGET:
            plan.skipped_platform.append(r)
        else:
            candidates.append(r)

    # One game, one decision: rows sharing a base title (Series + One, or "Foo" + "Foo Deluxe
    # Edition") are grouped, and overrides apply to the group, whichever row they are keyed on.
    groups: dict[str, list[OwnedRow]] = {}
    for r in candidates:
        groups.setdefault(base_title(r.title), []).append(r)

    pending: list[tuple[OwnedRow, datetime | None]] = []
    for group in groups.values():
        group.sort(key=lambda r: (r.platform != "xbox-series-x-s", r.ta_id))  # Series row first
        rep = group[0]
        if any((overrides.get(r.ta_id) or {}).get("deku") == SKIP for r in group):
            plan.skipped_override.extend(group)
            continue
        plan.duplicates.extend(group[1:])
        # The representative's slug wins; otherwise the lowest TA id that has one.
        slug = next((s for r in group
                     if (s := (overrides.get(r.ta_id) or {}).get("deku"))), None)
        cached = cache.get(str(rep.ta_id)) or {}
        status = cached.get("status")
        checked = _parse(cached.get("checked")) if cached.get("checked") else None
        cools = {NONE, MATCHED, ADDED} | ({FOUND} if dry_run else set())
        # An add that may have left an entry, recorded on any row of the game (the representative can
        # change between runs): re-checked first, every run, never cooled.
        guard = next((e for r in group if (e := cache.get(str(r.ta_id)) or {}).get("status") in UNSETTLED), None)
        if slug:
            # A manual fix: already there by slug, otherwise look it up whatever the cache says (the
            # step's item-keyed guard still refuses an item whose earlier add is unconfirmed).
            if slug in counted_slugs:
                plan.already.append(rep)
            elif (status in cools and slug in (cached.get("slug"), cached.get("override"))
                  and checked is not None and timedelta(0) <= now - checked < COOLDOWN):
                plan.cooling.append(rep)  # this very override was handled less than 7 days ago
            else:
                plan.slugs[rep.ta_id] = slug
                pending.append((rep, checked))
        elif guard is not None:
            if norm(rep.title) in counted_titles or base_title(rep.title) in counted_titles:
                plan.already.append(rep)  # an Xbox / unset entry with its title is on the share page
            else:
                plan.unsettled[rep.ta_id] = dict(guard)
                pending.append((rep, checked))
        elif norm(rep.title) in counted_titles or base_title(rep.title) in counted_titles:
            plan.already.append(rep)
        elif status in {ADDED, MATCHED, FOUND} and cached.get("slug") in counted_slugs:
            plan.already.append(rep)
        elif (status in cools and checked is not None
              and timedelta(0) <= now - checked < COOLDOWN):
            plan.cooling.append(rep)
        else:
            pending.append((rep, checked))

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    pending.sort(key=lambda p: (
        p[0].ta_id not in plan.unsettled,  # an add that may have left an entry: checked first, every run
        p[0].platform != "xbox-series-x-s",  # Series before One
        p[1] is not None,  # never attempted before attempted
        p[1] or epoch,  # then the oldest check first
        p[0].ta_id,
    ))
    budget = max(budget, 0)
    plan.lookups = [r for r, _ in pending[:budget]]
    plan.slugs = {r.ta_id: plan.slugs[r.ta_id] for r in plan.lookups if r.ta_id in plan.slugs}
    plan.unsettled = {r.ta_id: plan.unsettled[r.ta_id] for r in plan.lookups if r.ta_id in plan.unsettled}
    plan.backlog = max(len(pending) - budget, 0)
    return plan


# --------------------------------------------------------------------------- the step

log = logging.getLogger("wsync")


@dataclass
class StepResult:
    dry_run: bool = True
    plan: CollectionPlan | None = None  # None: a collection read failed, nothing was planned
    added: list[str] = field(default_factory=list)  # titles added this run (live)
    outcomes: dict[str, int] = field(default_factory=dict)  # lookup outcome -> count, this run
    stopped: str | None = None  # why the step ended before its last lookup
    body: str | None = None  # the dry-run review text


class _Stop(Exception):
    """End the step quietly (rate limit, network trouble, deadline): no issue, exit status unaffected."""


class _Fail(Exception):
    """End the step with an issue (exit 1): the message says what to do."""


def _err(e: BaseException) -> str:
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _query(title: str) -> str:
    """Search text: trademark signs dropped outright, whitespace collapsed (as the wishlist step)."""
    return " ".join(re.sub("[™®©]", "", title).split())


def _has_exact(title: str, results: list[tuple[str, str]]) -> bool:
    return any(norm(t) == norm(title) or base_title(t) == base_title(title) for t, _ in results)


def _counts_entry(entry: dict | None) -> bool:
    return bool(entry) and entry.get("status") in {MATCHED, ADDED}


def _ta_rows(run, ta, state, now: datetime, res: "StepResult") -> list[OwnedRow] | None:
    """The TA collection, from state while that read is still current (`_ta_fresh`). None when there is none this run:
    after an issue (a failed read) or quietly (`res.stopped`) during the 6 h back-off after one, or
    when less than 10 minutes remain before the write deadline."""
    cached = state.ta_collection or {}
    fetched = _parse(cached.get("fetched")) if cached.get("fetched") else None
    if fetched is not None and _ta_fresh(fetched, now, run.cfg.ta_read_after):
        try:
            rows = [OwnedRow(**r) for r in cached["rows"]]
        except (KeyError, TypeError):
            rows = None
        if rows is not None:
            hours = (now - fetched).total_seconds() / 3600
            run.say(f"collection: TrueAchievements collection from state ({len(rows)} titles, read {hours:.1f} h ago)")
            return rows
    tried = _parse(cached.get("last_attempt")) if cached.get("last_attempt") else None
    if tried is not None and timedelta(0) <= now - tried < TA_RETRY:
        # Already reported by the failed run; a warning only, so the back-off doesn't fail six runs
        # (the wishlist step is fine, and failed runs would trip the stale-sync alert).
        res.stopped = "TrueAchievements read back-off"
        run.say(f"collection: TrueAchievements collection read failed {(now - tried).total_seconds() / 3600:.1f} h "
                f"ago ({cached.get('error') or 'unknown error'}); next try "
                f"{TA_RETRY.total_seconds() / 3600:.0f} h after it", logging.WARNING)
        return None
    if run.write_time_left() < TA_READ_MARGIN:
        res.stopped = "write deadline"
        run.say("collection: step not started, too close to the write deadline for a TrueAchievements "
                "collection read; next run", logging.WARNING)
        return None
    try:
        rows = ta.collection()
    except TAError as e:
        err = run.scrub(_err(e))
        state.ta_collection = {**cached, "last_attempt": now.isoformat(), "error": err}
        run.issue(_ta_failed(err))
        return None
    state.ta_collection = {"fetched": now.isoformat(), "rows": [asdict(r) for r in rows]}
    run.say(f"collection: read the TrueAchievements collection ({len(rows)} titles)")
    return rows


def _ta_fresh(fetched: datetime, now: datetime, read_after: dtime | None) -> bool:
    """Whether the cached TA collection is still this period's. With a daily read time (UTC) the
    cache lasts until the first run past that time after it was read; without one, 24 h."""
    if not timedelta(0) <= now - fetched:
        return False  # a cache from the future: read again
    if read_after is None:
        return now - fetched < TA_MAX_AGE
    now_utc = now.astimezone(timezone.utc)
    boundary = now_utc.replace(hour=read_after.hour, minute=read_after.minute, second=0, microsecond=0)
    if boundary > now_utc:
        boundary -= timedelta(days=1)  # the most recent read time at or before now
    return fetched >= boundary


def _settle_visible(run, state, entries: list[OwnedEntry], now: datetime) -> None:
    """Settle earlier adds the share page (just read) shows as done, at no request cost: an item in
    `collection_unconfirmed` with an Xbox or unset entry leaves the guard, and every `adding` /
    `partial` row on such an item is recorded as added. (The planner files such a game under
    `already`, so the item-page re-check would never run for it.)"""
    visible: dict[str, OwnedEntry] = {}
    for e in entries:
        if e.platform is None or e.platform in _XBOX:
            visible.setdefault(e.slug, e)
    unconfirmed = state.collection_unconfirmed
    for slug in sorted(s for s in unconfirmed if s in visible):
        unconfirmed.pop(slug)
        run.say(f"collection: the earlier add on {slug} is on the share page now")
    for key, entry in state.collection.items():
        slug = entry.get("slug")
        if entry.get("status") in UNSETTLED and slug in visible:
            e = visible[slug]
            new = {"status": ADDED, "slug": slug, "checked": now.isoformat(), "title": entry.get("title")}
            new.update({k: v for k, v in (("format", e.format), ("platform", e.platform)) if v})
            state.collection[key] = new


def _ta_failed(err: str) -> str:
    return f"TrueAchievements collection read failed, no Deku collection adds this run: {err}"


class _Step:
    def __init__(self, run, deku, state, plan: CollectionPlan, now: datetime, dry: bool, res: StepResult):
        self.run, self.deku, self.state, self.plan = run, deku, state, plan
        self.now, self.dry, self.res = now, dry, res
        self.override: str | None = None  # the override slug of the lookup under way
        self.dups: dict[str, list[OwnedRow]] = {}
        for r in plan.duplicates:
            self.dups.setdefault(base_title(r.title), []).append(r)

    def _item(self, slug: str) -> DekuItem:
        self.run.sleep(PACE)
        return self.deku.item(slug)

    def _group(self, row: OwnedRow) -> list[OwnedRow]:
        return [row, *self.dups.get(base_title(row.title), [])]

    def _record(self, row: OwnedRow, status: str, slug: str | None, reason: str = "", **extra) -> None:
        entry = {"status": status, "slug": slug, "checked": self.now.isoformat(), "title": row.title, **extra}
        if self.override:
            entry["override"] = self.override  # its 7-day cooldown applies while the override stays the same
        if reason:
            entry["reason"] = reason
        for r in self._group(row):
            self.state.collection[str(r.ta_id)] = dict(entry)
        self.res.outcomes[status] = self.res.outcomes.get(status, 0) + 1
        platform = extra.get("platform") or TARGET[row.platform]
        text = {MATCHED: "already in the collection", ADDED: "added", FOUND: "would add (dry run)",
                NONE: "no match"}[status]
        self.run.say(f"collection: {row.title} [{platform}] -> {text}"
                     + (f" ({slug})" if slug else "") + (f": {reason}" if reason else ""))

    def lookup(self, row: OwnedRow) -> None:
        cached = self.state.collection.get(str(row.ta_id)) or {}
        self.override = None
        if row.ta_id in self.plan.unsettled:  # an earlier add may have left an entry: never add again
            guard = self.plan.unsettled[row.ta_id]
            if not guard.get("slug"):
                raise _Fail(f"check {row.title} on Deku Deals by hand: an earlier collection add may have left "
                            "an entry; not added again until a `collection` override settles it; collection "
                            "step stopped")
            return self._recheck(row, self._item(guard["slug"]), guard)
        slug, how = self.plan.slugs.get(row.ta_id), "override"
        self.override = slug
        if slug is None and cached.get("status") == FOUND and cached.get("slug"):
            slug, how = cached["slug"], "cached"
        it = None
        if slug:
            it = self._item(slug)
            if how == "cached" and pick(row.title, [(it.title, it.slug)]).status != "matched":
                self.run.say(f"collection: cached match for {row.title} ({slug}) no longer matches; searching again")
                it = None
        if it is None:
            results = self.deku.search(_query(row.title))
            bare = strip_edition_noise(row.title)
            if not _has_exact(row.title, results) and norm(bare) != norm(row.title):
                seen = {s for _, s in results}
                results += [(t, s) for t, s in self.deku.search(_query(bare)) if s not in seen]
            m = pick(row.title, results)
            if m.status != "matched":
                why = (f"{len(m.candidates)} candidates, none confident" if m.candidates else "not found")
                return self._record(row, NONE, None, why)
            it = self._item(m.target)
            if pick(row.title, [(it.title, it.slug)]).status != "matched":
                return self._record(row, NONE, None, f"item page title {it.title!r} differs")
        if it.slug in self.state.collection_unconfirmed:
            # An earlier add on this very item is unconfirmed (whatever row, override or redirect led
            # here): only the page just read can settle it; own() is never called on it.
            return self._recheck(row, it)
        target = choose_platform(row.platform, it.offered)
        if target is None:
            return self._record(row, NONE, None, f"{it.slug} is not listed for any Xbox platform")
        if any(o.platform is None or o.platform in _XBOX for o in it.owned):
            return self._record(row, MATCHED, it.slug)
        fmt = deku_format(row.media)
        if self.dry:
            return self._record(row, FOUND, it.slug, format=fmt, platform=target)
        if self.run.past_deadline():
            raise _Stop("write deadline")
        unconfirmed = self.state.collection_unconfirmed
        if it.slug in unconfirmed:  # (checked above; never own() a listed item, whatever happened since)
            return self._recheck(row, it)
        # Write-ahead, saved before the create: if the add fails part-way or the run dies inside own(),
        # the item stays listed and own() is never called on it again. The row-level `adding` (on every
        # row of the game) sends the game to a re-check first on the next runs.
        keys = [str(r.ta_id) for r in self._group(row)]
        previous = {k: self.state.collection.get(k) for k in keys}
        baseline = [o.public_id for o in it.owned]
        titles = list(dict.fromkeys(r.title for r in self._group(row)))
        unconfirmed[it.slug] = {"since": self.now.isoformat(), "titles": titles, "baseline": baseline}
        ahead = {"status": ADDING, "slug": it.slug, "baseline": baseline, "checked": self.now.isoformat(),
                 "title": row.title, "format": fmt, "platform": target}
        for k in keys:
            self.state.collection[k] = dict(ahead)
        try:
            self.run.checkpoint()
        except Exception as e:  # noqa: BLE001
            unconfirmed.pop(it.slug, None)
            for k, old in previous.items():
                if old is None:
                    self.state.collection.pop(k, None)
                else:
                    self.state.collection[k] = old
            raise _Fail(f"state not saved before adding {row.title} to the Deku collection "
                        f"({type(e).__name__}); not added, collection step stopped") from None
        try:
            self.deku.own(it, fmt, target)  # `it` is the item page read just now
        except DekuPartialOwn:
            for k in keys:
                self.state.collection[k]["status"] = PARTIAL
            raise  # the item stays in `collection_unconfirmed`
        unconfirmed.pop(it.slug, None)  # own() read the new entry back: confirmed
        self.res.added.append(row.title)
        self._record(row, ADDED, it.slug, format=fmt, platform=target)

    def _recheck(self, row: OwnedRow, it: DekuItem, guard: dict | None = None) -> None:
        """`it` (just read) is an item an earlier add may have left an entry on: recorded as added once
        its page shows an Xbox or unset entry (and dropped from `collection_unconfirmed`); otherwise a
        hand check is needed (an issue that stops the step). Never calls own()."""
        unconfirmed = self.state.collection_unconfirmed
        slugs = [it.slug, *([guard["slug"]] if guard and guard.get("slug") else [])]
        record = next((unconfirmed[s] for s in slugs if s in unconfirmed), None) or guard or {}
        xbox = [o for o in it.owned if o.platform is None or o.platform in _XBOX]
        if xbox:
            for s in slugs:
                unconfirmed.pop(s, None)
            self.run.say(f"collection: {row.title}: the earlier add on {it.slug} is in the collection now")
            extra = {k: v for k, v in (("format", xbox[0].format), ("platform", xbox[0].platform)) if v}
            return self._record(row, ADDED, it.slug, **extra)
        baseline = set(record.get("baseline") or ())
        new = sorted(o.platform or "unset" for o in it.owned if o.public_id not in baseline)
        if new:
            what = (f"an earlier collection add on this item left an entry ({', '.join(new)}) that is not on "
                    "Xbox: set it to Xbox by hand")
        else:
            what = ("an earlier collection add on this item was not confirmed, and its page shows no new entry "
                    "since, so it most likely left none (the create may have been refused): add it by hand on "
                    "Xbox if it is missing")
        raise _Fail(f"check {row.title} ({it.slug}) on Deku Deals by hand: {what}. It is not added again "
                    "until the item shows an Xbox entry, or a `collection` override skips the game or names "
                    "another item; collection step stopped")


def run_collection_step(run, deku, ta, state, overrides: dict[int, dict], now: datetime) -> StepResult:
    """One run of the collection step. `run` is the wishlist run (`wsync.run._Run`): its `say`, `issue`,
    `scrub`, `notify`, `sleep`, `past_deadline()`, `write_time_left()`, `checkpoint()` (save the state
    now), `collection_dry` and `reporter`.

    Failures that leave nothing to report become `run.issue` (exit 1); a Deku 429, a network error or
    the write deadline end the step quietly. Exceptions other than Deku/TA errors propagate.
    """
    dry = run.collection_dry
    res = StepResult(dry_run=dry)
    rows = _ta_rows(run, ta, state, now, res)
    if rows is None:
        return res
    paced = deku.paced(PACE)
    try:
        run.sleep(PACE)
        entries = paced.collection()
    except (DekuRateLimited, DekuNetworkError) as e:
        res.stopped = f"Deku collection read: {type(e).__name__}"
        run.say(f"collection: Deku Deals collection read stopped ({type(e).__name__}); "
                "no lookups this run, retried next run", logging.WARNING)
        return res
    except DekuError as e:
        run.issue(f"Deku Deals collection read failed, no Deku collection adds this run: {_err(e)}")
        return res
    _settle_visible(run, state, entries, now)
    plan = plan_collection(rows, entries, overrides, state.collection, now, dry_run=dry)
    res.plan = plan
    owned = sum(r.ownership == "Owned" for r in rows)
    run.say(f"collection: {'DRY RUN' if dry else 'APPLY'}; TrueAchievements {len(rows)} titles, {owned} owned; "
            f"Deku collection {len(entries)} entries; already {len(plan.already)}, skipped platform "
            f"{len(plan.skipped_platform)}, skipped by override {len(plan.skipped_override)}, cooling "
            f"{len(plan.cooling)}, duplicates {len(plan.duplicates)}, lookups {len(plan.lookups)}, "
            f"backlog {plan.backlog}")

    step = _Step(run, paced, state, plan, now, dry, res)
    for n, row in enumerate(plan.lookups):
        if run.past_deadline():
            res.stopped = "write deadline"
            run.say(f"collection: {len(plan.lookups) - n} lookups left for the next run (past the write "
                    "deadline)", logging.WARNING)
            break
        try:
            step.lookup(row)
        except _Stop as e:
            res.stopped = str(e)
            run.say(f"collection: {len(plan.lookups) - n} lookups left for the next run ({e})", logging.WARNING)
            break
        except _Fail as e:
            res.stopped = "needs attention"
            run.issue(f"Deku collection: {e}")
            break
        except (DekuRateLimited, DekuNetworkError) as e:
            res.stopped = type(e).__name__
            run.say(f"collection: Deku Deals answered {type(e).__name__} looking up {row.title}; "
                    f"step stopped, {len(plan.lookups) - n} lookups left for the next run", logging.WARNING)
            break
        except DekuPartialOwn as e:
            res.stopped = "partial add"
            run.issue(f"Deku Deals collection add of {row.title} failed after the entry was created; it may be "
                      f"in the collection with the wrong or no platform/format, check it by hand ({_err(e)}); "
                      "collection step stopped")
            break
        except DekuError as e:
            res.stopped = type(e).__name__
            run.issue(f"Deku Deals collection lookup failed for {row.title}: {_err(e)}; collection step stopped")
            break
    done = ", ".join(f"{k} {v}" for k, v in sorted(res.outcomes.items())) or "none"
    run.say(f"collection: this run: {done}" + (f"; stopped: {res.stopped}" if res.stopped else ""))
    _report(run, res, rows, plan, state)
    return res


def _review_lists(rows: list[OwnedRow], plan: CollectionPlan, cache: dict[str, dict],
                  unconfirmed: dict[str, dict]):
    """(would add, unmatched, check by hand, already count, waiting count) for the dry-run review."""
    out_ids = {r.ta_id for r in plan.skipped_override} | {r.ta_id for r in plan.duplicates}
    already_ids = {r.ta_id for r in plan.already}
    reps = [r for r in rows if r.ownership == "Owned" and r.platform in TARGET and r.ta_id not in out_ids]
    would, unmatched, already, waiting = [], [], 0, 0
    for r in reps:
        entry = cache.get(str(r.ta_id)) or {}
        if r.ta_id in already_ids or _counts_entry(entry):
            already += 1
        elif entry.get("status") == FOUND:
            would.append((r.title, entry.get("slug") or "?", entry.get("format") or "format unset",
                          entry.get("platform") or TARGET[r.platform]))
        elif entry.get("status") == NONE:
            unmatched.append((r.title, r.ta_id))
        elif r.ta_id in plan.unsettled or entry.get("status") in UNSETTLED:
            continue  # its item is under "Check by hand"
        else:
            waiting += 1
    check = sorted((", ".join(rec.get("titles") or ()) or "?", slug) for slug, rec in unconfirmed.items())
    return sorted(would), sorted(unmatched), check, already, waiting


def review_signature(rows: list[OwnedRow], plan: CollectionPlan, cache: dict[str, dict],
                     unconfirmed: dict[str, dict]) -> str:
    """What the "plan ready" ping tracks: the would-add, unmatched and check-by-hand lists only. The
    waiting count moves every run while the backlog drains, so it never triggers a ping by itself."""
    would, unmatched, check, _, _ = _review_lists(rows, plan, cache, unconfirmed)
    blob = json.dumps([would, unmatched, check], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def review_body(rows: list[OwnedRow], plan: CollectionPlan, cache: dict[str, dict],
                unconfirmed: dict[str, dict]) -> tuple[str, dict[str, int]]:
    """The dry-run review text (deterministic: a re-run with nothing new gives the same text) and its counts.
    `unconfirmed` is `State.collection_unconfirmed`: the "Check by hand" list, one line per item."""
    would, unmatched, check, already, waiting = _review_lists(rows, plan, cache, unconfirmed)
    counts = {"would_add": len(would), "already": already, "unmatched": len(unmatched), "waiting": waiting,
              "check": len(check)}
    lines = [
        "Dry run: nothing has been added to the Deku Deals collection. Source: TrueAchievements games "
        "marked Owned on Xbox Series X|S or Xbox One.",
        f"Summary: would add {len(would)}; already in the collection {already}; unmatched {len(unmatched)}; "
        f"waiting for a lookup {waiting}",
        f"## Would add ({len(would)})",
    ]
    lines += [f"- {t} (`{s}`, {f}, {p})" for t, s, f, p in sorted(would)]
    lines.append(f"## Unmatched ({len(unmatched)})")
    if unmatched:
        lines.append("No confident Deku Deals match (or not listed for any Xbox platform); retried 7 days after "
                     "each try. Fix one with a `collection` override keyed by its TrueAchievements game id.")
    lines += [f"- {t} (TA {i})" for t, i in sorted(unmatched)]
    if check:
        lines.append(f"## Check by hand ({len(check)})")
        lines.append("An earlier add may have left an entry; these are never added again automatically.")
        lines += [f"- {t} (`{s}`)" for t, s in sorted(check)]
    lines += [
        "## Counts",
        f"- Owned on TrueAchievements: {sum(r.ownership == 'Owned' for r in rows)}",
        f"- Already in the Deku collection: {already}",
        f"- Skipped, platform not synced (Xbox 360, Windows, mobile, Switch): {len(plan.skipped_platform)}",
        f"- Skipped by override: {len(plan.skipped_override)}",
        f"- Same game as another row (one Deku entry per game): {len(plan.duplicates)}",
        f"- Unmatched, cooling 7 days between tries: {len(unmatched)}",
        f"- Waiting for a lookup: {waiting}",
    ]
    return "\n".join(lines), counts


def _report(run, res: StepResult, rows: list[OwnedRow], plan: CollectionPlan, state) -> None:
    if res.dry_run:
        body, counts = review_body(rows, plan, state.collection, state.collection_unconfirmed)
        sig = review_signature(rows, plan, state.collection, state.collection_unconfirmed)
        res.body = body
        for line in body.splitlines():
            run.say(f"collection plan | {line}")
        try:
            run.reporter.collection_review(body, dry_run=True)  # the task text, waiting count included
        except Exception as e:  # noqa: BLE001 - reporting must not take the step down
            run.issue(f"Vikunja collection review task not updated ({type(e).__name__})")
            return
        if sig != state.collection_review:  # pinged only when the lists change, not the waiting count
            state.collection_review = sig
            run.notify("Deku collection plan ready",
                       f"Dry run: would add {counts['would_add']} to the Deku collection; already there "
                       f"{counts['already']}; unmatched {counts['unmatched']}; waiting for a lookup "
                       f"{counts['waiting']}.\nReview the plan in Vikunja (Wishlist Sync).")
        return
    state.collection_review = None  # a later dry run pings its first plan again
    try:
        run.reporter.collection_review(None, dry_run=False)
    except Exception as e:  # noqa: BLE001
        run.issue(f"Vikunja collection review task not closed ({type(e).__name__})")
    if res.added:
        run.notify("Deku collection", f"+{len(res.added)} Deku collection\n"
                   + "\n".join(f"- {t}" for t in sorted(res.added)))

"""Reconciliation planner (pure, no I/O).

Decides what to add, update and remove on each list. Removals are the
dangerous part: they happen only for entries that no Xbox item matches
confidently, and are paused for a whole list while any Xbox item on that list
is ambiguous (the unresolved item might be the one that owns the entry).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from wsync.feed import XboxItem, is_dlc
from wsync.match import Match

XBOX_PLATFORMS = frozenset({"xbox_series", "xbox_one"})
_NONE = Match("none", None, [])
AMBIGUOUS_PREFIX = "ambiguous match for: "
TRANSIENT = "transient: "  # candidate prefix: an unresolved lookup, not a matching problem


def transient(reason: str) -> Match:
    """Ambiguous for planning (removals pause), but no problem task: the next run retries."""
    return Match("ambiguous", None, [TRANSIENT + reason])


def is_transient(m: Match | None) -> bool:
    return m is not None and m.status == "ambiguous" and any(c.startswith(TRANSIENT) for c in m.candidates)


@dataclass(frozen=True)
class Entry:
    target: str
    title: str
    format: str | None = None  # Deku only
    platforms: frozenset[str] = frozenset()  # Deku only


@dataclass(frozen=True)
class Problem:
    product_id: str
    title: str
    kind: Literal["unmatched-deku", "unmatched-ta", "d2d-disputed"]
    detail: str


@dataclass
class ListPlan:
    adds: list[Entry] = field(default_factory=list)
    removes: list[Entry] = field(default_factory=list)
    updates: list[tuple[Entry, Entry]] = field(default_factory=list)  # (current, desired)
    paused_removes: list[Entry] = field(default_factory=list)
    pause_reason: str | None = None


@dataclass
class Plan:
    deku: ListPlan = field(default_factory=ListPlan)
    ta: ListPlan = field(default_factory=ListPlan)
    problems: list[Problem] = field(default_factory=list)
    skipped_unannounced: list[str] = field(default_factory=list)


def desired_format(item: XboxItem, verdict: str | None) -> str:
    if not is_dlc(item) and verdict in ("yes", "disputed"):
        return "either"
    return "digital"


def _classify(matches: dict[str, Match], item: XboxItem) -> tuple[str, Match]:
    """Return ("matched" | "none" | "ambiguous", match).

    Fail safe: only a clean "matched" with a target, or a clean "none" (or no
    entry at all), is acted on. Anything else is treated as ambiguous so it
    pauses removals instead of unlocking them.
    """
    m = matches.get(item.product_id, _NONE)
    if m.status == "matched" and isinstance(m.target, str) and m.target:
        return "matched", m
    if m.status == "none":
        return "none", m
    return "ambiguous", m


def _unmatched_detail(status: str, m: Match) -> str:
    if m.status == "ambiguous":
        return "ambiguous match, candidates: " + "; ".join(m.candidates)
    if status == "ambiguous":
        return "unrecognized match result"
    return "no page found"


def _reconcile(
    *,
    items: list[XboxItem],
    matches: dict[str, Match],
    current: dict[str, Entry],
    problem_kind: Literal["unmatched-deku", "unmatched-ta"],
    desired_for,  # (item, target) -> Entry
    merge,  # (Entry, Entry) -> Entry, combining two items on one target
    settle,  # (current, desired) -> desired, adjusted against what is already there
    is_changed,  # (current, desired) -> bool
    plan: Plan,
    skipped_ids: set[str],
    ambiguous_ids: set[str],
) -> ListPlan:
    lp = ListPlan()
    desired: dict[str, Entry] = {}
    ambiguous_titles: list[str] = []
    unresolved = 0

    for item in items:
        status, m = _classify(matches, item)
        if status == "matched":
            entry = desired_for(item, m.target)
            desired[m.target] = merge(desired[m.target], entry) if m.target in desired else entry
        elif status == "ambiguous":
            if is_transient(m):
                unresolved += 1  # counted, not named: which lookup flaked is noise for a reviewer
            else:
                ambiguous_titles.append(item.title)
            ambiguous_ids.add(item.product_id)
            plan.problems.append(
                Problem(item.product_id, item.title, problem_kind, _unmatched_detail(status, m))
            )
        elif item.kind == "unannounced":
            skipped_ids.add(item.product_id)
        else:
            plan.problems.append(
                Problem(item.product_id, item.title, problem_kind, _unmatched_detail(status, m))
            )

    for target, want in desired.items():
        have = current.get(target)
        if have is None:
            lp.adds.append(want)
        else:
            want = settle(have, want)
            if is_changed(have, want):
                lp.updates.append((have, want))

    stale = [e for t, e in current.items() if t not in desired]
    if ambiguous_titles or unresolved:
        lp.paused_removes = stale
        reasons = [AMBIGUOUS_PREFIX + ", ".join(ambiguous_titles)] if ambiguous_titles else []
        if unresolved:
            reasons.append(f"{unresolved} lookup{'s' if unresolved != 1 else ''} retried next run")
        lp.pause_reason = "; ".join(reasons)
    else:
        lp.removes = stale
    return lp


def build_plan(
    items: list[XboxItem],
    deku_matches: dict[str, Match],
    ta_matches: dict[str, Match],
    verdicts: dict[str, str | None],
    deku_current: dict[str, Entry],
    ta_current: dict[str, Entry],
    deku_item_platforms: dict[str, frozenset[str]],
) -> Plan:
    plan = Plan()
    skipped_ids: set[str] = set()
    ambiguous_ids: set[str] = set()

    def deku_desired(item: XboxItem, target: str) -> Entry:
        # Unknown target: plan the add with no platforms; the client fills them in.
        plats = frozenset(deku_item_platforms.get(target, frozenset())) & XBOX_PLATFORMS
        return Entry(target, item.title, desired_format(item, verdicts.get(item.product_id)), plats)

    def deku_merge(a: Entry, b: Entry) -> Entry:
        fmt = "either" if "either" in (a.format, b.format) else a.format
        return Entry(a.target, a.title, fmt, a.platforms | b.platforms)

    def deku_settle(have: Entry, want: Entry) -> Entry:
        if want.target in deku_item_platforms:
            return want
        # Platforms unknown for this page: keep the Xbox boxes already ticked rather than clear them.
        return Entry(want.target, want.title, want.format, have.platforms & XBOX_PLATFORMS)

    def deku_changed(have: Entry, want: Entry) -> bool:
        # Only the Xbox checkboxes are ours; other platforms on the page are left alone.
        return (
            # format None: the Deku item has no format choice (DLC), so only platforms count
            (have.format is not None and have.format != want.format)
            or (have.platforms & XBOX_PLATFORMS) != (want.platforms & XBOX_PLATFORMS)
        )

    plan.deku = _reconcile(
        items=items, matches=deku_matches, current=deku_current,
        problem_kind="unmatched-deku", desired_for=deku_desired, merge=deku_merge,
        settle=deku_settle, is_changed=deku_changed, plan=plan, skipped_ids=skipped_ids, ambiguous_ids=ambiguous_ids,
    )
    plan.ta = _reconcile(
        items=items, matches=ta_matches, current=ta_current,
        problem_kind="unmatched-ta",
        desired_for=lambda item, target: Entry(target, item.title),
        merge=lambda a, b: a,
        settle=lambda have, want: want,
        is_changed=lambda have, want: False,
        plan=plan, skipped_ids=skipped_ids, ambiguous_ids=ambiguous_ids,
    )

    for item in items:
        if (
            item.product_id in skipped_ids
            and item.product_id not in ambiguous_ids
            and item.title not in plan.skipped_unannounced
        ):
            plan.skipped_unannounced.append(item.title)
        if not is_dlc(item) and verdicts.get(item.product_id) == "disputed":
            plan.problems.append(
                Problem(item.product_id, item.title, "d2d-disputed", "Disc to Digital sources disagree")
            )
    return plan

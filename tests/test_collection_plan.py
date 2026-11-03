from datetime import datetime, timedelta, timezone

from wsync.collection import TARGET, deku_format, plan_collection
from wsync.deku import OwnedEntry
from wsync.ta_coll import OwnedRow

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def row(ta_id, title, platform="xbox-series-x-s", ownership="Owned", media="Digital"):
    return OwnedRow(ta_id, title, platform, ownership, media)


def entry(title, platform=None, slug=None, fmt=None):
    return OwnedEntry(slug or title.lower().replace(" ", "-"), title, platform, fmt)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def plan(rows, entries=(), overrides=None, cache=None, budget=25):
    return plan_collection(rows, list(entries), overrides or {}, cache or {}, NOW, budget)


def test_target_and_format():
    assert TARGET == {"xbox-series-x-s": "xbox_series", "xbox-one": "xbox_one"}
    assert deku_format("Disc") == "physical"
    for m in ("Digital", "Disc-to-Digital", "Disc & Digital"):
        assert deku_format(m) == "digital"
    assert deku_format("") is None
    assert deku_format("Rental") is None


def test_only_owned_rows_are_considered():
    p = plan([row(1, "Halo", ownership="Xbox Game Pass (deprecated)"),
              row(2, "Doom", ownership="No longer have"), row(3, "Fable")])
    assert [r.ta_id for r in p.lookups] == [3]
    assert not (p.already or p.skipped_platform or p.skipped_override or p.cooling)


def test_other_platforms_are_skipped():
    rows = [row(1, "A", "xbox-360"), row(2, "B", "windows"), row(3, "C", "nintendo-switch"),
            row(4, "D", "ios"), row(5, "E", "android"), row(6, "F", "xbox-one")]
    p = plan(rows)
    assert [r.ta_id for r in p.skipped_platform] == [1, 2, 3, 4, 5]
    assert [r.ta_id for r in p.lookups] == [6]


def test_override_skip_and_slug():
    rows = [row(1, "Skipped"), row(2, "Weird Title"), row(3, "Plain")]
    p = plan(rows, overrides={1: {"deku": "skip"}, 2: {"deku": "weird-slug"}})
    assert [r.ta_id for r in p.skipped_override] == [1]
    assert [r.ta_id for r in p.lookups] == [2, 3]


def test_override_slug_already_in_collection_is_already():
    p = plan([row(2, "Weird Title")], [entry("Other Name", "xbox_series", slug="weird-slug")],
             overrides={2: {"deku": "weird-slug"}})
    assert [r.ta_id for r in p.already] == [2] and not p.lookups


def test_override_slug_bypasses_cooling():
    p = plan([row(2, "Weird")], overrides={2: {"deku": "weird-slug"}},
             cache={"2": {"status": "none", "slug": None, "checked": ago(1)}})
    assert [r.ta_id for r in p.lookups] == [2]


def test_platform_skip_wins_over_override_skip():
    p = plan([row(1, "A", "windows")], overrides={1: {"deku": "skip"}})
    assert [r.ta_id for r in p.skipped_platform] == [1] and not p.skipped_override


def test_xbox_entry_with_same_title_is_already():
    entries = [entry("Halo Infinite", "xbox_one"), entry("Doom", "xbox_series")]
    p = plan([row(1, "Halo Infinite"), row(2, "DOOM"), row(3, "Fable")], entries)
    assert [r.ta_id for r in p.already] == [1, 2]
    assert [r.ta_id for r in p.lookups] == [3]


def test_base_title_match_is_already():
    p = plan([row(1, "Foo Deluxe Edition")], [entry("Foo", "xbox_series")])
    assert [r.ta_id for r in p.already] == [1]
    p = plan([row(1, "Foo")], [entry("Foo Deluxe Edition", "xbox_one")])
    assert [r.ta_id for r in p.already] == [1]


def test_unset_platform_entry_counts_as_already():
    p = plan([row(1, "Halo")], [entry("Halo", None)])
    assert [r.ta_id for r in p.already] == [1] and not p.lookups


def test_switch_or_ps_only_hit_is_never_already_and_goes_to_lookup():
    entries = [entry("Halo", "switch"), entry("Doom", "ps5")]
    p = plan([row(1, "Halo"), row(2, "Doom")], entries)
    assert not p.already
    assert [r.ta_id for r in p.lookups] == [1, 2]
    assert not hasattr(p, "other_platform")


def test_switch_entry_plus_unset_entry_is_already():
    p = plan([row(1, "Halo")], [entry("Halo", "switch"), entry("Halo", None, slug="halo-2")])
    assert [r.ta_id for r in p.already] == [1]


def test_cache_added_or_matched_with_slug_in_collection_is_already():
    entries = [entry("Totally Different", "xbox_series", slug="the-slug")]
    for status in ("added", "matched"):
        p = plan([row(1, "Halo")], entries, cache={"1": {"status": status, "slug": "the-slug", "checked": ago(2)}})
        assert [r.ta_id for r in p.already] == [1]


def test_cache_added_but_slug_missing_or_only_non_xbox_cools_then_goes_to_lookup():
    # Hidden from the share page or deleted by hand: re-checked weekly, not every run.
    p = plan([row(1, "Halo")], [entry("Other", "switch", slug="the-slug")],
             cache={"1": {"status": "added", "slug": "the-slug", "checked": ago(2)}})
    assert not p.already and not p.lookups and [r.ta_id for r in p.cooling] == [1]
    p = plan([row(1, "Halo")], [], cache={"1": {"status": "matched", "slug": "gone", "checked": ago(8)}})
    assert [r.ta_id for r in p.lookups] == [1]


def test_none_cache_cools_for_seven_days():
    cache = {"1": {"status": "none", "slug": None, "checked": ago(6.9)},
             "2": {"status": "none", "slug": None, "checked": ago(7.1)}}
    p = plan([row(1, "A"), row(2, "B")], cache=cache)
    assert [r.ta_id for r in p.cooling] == [1]
    assert [r.ta_id for r in p.lookups] == [2]


def test_garbage_checked_stamp_is_treated_as_stale():
    p = plan([row(1, "A")], cache={"1": {"status": "none", "slug": None, "checked": "bogus"}})
    assert [r.ta_id for r in p.lookups] == [1]


def test_series_and_one_rows_of_one_game_make_one_series_lookup():
    rows = [row(1, "Halo", "xbox-one"), row(2, "Halo", "xbox-series-x-s")]
    p = plan(rows)
    assert [r.ta_id for r in p.lookups] == [2]
    assert p.lookups[0].platform == "xbox-series-x-s"
    assert [r.ta_id for r in p.duplicates] == [1]
    assert p.backlog == 0


def test_duplicate_title_with_different_case_or_edition_punctuation_groups():
    p = plan([row(1, "Halo: Infinite", "xbox-one"), row(2, "HALO INFINITE")])
    assert [r.ta_id for r in p.lookups] == [2]


def test_order_series_first_then_never_attempted_then_oldest_checked():
    rows = [row(1, "One Old", "xbox-one"), row(2, "Series Old"), row(3, "Series New"),
            row(4, "Series Never"), row(5, "One Never", "xbox-one")]
    cache = {"2": {"status": "none", "slug": None, "checked": ago(30)},
             "3": {"status": "none", "slug": None, "checked": ago(8)},
             "1": {"status": "none", "slug": None, "checked": ago(9)}}
    p = plan(rows, cache=cache)
    assert [r.ta_id for r in p.lookups] == [4, 2, 3, 5, 1]


def test_budget_cuts_and_counts_backlog():
    rows = [row(i, f"Game {i}") for i in range(1, 8)]
    p = plan(rows, budget=3)
    assert [r.ta_id for r in p.lookups] == [1, 2, 3]
    assert p.backlog == 4


def test_default_budget_is_25():
    p = plan([row(i, f"Game {i}") for i in range(1, 31)])
    assert len(p.lookups) == 25 and p.backlog == 5


def test_planner_does_not_mutate_inputs():
    rows = [row(1, "Halo")]
    entries = [entry("Halo", "switch")]
    cache = {"1": {"status": "none", "slug": None, "checked": ago(9)}}
    import copy
    before = copy.deepcopy(cache)
    plan(rows, entries, cache=cache)
    assert cache == before and rows == [row(1, "Halo")]


def test_skip_on_series_row_skips_the_whole_game():
    rows = [row(1, "Halo", "xbox-one"), row(2, "Halo", "xbox-series-x-s")]
    p = plan(rows, overrides={2: {"deku": "skip"}})
    assert sorted(r.ta_id for r in p.skipped_override) == [1, 2]
    assert not p.lookups and not p.duplicates


def test_skip_on_one_row_skips_the_whole_game():
    rows = [row(1, "Halo", "xbox-one"), row(2, "Halo", "xbox-series-x-s")]
    p = plan(rows, overrides={1: {"deku": "skip"}})
    assert sorted(r.ta_id for r in p.skipped_override) == [1, 2] and not p.lookups


def test_slug_on_one_row_applies_to_the_series_lookup():
    rows = [row(1, "Halo", "xbox-one"), row(2, "Halo", "xbox-series-x-s")]
    p = plan(rows, overrides={1: {"deku": "halo-slug"}})
    assert [r.ta_id for r in p.lookups] == [2]
    assert p.slugs == {2: "halo-slug"}


def test_conflicting_slugs_prefer_representative_then_lowest_id():
    rows = [row(1, "Halo", "xbox-one"), row(2, "Halo", "xbox-series-x-s")]
    p = plan(rows, overrides={1: {"deku": "one-slug"}, 2: {"deku": "series-slug"}})
    assert p.slugs == {2: "series-slug"}
    rows = [row(5, "Halo", "xbox-one"), row(3, "Halo", "xbox-one"), row(9, "Halo", "xbox-series-x-s")]
    p = plan(rows, overrides={5: {"deku": "five"}, 3: {"deku": "three"}})
    assert p.slugs == {9: "three"}


def test_slug_override_on_other_row_already_in_collection():
    rows = [row(1, "Halo", "xbox-one"), row(2, "Halo", "xbox-series-x-s")]
    p = plan(rows, [entry("Other", "xbox_one", slug="halo-slug")], overrides={1: {"deku": "halo-slug"}})
    assert [r.ta_id for r in p.already] == [2] and not p.lookups and not p.slugs


def test_groups_by_base_title():
    rows = [row(1, "Foo Deluxe Edition", "xbox-one"), row(2, "Foo", "xbox-series-x-s")]
    p = plan(rows)
    assert [r.ta_id for r in p.lookups] == [2]
    assert [r.ta_id for r in p.duplicates] == [1]
    assert p.backlog == 0


def test_future_dated_checked_does_not_cool():
    p = plan([row(1, "A")], cache={"1": {"status": "none", "slug": None, "checked": ago(-3)}})
    assert not p.cooling and [r.ta_id for r in p.lookups] == [1]


def test_naive_now_is_rejected():
    import pytest
    with pytest.raises(ValueError, match="timezone"):
        plan_collection([], [], {}, {}, datetime(2026, 11, 2))


def test_dry_run_found_entry_counts_as_already_once_its_slug_is_on_an_xbox_entry():
    cache = {"1": {"status": "found", "slug": "halo-x", "checked": ago(1)}}
    p = plan([row(1, "Halo")], [entry("Halo Renamed", "xbox_series", slug="halo-x")], cache=cache)
    assert [r.ta_id for r in p.already] == [1]
    # Not in the collection yet: still a lookup (a live run adds it), after never-attempted rows.
    p = plan([row(1, "Halo"), row(2, "Fable")], cache=cache)
    assert [r.ta_id for r in p.lookups] == [2, 1]


def test_found_cools_in_a_dry_run_but_is_an_add_in_a_live_run():
    cache = {"1": {"status": "found", "slug": "halo", "checked": ago(1)}}
    dry = plan_collection([row(1, "Halo")], [], {}, cache, NOW, dry_run=True)
    assert [r.ta_id for r in dry.cooling] == [1] and not dry.lookups
    live = plan_collection([row(1, "Halo")], [], {}, cache, NOW)
    assert [r.ta_id for r in live.lookups] == [1]
    stale = plan_collection([row(1, "Halo")], [], {}, {"1": {**cache["1"], "checked": ago(8)}}, NOW, dry_run=True)
    assert [r.ta_id for r in stale.lookups] == [1]


def test_adding_or_partial_rows_are_checked_first_every_run_and_never_cool():
    cache = {"1": {"status": "partial", "slug": "halo", "checked": ago(0.01)},
             "2": {"status": "adding", "slug": "fable", "checked": ago(0.01)},
             "3": {"status": "matched", "slug": "gone", "checked": ago(30)}}
    p = plan([row(3, "Doom"), row(1, "Halo", "xbox-one"), row(2, "Fable", "xbox-one"), row(4, "Gears")],
             cache=cache, budget=2)
    assert [r.ta_id for r in p.lookups] == [1, 2] and p.backlog == 2 and not p.cooling
    p = plan([row(1, "Halo")], [entry("Halo", "switch", slug="halo")], cache=cache)
    assert [r.ta_id for r in p.lookups] == [1]  # a non-Xbox entry on the share page proves nothing


def test_unsettled_guard_comes_from_any_row_and_an_override_slug_goes_to_the_step():
    guard = {"status": "partial", "slug": "halo-x", "checked": ago(1)}
    rows = [row(1, "Halo", "xbox-series-x-s"), row(2, "Halo", "xbox-one")]
    p = plan(rows, cache={"2": guard})  # recorded on the One row; the Series row represents the game
    assert [r.ta_id for r in p.lookups] == [1] and p.unsettled == {1: guard} and not p.slugs
    # An override slug (the same item or another) is resolved by the step, whose item-keyed guard decides.
    for slug in ("halo-x", "halo-new"):
        p = plan(rows, overrides={2: {"deku": slug}}, cache={"2": guard})
        assert not p.unsettled and p.slugs == {1: slug} and [r.ta_id for r in p.lookups] == [1]
    p = plan(rows, overrides={1: {"deku": "skip"}}, cache={"2": guard})
    assert not p.lookups and len(p.skipped_override) == 2

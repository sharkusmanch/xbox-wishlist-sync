"""The Deku collection step (TA "Owned" -> Deku collection, add-only), driven through `run()` with fakes."""

import json
import logging
from datetime import timedelta

import pytest

from test_run import NOW, SECRETS, FakeDeku, FakeTA, Harness, basic
from wsync.deku import AuthError as DekuAuthError
from wsync.deku import DekuError, DekuNetworkError, DekuPartialOwn, DekuRateLimited, OwnedDetail
from wsync.errors import TAError
from wsync.run import Terminated
from wsync.state import load_state, save_state, State
from wsync.ta import IncompleteRead
from wsync.ta_coll import OwnedRow

SX, ONE, X360 = "xbox-series-x-s", "xbox-one", "xbox-360"


def row(ta_id, title, platform=SX, media="Digital", ownership="Owned"):
    return OwnedRow(ta_id, title, platform, ownership, media)


def only_collection(tmp_path, games, rows, *, dry_run=True, collection_dry_run=True, overrides=None, **cfg):
    """A `--only collection` run: no feed, no wishlists."""
    deku = FakeDeku(games, {})
    ta = FakeTA({}, {})
    ta.owned_rows = list(rows)
    h = Harness(tmp_path, records=None, deku=deku, ta=ta, overrides=overrides, lists=(), collection=True,
                dry_run=dry_run, collection_dry_run=collection_dry_run, **cfg)
    return h


def live(tmp_path, games, rows, **kw):
    return only_collection(tmp_path, games, rows, dry_run=False, collection_dry_run=False, **kw)


GAMES = {
    "alpha-quest": ("Alpha Quest", {"xbox_series", "xbox_one"}),
    "beta-saga": ("Beta Saga", {"xbox_one"}),
    "gamma-tale": ("Gamma Tale", {"xbox_series"}),
}


def owns(h):
    return [w for w in h.deku.writes if w[0] == "own"]


def cache(h):
    return load_state(h.cfg.state).collection


def unconfirmed(h):
    return load_state(h.cfg.state).collection_unconfirmed


# --------------------------------------------------------------------------- dry run / live


def test_dry_run_makes_no_own_calls_but_upserts_the_review_task(tmp_path):
    h = only_collection(tmp_path, GAMES, [row(1, "Alpha Quest", media="Disc"), row(2, "Beta Saga", ONE)])
    assert h.run() == 0
    assert owns(h) == [] and ("own", "alpha-quest") not in h.deku.calls
    body, dry = h.reporter.collection_reviews[-1]
    assert dry is True
    assert "Alpha Quest (`alpha-quest`, physical, xbox_series)" in body
    assert "Beta Saga (`beta-saga`, digital, xbox_one)" in body
    assert [t for t, _ in h.reporter.notes] == ["Deku collection plan ready"]
    c = cache(h)
    assert c["1"]["status"] == "found" and c["1"]["slug"] == "alpha-quest"
    assert c["1"]["checked"] == NOW.isoformat()


def test_unchanged_dry_run_plan_pings_once(tmp_path):
    h = only_collection(tmp_path, GAMES, [row(1, "Alpha Quest")])
    assert h.run() == 0 and h.run() == 0
    assert [t for t, _ in h.reporter.notes] == ["Deku collection plan ready"]
    assert len(h.reporter.collection_reviews) == 2


def test_dry_run_matches_are_not_looked_up_again_and_a_live_run_reuses_them(tmp_path):
    h = only_collection(tmp_path, GAMES, [row(1, "Alpha Quest")])
    assert h.run() == 0
    h.deku.calls.clear()
    assert h.run() == 0
    assert [c for c in h.deku.calls if c[0] != "sleep"] == [("collection",)]
    h.cfg.dry_run = h.cfg.collection_dry_run = False
    h.deku.calls.clear()
    assert h.run() == 0
    assert not [c for c in h.deku.calls if c[0] == "search"]  # the cached slug, re-read and re-checked
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]


def test_overall_dry_run_forces_the_step_dry(tmp_path):
    h = only_collection(tmp_path, GAMES, [row(1, "Alpha Quest")], dry_run=True, collection_dry_run=False)
    assert h.run() == 0
    assert owns(h) == []
    assert h.reporter.collection_reviews[-1][1] is True


def test_live_adds_call_own_with_mapped_format_and_platform(tmp_path):
    rows = [row(1, "Alpha Quest", media="Disc"), row(2, "Beta Saga", ONE, media="Disc-to-Digital"),
            row(3, "Gamma Tale", media="")]
    h = live(tmp_path, GAMES, rows)
    assert h.run() == 0
    assert sorted(owns(h)) == [("own", "alpha-quest", "physical", "xbox_series"),
                               ("own", "beta-saga", "digital", "xbox_one"),
                               ("own", "gamma-tale", None, "xbox_series")]
    assert {k: v["status"] for k, v in cache(h).items()} == {"1": "added", "2": "added", "3": "added"}
    assert h.reporter.collection_reviews[-1] == (None, False)  # the review task is closed
    assert [(t, b.splitlines()[0]) for t, b in h.reporter.notes] == [("Deku collection", "+3 Deku collection")]
    # Next run: all three are on the collection page now; nothing is looked up or added.
    h.deku.calls.clear()
    h.reporter.notes.clear()
    assert h.run() == 0
    assert [c for c in h.deku.calls if c[0] != "sleep"] == [("collection",)]
    assert h.reporter.notes == []


def test_series_and_one_rows_of_one_game_make_one_series_add(tmp_path):
    h = live(tmp_path, GAMES, [row(5, "Alpha Quest", ONE), row(4, "Alpha Quest", SX)])
    assert h.run() == 0
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]
    c = cache(h)
    assert c["4"]["status"] == "added" and c["5"]["status"] == "added"  # the duplicate shares it


def test_non_target_platforms_and_not_owned_rows_make_no_request(tmp_path):
    rows = [row(1, "Alpha Quest", X360), row(2, "Beta Saga", "windows"),
            row(3, "Gamma Tale", ownership="Xbox Game Pass")]
    h = live(tmp_path, GAMES, rows)
    assert h.run() == 0
    assert [c for c in h.deku.calls if c[0] != "sleep"] == [("collection",)]
    assert owns(h) == []


# --------------------------------------------------------------------------- item page decides


@pytest.mark.parametrize("platform", ["xbox_one", "xbox_series", None])
def test_existing_xbox_or_unset_entry_on_the_item_page_is_already(tmp_path, platform):
    # Titled differently on the share page, so only the item page shows it.
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.owned["alpha-quest"] = [OwnedDetail("o1", platform, "digital")]
    h.deku.collection = lambda: []
    assert h.run() == 0
    assert owns(h) == []
    assert cache(h)["1"]["status"] == "matched" and cache(h)["1"]["slug"] == "alpha-quest"


def test_switch_only_entry_gets_a_separate_xbox_entry_and_is_left_alone(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    switch = OwnedDetail("o-switch", "switch", "physical")
    h.deku.owned["alpha-quest"] = [switch]
    assert h.run() == 0
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]
    assert h.deku.owned["alpha-quest"][0] == switch


def test_series_row_whose_item_offers_only_xbox_one_is_added_as_xbox_one(tmp_path):
    # TA's platform is the console it was played on: an Xbox One game played on a Series X.
    h = live(tmp_path, GAMES, [row(1, "Beta Saga", SX, media="Disc")])  # Deku: Xbox One only
    assert h.run() == 0
    assert owns(h) == [("own", "beta-saga", "physical", "xbox_one")]
    assert cache(h)["1"]["status"] == "added" and cache(h)["1"]["platform"] == "xbox_one"


def test_one_row_whose_item_offers_only_xbox_series_is_added_as_xbox_series(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Gamma Tale", ONE)])  # Deku: Series only
    assert h.run() == 0
    assert owns(h) == [("own", "gamma-tale", "digital", "xbox_series")]


@pytest.mark.parametrize("platform, want", [(SX, "xbox_series"), (ONE, "xbox_one")])
def test_item_offering_both_uses_the_ta_platform(tmp_path, platform, want):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest", platform)])
    assert h.run() == 0
    assert owns(h) == [("own", "alpha-quest", "digital", want)]


def test_dry_run_lists_the_chosen_platform(tmp_path):
    h = only_collection(tmp_path, GAMES, [row(1, "Beta Saga", SX)])
    assert h.run() == 0
    assert "Beta Saga (`beta-saga`, digital, xbox_one)" in h.reporter.collection_reviews[-1][0]


def test_item_without_any_xbox_platform_is_not_added(tmp_path):
    games = {**GAMES, "beta-saga": ("Beta Saga", {"switch", "ps5"})}
    h = live(tmp_path, games, [row(1, "Beta Saga", SX)])
    assert h.run() == 0
    assert owns(h) == []
    assert cache(h)["1"]["status"] == "none"


def test_live_run_re_verifies_a_dry_run_match_on_the_fresh_item_page(tmp_path):
    h = only_collection(tmp_path, GAMES, [row(1, "Alpha Quest"), row(3, "Gamma Tale")])
    assert h.run() == 0
    assert {cache(h)[k]["status"] for k in ("1", "3")} == {"found"}
    # Before the live run, Alpha Quest gets an Xbox entry that the share page doesn't show.
    h.deku.owned["alpha-quest"] = [OwnedDetail("o-hidden", "xbox_one", "digital")]
    h.deku.collection = lambda: []
    h.cfg.dry_run = h.cfg.collection_dry_run = False
    h.deku.calls.clear()
    assert h.run() == 0
    assert ("item", "alpha-quest") in h.deku.calls and ("item", "gamma-tale") in h.deku.calls
    assert owns(h) == [("own", "gamma-tale", "digital", "xbox_series")]  # found = needs an add
    assert cache(h)["1"]["status"] == "matched" and cache(h)["3"]["status"] == "added"


def test_no_confident_match_is_cached_with_checked_and_skipped_for_a_week(tmp_path):
    games = {**GAMES, "delta-one": ("Delta Force", {"xbox_series"}), "delta-two": ("Delta Force", {"xbox_series"})}
    h = live(tmp_path, games, [row(1, "Delta Force"), row(2, "Unknown Thing")])
    assert h.run() == 0
    c = cache(h)
    assert c["1"]["status"] == "none" and c["2"]["status"] == "none"
    assert c["1"]["checked"] == NOW.isoformat() and c["1"]["slug"] is None
    h.deku.searches.clear()
    h.now = NOW + timedelta(days=6)
    assert h.run() == 0
    assert h.deku.searches == []
    h.now = NOW + timedelta(days=8)
    assert h.run() == 0
    assert "Delta Force" in h.deku.searches and "Unknown Thing" in h.deku.searches


def test_search_falls_back_to_the_title_without_edition_noise(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Gamma Tale Deluxe Edition")])
    h.deku.results = {"Gamma Tale Deluxe Edition": []}
    assert h.run() == 0
    assert h.deku.searches == ["Gamma Tale Deluxe Edition", "Gamma Tale"]
    assert owns(h) == [("own", "gamma-tale", "digital", "xbox_series")]


def test_override_slug_and_skip(tmp_path):
    games = {**GAMES, "alpha-quest-remastered": ("Alpha Quest Remastered", {"xbox_series"})}
    ov = {"collection": {"1": {"deku": "alpha-quest-remastered"}, "2": {"deku": "skip"}}}
    h = live(tmp_path, games, [row(1, "Alpha Quest"), row(2, "Gamma Tale")], overrides=ov)
    assert h.run() == 0
    assert h.deku.searches == []
    assert owns(h) == [("own", "alpha-quest-remastered", "digital", "xbox_series")]
    assert "2" not in cache(h)


# --------------------------------------------------------------------------- budget and pacing


def test_budget_of_25_lookups_and_pacing_client(tmp_path):
    games = {f"game-{i:02d}": (f"Game {i:02d}", {"xbox_series"}) for i in range(30)}
    rows = [row(100 + i, f"Game {i:02d}") for i in range(30)]
    h = only_collection(tmp_path, games, rows)
    assert h.run() == 0
    assert h.deku.paced_with == [4.0]
    assert len(h.deku.searches) == 25
    calls = h.deku.calls
    for n, c in enumerate(calls):
        if c[0] in ("collection", "item"):
            assert calls[n - 1] == ("sleep", 4.0), c
    body = h.reporter.collection_reviews[-1][0]
    assert "Waiting for a lookup: 5" in body
    h.deku.searches.clear()
    assert h.run() == 0
    assert len(h.deku.searches) == 5  # the rest, then nothing left


# --------------------------------------------------------------------------- failures


def test_ta_incomplete_read_adds_nothing_and_fails_the_run(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.ta.fail_collection = IncompleteRead("read 90 of 100 collection titles")
    assert h.run() == 1
    assert owns(h) == [] and h.deku.calls == []
    st = load_state(h.cfg.state).ta_collection
    assert "rows" not in st and "fetched" not in st and st["last_attempt"] == NOW.isoformat()
    assert any("failed" in t.lower() and "TrueAchievements collection" in b for t, b in h.reporter.notes)


def test_deku_collection_incomplete_adds_nothing_and_fails_the_run(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_collection = DekuError("read 80 collection entries, the page says 90")
    assert h.run() == 1
    assert owns(h) == [] and h.deku.searches == []
    assert any("failed" in t.lower() and "Deku Deals collection" in b for t, b in h.reporter.notes)


@pytest.mark.parametrize("exc", [DekuRateLimited("GET /collection/<id> answered HTTP 429"),
                                 DekuNetworkError("network error: TimeoutError")])
def test_deku_collection_read_rate_limited_or_offline_stops_quietly(tmp_path, exc):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_collection = exc
    assert h.run() == 0
    assert owns(h) == [] and h.deku.searches == []
    assert h.reporter.notes == []


@pytest.mark.parametrize("exc", [DekuRateLimited("GET /items/gamma-tale answered HTTP 429"),
                                 DekuNetworkError("network error: TimeoutError")])
def test_rate_limit_mid_step_stops_further_requests_exit_0(tmp_path, exc):
    rows = [row(1, "Alpha Quest"), row(2, "Beta Saga", ONE), row(3, "Gamma Tale")]
    h = live(tmp_path, GAMES, rows)  # lookup order: Series rows first (1, 3), then One (2)
    h.deku.fail_item["gamma-tale"] = exc
    assert h.run() == 0
    requests = [c for c in h.deku.calls if c[0] != "sleep"]
    assert requests[-1] == ("item", "gamma-tale")  # nothing after the 429
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]
    assert "2" not in cache(h) and "3" not in cache(h)  # retried next run, not "none"
    assert not any("failed" in t.lower() for t, _ in h.reporter.notes)


def test_partial_own_is_a_failure_naming_the_game_and_stops_the_step(tmp_path):
    rows = [row(1, "Alpha Quest"), row(3, "Gamma Tale")]
    h = live(tmp_path, GAMES, rows)
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("'alpha-quest': collection add failed after the create")
    assert h.run() == 1
    assert ("search", "Gamma Tale") not in h.deku.calls
    notes = [b for t, b in h.reporter.notes if "failed" in t.lower()]
    assert notes and "Alpha Quest" in notes[0]
    c = cache(h)["1"]
    assert c["status"] == "partial" and c["slug"] == "alpha-quest" and c["baseline"] == []
    u = unconfirmed(h)
    assert set(u) == {"alpha-quest"} and u["alpha-quest"]["titles"] == ["Alpha Quest"]
    assert u["alpha-quest"]["since"] == NOW.isoformat()
    # Next run: no second own() for that game; a hand check is asked for, and the step stops there.
    h.deku.fail_own.clear()
    h.deku.calls.clear()
    assert h.run() == 1
    assert ("own", "alpha-quest") not in h.deku.calls and ("item", "alpha-quest") in h.deku.calls
    assert ("search", "Gamma Tale") not in h.deku.calls
    assert any("by hand" in i for i in h.last_issues())
    assert cache(h)["1"]["status"] == "partial"


def test_partial_add_that_left_an_xbox_entry_is_recorded_as_added(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("read-back differs")
    assert h.run() == 1
    h.deku.fail_own.clear()
    h.deku.owned["alpha-quest"] = [OwnedDetail("o-new", "xbox_series", None)]  # finished by hand
    h.deku.collection = lambda: []  # (hidden from the share page)
    assert h.run() == 0
    assert cache(h)["1"]["status"] == "added" and not owns(h)


def test_sigterm_during_own_leaves_adding_in_state_and_is_never_repeated(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.owned["alpha-quest"] = [OwnedDetail("o-switch", "switch", "physical")]

    def created_then_killed(item, fmt, platform):
        h.deku.calls.append(("own", item.slug))
        h.deku.owned["alpha-quest"].append(OwnedDetail("o-bare", "switch", None))  # default platform
        raise Terminated()

    h.deku.own = created_then_killed
    assert h.run() == 1
    c = cache(h)["1"]
    assert c["status"] == "adding" and c["baseline"] == ["o-switch"] and c["platform"] == "xbox_series"
    assert set(unconfirmed(h)) == {"alpha-quest"}
    h.deku.calls.clear()
    assert h.run() == 1
    assert ("own", "alpha-quest") not in h.deku.calls
    assert any("by hand" in i and "Alpha Quest" in i for i in h.last_issues())


def test_write_ahead_is_on_disk_before_own(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    real_own = h.deku.own
    seen = {}

    def own(*a):
        on_disk = load_state(h.cfg.state)
        seen["status"] = on_disk.collection["1"]["status"]
        seen["unconfirmed"] = set(on_disk.collection_unconfirmed)
        return real_own(*a)

    h.deku.own = own
    assert h.run() == 0
    assert seen == {"status": "adding", "unconfirmed": {"alpha-quest"}}
    assert cache(h)["1"]["status"] == "added" and unconfirmed(h) == {}  # read back: confirmed


def test_failed_write_ahead_is_rolled_back_and_nothing_is_added(tmp_path, monkeypatch):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])

    def broken(self):
        raise OSError("disk full")

    monkeypatch.setattr("wsync.run._Run.checkpoint", broken)
    assert h.run() == 1
    assert not owns(h) and ("own", "alpha-quest") not in h.deku.calls
    assert unconfirmed(h) == {} and "1" not in cache(h)
    assert any("not saved before adding" in i for i in h.last_issues())


def test_skip_override_resolves_a_partial_add(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("boom")
    assert h.run() == 1
    (tmp_path / "overrides.json").write_text(json.dumps({"collection": {"1": {"deku": "skip"}}}))
    h.deku.calls.clear()
    assert h.run() == 0
    assert [c for c in h.deku.calls if c[0] != "sleep"] == [("collection",)]


@pytest.mark.parametrize("exc", [DekuError("GET /items/alpha-quest answered HTTP 500"),
                                 DekuAuthError("Deku Deals session not accepted")])
def test_other_deku_error_during_lookups_is_a_failure_and_stops_the_step(tmp_path, exc):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest"), row(3, "Gamma Tale")])
    h.deku.fail_item["alpha-quest"] = exc
    assert h.run() == 1
    assert ("search", "Gamma Tale") not in h.deku.calls
    assert owns(h) == []


def test_invalid_collection_overrides_fail_only_the_collection_step(tmp_path):
    h = basic(tmp_path, dry_run=False, collection=True, collection_dry_run=False,
              overrides={"collection": {"abc": {"deku": "x"}}})
    h.ta.owned_rows = [row(1, "Alpha Quest")]
    assert h.run() == 1
    assert [w[0] for w in h.deku.writes] == ["add", "update", "remove"]  # the wishlist was synced
    assert not owns(h) and h.ta.collection_calls == 0


# --------------------------------------------------------------------------- TA read cache


def test_ta_collection_is_read_at_most_once_a_day(tmp_path):
    h = only_collection(tmp_path, GAMES, [row(1, "Alpha Quest")])
    assert h.run() == 0 and h.ta.collection_calls == 1
    h.now = NOW + timedelta(hours=23)
    assert h.run() == 0 and h.ta.collection_calls == 1
    h.now = NOW + timedelta(hours=25)
    assert h.run() == 0 and h.ta.collection_calls == 2
    st = load_state(h.cfg.state).ta_collection
    assert st["fetched"] == (NOW + timedelta(hours=25)).isoformat()
    assert st["rows"][0]["title"] == "Alpha Quest"


# --------------------------------------------------------------------------- wiring


def test_only_collection_skips_the_wishlist_and_needs_no_feed(tmp_path):
    h = only_collection(tmp_path, GAMES, [row(1, "Alpha Quest")])
    assert not h.cfg.feed.exists()
    assert h.run() == 0
    assert h.deku.wishlist_calls == 0 and h.ta.wishlist_calls == 0
    assert h.reporter.reviews == [] and h.reporter.problem_syncs == []


def test_step_runs_after_the_wishlist_step_and_never_affects_it(tmp_path):
    h = basic(tmp_path, dry_run=False, collection=True, collection_dry_run=False)
    h.ta.owned_rows = [row(1, "Gamma Tale")]
    h.deku.games["gamma-tale"] = ("Gamma Tale", {"xbox_series"})
    assert h.run() == 0
    kinds = [j[1] for j in h.journal if j[0] == "deku"]
    assert kinds == ["add", "update", "remove", "own"]
    assert load_state(h.cfg.state).last_feed_count == 2


def test_unexpected_error_in_the_step_is_a_failure_after_the_wishlist_completed(tmp_path):
    h = basic(tmp_path, dry_run=False, collection=True, collection_dry_run=False)
    h.ta.fail_collection = RuntimeError("boom")
    assert h.run() == 1
    assert [w[0] for w in h.deku.writes] == ["add", "update", "remove"]
    assert h.ta.writes == [("add", "101"), ("add", "102"), ("remove", "999")]
    assert any("Deku collection" in b for t, b in h.reporter.notes if "failed" in t.lower())


def test_step_not_started_past_the_write_deadline(tmp_path):
    h = basic(tmp_path, dry_run=False, collection=True, collection_dry_run=False, write_deadline=100.0)
    h.ta.owned_rows = [row(1, "Alpha Quest")]
    real_add = h.deku.add

    def slow_add(*a):
        h.clock += 200
        return real_add(*a)

    h.deku.add = slow_add
    assert h.run() == 0
    assert ("collection",) not in h.deku.calls and h.ta.collection_calls == 0


def test_deadline_rechecked_just_before_own(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")], write_deadline=1000.0)
    real_item = h.deku.item

    def slow_item(slug):
        h.clock += 2000
        return real_item(slug)

    h.deku.item = slow_item
    assert h.run() == 0
    assert owns(h) == [] and "1" not in cache(h)  # looked up again next run


def test_hidden_or_hand_deleted_entry_is_rechecked_weekly_and_re_added(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    assert h.run() == 0 and len(owns(h)) == 1
    h.deku.owned.clear()  # deleted by hand on Deku
    h.deku.calls.clear()
    h.now = NOW + timedelta(days=1)
    assert h.run() == 0
    assert [c for c in h.deku.calls if c[0] != "sleep"] == [("collection",)]
    h.now = NOW + timedelta(days=8)
    assert h.run() == 0
    assert len(owns(h)) == 2  # TA is the source of truth: added again


def test_deadline_between_lookups_stops_the_step(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest"), row(3, "Gamma Tale")], write_deadline=1000.0)
    real_own = h.deku.own

    def slow_own(*a):
        h.clock += 2000
        return real_own(*a)

    h.deku.own = slow_own
    assert h.run() == 0
    assert [w[1] for w in owns(h)] == ["alpha-quest"]
    assert ("search", "Gamma Tale") not in h.deku.calls


def test_secrets_and_collection_id_stay_out_of_the_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_collection = DekuError(f"GET /collection/{SECRETS['deku_wishlist_id']} answered HTTP 500")
    assert h.run() == 1
    text = caplog.text + json.dumps(h.reporter.notes)
    for value in SECRETS.values():
        assert value not in text


def test_review_body_lists_unsettled_adds_for_a_hand_check():
    from wsync.collection import plan_collection, review_body

    rows = [row(1, "Alpha Quest"), row(2, "Beta Saga", ONE)]
    cache = {"1": {"status": "partial", "slug": "alpha-quest", "checked": NOW.isoformat()},
             "2": {"status": "found", "slug": "beta-saga", "format": "digital", "platform": "xbox_one",
                   "checked": NOW.isoformat()}}
    unconfirmed = {"alpha-quest": {"since": NOW.isoformat(), "titles": ["Alpha Quest"]},
                   "aq-old": {"since": NOW.isoformat(), "titles": ["Alpha Quest"]}}  # an earlier override's item
    body, counts = review_body(rows, plan_collection(rows, [], {}, cache, NOW, dry_run=True), cache, unconfirmed)
    assert "## Check by hand (2)\n" in body and "- Alpha Quest (`alpha-quest`)" in body
    assert "- Alpha Quest (`aq-old`)" in body
    assert counts["check"] == 2 and counts["would_add"] == 1 and counts["waiting"] == 0


def _partial_via_override(tmp_path, slug):
    games = {**GAMES, "alpha-quest-remastered": ("Alpha Quest Remastered", {"xbox_series"})}
    h = live(tmp_path, games, [row(1, "Alpha Quest")], overrides={"collection": {"1": {"deku": slug}}})
    h.deku.fail_own[slug] = DekuPartialOwn("read-back differs")
    assert h.run() == 1
    assert cache(h)["1"]["status"] == "partial" and set(unconfirmed(h)) == {slug}
    h.deku.fail_own.clear()
    h.deku.calls.clear()
    return h


def test_same_override_slug_does_not_repeat_a_partial_add(tmp_path):
    h = _partial_via_override(tmp_path, "alpha-quest-remastered")
    assert h.run() == 1
    assert not owns(h) and ("item", "alpha-quest-remastered") in h.deku.calls
    assert any("by hand" in i for i in h.last_issues())


def test_a_different_override_slug_is_a_new_decision(tmp_path):
    h = _partial_via_override(tmp_path, "alpha-quest-remastered")
    (tmp_path / "overrides.json").write_text(json.dumps({"collection": {"1": {"deku": "alpha-quest"}}}))
    assert h.run() == 0
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]
    assert set(unconfirmed(h)) == {"alpha-quest-remastered"}  # the earlier item stays guarded


def test_skip_override_settles_a_partial_add_made_through_an_override(tmp_path):
    h = _partial_via_override(tmp_path, "alpha-quest-remastered")
    (tmp_path / "overrides.json").write_text(json.dumps({"collection": {"1": {"deku": "skip"}}}))
    assert h.run() == 0
    assert not owns(h) and [c for c in h.deku.calls if c[0] != "sleep"] == [("collection",)]


def test_partial_guard_survives_a_new_representative_row(tmp_path):
    h = live(tmp_path, GAMES, [row(5, "Alpha Quest", ONE)])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("read-back differs")
    assert h.run() == 1
    h.deku.fail_own.clear()
    h.deku.calls.clear()
    h.ta.owned_rows = [row(4, "Alpha Quest", SX), row(5, "Alpha Quest", ONE)]  # a Series row appears
    h.now = NOW + timedelta(days=2)  # past the 24 h TA cache
    assert h.run() == 1
    assert ("own", "alpha-quest") not in h.deku.calls
    assert any("by hand" in i for i in h.last_issues())


def test_write_ahead_and_partial_are_stored_on_every_row_of_the_game(tmp_path):
    h = live(tmp_path, GAMES, [row(4, "Alpha Quest", SX), row(5, "Alpha Quest", ONE)])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("boom")
    assert h.run() == 1
    assert cache(h)["4"]["status"] == cache(h)["5"]["status"] == "partial"
    h.deku.fail_own.clear()
    h.ta.owned_rows = [row(5, "Alpha Quest", ONE)]  # the Series row leaves Owned
    h.now = NOW + timedelta(days=2)
    h.deku.calls.clear()
    assert h.run() == 1 and ("own", "alpha-quest") not in h.deku.calls


def _override(tmp_path, slug):
    (tmp_path / "overrides.json").write_text(json.dumps({"collection": {"1": {"deku": slug}}}))


def test_override_redirecting_to_an_attempted_item_does_not_repeat_the_add(tmp_path):
    h = _partial_via_override(tmp_path, "alpha-quest-remastered")
    h.deku.redirects["aq-remastered-2021"] = "alpha-quest-remastered"
    _override(tmp_path, "aq-remastered-2021")
    assert h.run() == 1
    assert ("item", "aq-remastered-2021") in h.deku.calls and not owns(h)
    assert any("by hand" in i for i in h.last_issues())
    assert set(unconfirmed(h)) == {"alpha-quest-remastered"}


def test_switching_back_to_an_earlier_failed_override_does_not_re_add_it(tmp_path):
    games = {**GAMES, "alpha-quest-remastered": ("Alpha Quest Remastered", {"xbox_series"})}
    h = live(tmp_path, games, [row(1, "Alpha Quest")])
    for slug in ("alpha-quest-remastered", "alpha-quest"):  # X fails, then Y fails
        _override(tmp_path, slug)
        h.deku.fail_own[slug] = DekuPartialOwn("read-back differs")
        assert h.run() == 1
    assert set(unconfirmed(h)) == {"alpha-quest-remastered", "alpha-quest"}
    h.deku.fail_own.clear()
    h.deku.calls.clear()
    _override(tmp_path, "alpha-quest-remastered")  # back to X
    assert h.run() == 1
    assert not owns(h) and ("item", "alpha-quest-remastered") in h.deku.calls
    assert any("by hand" in i for i in h.last_issues())
    _override(tmp_path, "alpha-quest")  # and to Y again
    h.deku.calls.clear()
    assert h.run() == 1 and not owns(h) and ("item", "alpha-quest") in h.deku.calls


def test_a_confirmed_add_leaves_no_attempt_history_so_a_hand_deleted_entry_is_re_added(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("boom")
    assert h.run() == 1
    h.deku.fail_own.clear()
    h.deku.owned["alpha-quest"] = [OwnedDetail("o-fixed", "xbox_series", None)]  # fixed by hand
    h.deku.collection = lambda: []  # not on the share page: the item page settles it
    assert h.run() == 0 and cache(h)["1"]["status"] == "added" and unconfirmed(h) == {}
    h.deku.owned.clear()  # later deleted by hand
    h.now = NOW + timedelta(days=8)
    assert h.run() == 0
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]


def test_a_row_that_joins_while_unsettled_and_stays_alone_never_re_adds_the_item(tmp_path):
    """Round-3 repro: row 5 partial on alpha-quest, row 4 joins, row 5 leaves Owned."""
    h = live(tmp_path, GAMES, [row(5, "Alpha Quest", ONE)])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("read-back differs")
    assert h.run() == 1
    h.deku.fail_own.clear()
    h.ta.owned_rows = [row(4, "Alpha Quest", SX), row(5, "Alpha Quest", ONE)]  # row 4 joins
    h.now = NOW + timedelta(days=2)
    assert h.run() == 1 and not owns(h)
    h.ta.owned_rows = [row(4, "Alpha Quest", SX)]  # row 5 leaves Owned
    h.now = NOW + timedelta(days=4)
    h.deku.calls.clear()
    assert h.run() == 1
    assert ("own", "alpha-quest") not in h.deku.calls and ("item", "alpha-quest") in h.deku.calls
    assert any("by hand" in i for i in h.last_issues())
    assert set(unconfirmed(h)) == {"alpha-quest"}


def test_item_guard_holds_without_any_row_record(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("boom")
    assert h.run() == 1
    h.deku.fail_own.clear()
    st = load_state(h.cfg.state)
    st.collection.clear()  # no row-level record left at all
    save_state(h.cfg.state, st)
    h.deku.calls.clear()
    assert h.run() == 1 and ("own", "alpha-quest") not in h.deku.calls
    assert any("by hand" in i for i in h.last_issues())


def test_another_game_led_to_an_unconfirmed_item_does_not_add_it(tmp_path):
    games = {**GAMES, "omega-run": ("Omega Run", {"xbox_series"})}
    h = live(tmp_path, games, [row(1, "Alpha Quest")])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("boom")
    assert h.run() == 1
    h.deku.fail_own.clear()
    h.ta.owned_rows = [row(1, "Alpha Quest"), row(2, "Omega Run")]
    h.now = NOW + timedelta(days=2)
    _override_all(tmp_path, {"1": {"deku": "skip"}, "2": {"deku": "alpha-quest"}})
    h.deku.calls.clear()
    assert h.run() == 1 and ("own", "alpha-quest") not in h.deku.calls
    assert any("by hand" in i and "Omega Run" in i for i in h.last_issues())


def test_an_unconfirmed_item_confirmed_through_any_path_leaves_the_guard(tmp_path):
    h = live(tmp_path, GAMES, [row(5, "Alpha Quest", ONE)])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("boom")
    assert h.run() == 1
    h.deku.fail_own.clear()
    st = load_state(h.cfg.state)
    st.collection.clear()
    save_state(h.cfg.state, st)
    h.deku.owned["alpha-quest"] = [OwnedDetail("o-fixed", "xbox_one", None)]  # set to Xbox by hand
    h.deku.collection = lambda: []  # hidden from the share page
    assert h.run() == 0 and not owns(h)
    assert unconfirmed(h) == {} and cache(h)["5"]["status"] == "added"


def _override_all(tmp_path, collection):
    (tmp_path / "overrides.json").write_text(json.dumps({"collection": collection}))


# --------------------------------------------------------------------------- the share page settles a guard


def test_a_hand_fixed_partial_add_visible_on_the_share_page_leaves_the_guard(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("boom")
    assert h.run() == 1
    h.deku.fail_own.clear()
    h.deku.owned["alpha-quest"] = [OwnedDetail("o-fixed", "xbox_series", "digital")]  # fixed by hand, visible
    h.deku.calls.clear()
    assert h.run() == 0
    assert unconfirmed(h) == {}
    c = cache(h)["1"]
    assert c["status"] == "added" and c["slug"] == "alpha-quest" and c["platform"] == "xbox_series"
    assert [c for c in h.deku.calls if c[0] != "sleep"] == [("collection",)]  # settled at no request cost
    assert not owns(h)


def test_a_hand_fixed_override_partial_visible_on_the_share_page_leaves_the_guard(tmp_path):
    h = _partial_via_override(tmp_path, "alpha-quest-remastered")
    h.deku.owned["alpha-quest-remastered"] = [OwnedDetail("o-fixed", None, None)]  # unset platform counts
    assert h.run() == 0
    assert unconfirmed(h) == {} and cache(h)["1"]["status"] == "added"
    assert [c for c in h.deku.calls if c[0] != "sleep"] == [("collection",)] and not owns(h)


def test_a_switch_entry_on_the_share_page_does_not_settle_the_guard(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("boom")
    assert h.run() == 1
    h.deku.fail_own.clear()
    h.deku.owned["alpha-quest"] = [OwnedDetail("o-sw", "switch", "digital")]
    assert h.run() == 1
    assert set(unconfirmed(h)) == {"alpha-quest"} and cache(h)["1"]["status"] == "partial"


def test_dry_run_review_drops_a_guard_the_share_page_settles(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("boom")
    assert h.run() == 1
    h.deku.fail_own.clear()
    h.deku.owned["alpha-quest"] = [OwnedDetail("o-fixed", "xbox_series", None)]
    h.cfg.dry_run = h.cfg.collection_dry_run = True
    assert h.run() == 0
    body, _ = h.reporter.collection_reviews[-1]
    assert "Check by hand" not in body and unconfirmed(h) == {}


# --------------------------------------------------------------------------- deadline, back-off, cooldown, review ping


def test_no_fresh_ta_read_with_less_than_ten_minutes_before_the_write_deadline(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")], write_deadline=599.0)
    assert h.run() == 0
    assert h.ta.collection_calls == 0 and [c for c in h.deku.calls if c[0] != "sleep"] == []
    assert h.reporter.notes == [] and load_state(h.cfg.state).ta_collection == {}


def test_a_fresh_ta_read_still_starts_with_ten_minutes_left(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")], write_deadline=601.0)
    assert h.run() == 0 and h.ta.collection_calls == 1
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]


def test_a_cached_ta_read_runs_the_step_close_to_the_deadline(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    st = State(ta_collection={"fetched": NOW.isoformat(), "rows": [row(1, "Alpha Quest").__dict__]})
    save_state(h.cfg.state, st)
    h.cfg.write_deadline = 100.0
    assert h.run() == 0 and h.ta.collection_calls == 0
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]


def test_failed_ta_read_is_recorded_and_not_retried_for_six_hours(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.ta.fail_collection = IncompleteRead("read 90 of 100 collection titles")
    assert h.run() == 1 and h.ta.collection_calls == 1
    st = load_state(h.cfg.state).ta_collection
    assert st["last_attempt"] == NOW.isoformat() and "90 of 100" in st["error"]
    pings = len(h.reporter.notes)
    h.now = NOW + timedelta(hours=5)
    assert h.run() == 0 and h.ta.collection_calls == 1  # backing off: a warning, no new read, run succeeds
    assert len(h.reporter.notes) == pings  # no new ping
    assert owns(h) == []
    h.ta.fail_collection = None
    h.now = NOW + timedelta(hours=6, minutes=1)
    assert h.run() == 0 and h.ta.collection_calls == 2
    st = load_state(h.cfg.state).ta_collection
    assert "last_attempt" not in st and "error" not in st and st["fetched"] == h.now.isoformat()
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]


def test_override_slug_already_handled_cools_for_a_week(tmp_path):
    games = {**GAMES, "alpha-quest-remastered": ("Alpha Quest Remastered", {"switch"})}
    h = live(tmp_path, games, [row(1, "Alpha Quest")],
             overrides={"collection": {"1": {"deku": "alpha-quest-remastered"}}})
    assert h.run() == 0
    assert cache(h)["1"]["status"] == "none"  # the override's item lists no Xbox platform
    h.deku.calls.clear()
    h.now = NOW + timedelta(hours=1)
    assert h.run() == 0
    assert [c for c in h.deku.calls if c[0] != "sleep"] == [("collection",)]  # no hourly item GET
    h.now = NOW + timedelta(days=8)
    assert h.run() == 0 and ("item", "alpha-quest-remastered") in h.deku.calls


def test_override_matched_but_hidden_cools_for_a_week(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")], overrides={"collection": {"1": {"deku": "beta-saga"}}})
    h.deku.owned["beta-saga"] = [OwnedDetail("o-x", "xbox_one", None)]
    h.deku.collection = lambda: []  # hidden from the share page
    assert h.run() == 0 and cache(h)["1"]["status"] == "matched"
    h.deku.calls.clear()
    h.now = NOW + timedelta(hours=1)
    assert h.run() == 0 and ("item", "beta-saga") not in h.deku.calls


def test_a_new_override_slug_is_looked_up_despite_the_cooldown(tmp_path):
    games = {**GAMES, "alpha-quest-remastered": ("Alpha Quest Remastered", {"switch"})}
    h = live(tmp_path, games, [row(1, "Alpha Quest")],
             overrides={"collection": {"1": {"deku": "alpha-quest-remastered"}}})
    assert h.run() == 0 and cache(h)["1"]["status"] == "none"
    _override(tmp_path, "alpha-quest")
    h.now = NOW + timedelta(hours=1)
    assert h.run() == 0
    assert owns(h) == [("own", "alpha-quest", "digital", "xbox_series")]


def test_review_ping_ignores_the_moving_waiting_count(tmp_path):
    rows = [row(i, f"Game {i}") for i in range(1, 31)]  # 30 games: more than one run's budget
    h = only_collection(tmp_path, {}, rows)
    assert h.run() == 0
    assert [t for t, _ in h.reporter.notes] == ["Deku collection plan ready"]  # 25 unmatched
    assert "waiting for a lookup 5" in h.reporter.collection_reviews[-1][0]
    # A new owned game appears and Deku answers 429 before any lookup: only the waiting count moves.
    h.ta.owned_rows = rows + [row(31, "Game 31")]
    h.now = NOW + timedelta(hours=25)  # past the 24 h TA cache
    for i in range(26, 32):
        h.deku.fail_search[f"Game {i}"] = DekuRateLimited("429")
    assert h.run() == 0
    body = h.reporter.collection_reviews[-1][0]
    assert "waiting for a lookup 6" in body  # the task text shows the new count...
    assert [t for t, _ in h.reporter.notes] == ["Deku collection plan ready"]  # ...but no new ping
    # The lookups go through: the unmatched list changes, so it pings again.
    h.deku.fail_search.clear()
    h.now = NOW + timedelta(hours=26)
    assert h.run() == 0
    assert [t for t, _ in h.reporter.notes] == ["Deku collection plan ready"] * 2


def test_review_ping_not_sent_when_only_the_waiting_count_moves(tmp_path):
    from wsync.collection import review_signature
    from wsync.collection import CollectionPlan

    rows = [row(1, "Alpha Quest"), row(2, "Beta Saga", ONE)]
    cache_ = {"1": {"status": "found", "slug": "alpha-quest", "format": "digital", "platform": "xbox_series"},
              "2": {"status": "none"}}
    a = review_signature(rows, CollectionPlan(), cache_, {})
    b = review_signature(rows + [row(3, "Gamma Tale")], CollectionPlan(), cache_, {})  # one more waiting
    assert a == b
    c = review_signature(rows, CollectionPlan(), {**cache_, "2": {"status": "found", "slug": "beta-saga"}}, {})
    assert c != a
    d = review_signature(rows, CollectionPlan(), cache_, {"alpha-quest": {"titles": ["Alpha Quest"]}})
    assert d != a


def test_hand_check_message_says_whether_an_entry_may_exist(tmp_path):
    h = live(tmp_path, GAMES, [row(1, "Alpha Quest")])
    h.deku.fail_own["alpha-quest"] = DekuPartialOwn("429 on the create")  # no entry appeared
    assert h.run() == 1
    h.deku.fail_own.clear()
    assert h.run() == 1 and not owns(h)
    msg = " ".join(h.last_issues())
    assert "no new entry" in msg and "most likely left none" in msg and "add it by hand" in msg
    h2 = live(tmp_path / "b", GAMES, [row(1, "Alpha Quest")])
    h2.deku.fail_own["alpha-quest"] = DekuPartialOwn("read-back differs")
    assert h2.run() == 1
    h2.deku.fail_own.clear()
    h2.deku.owned["alpha-quest"] = [OwnedDetail("o-new", "switch", None)]  # left on the default platform
    assert h2.run() == 1 and not owns(h2)
    msg = " ".join(h2.last_issues())
    assert "left an entry (switch)" in msg and "set it to Xbox by hand" in msg

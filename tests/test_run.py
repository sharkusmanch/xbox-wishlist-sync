import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from wsync.deku import DekuError, DekuItem
from wsync.errors import TAError
from wsync.match import norm
from wsync.plan import Entry
from wsync.run import Config, Deps, config_from, run
from wsync.state import State, load_state, save_state
from wsync.ta import AlreadyListed, AuthError as TAAuthError, IncompleteRead, TAGame

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
SERIES, ONE = "Xbox Series X|S", "Xbox One"


# --------------------------------------------------------------------------- fakes


class FakeDeku:
    def __init__(self, games, wishlist, *, journal=None):
        # games: slug -> (title, offered platforms); wishlist: slug -> (format, ticked platforms)
        self.games = dict(games)
        self.list = dict(wishlist)
        self.writes = []
        self.searches = []
        self.journal = journal if journal is not None else []
        self.last_items = {}
        self.fail_read = None
        self.fail_search = {}
        self.results = {}  # query -> canned results
        self.wishlist_calls = 0

    def _item(self, slug):
        title, offered = self.games[slug]
        on = slug in self.list
        fmt, ticked = self.list.get(slug, ("", frozenset()))
        settings = {}
        if on:
            settings = {"desired_format": fmt, "desired_price": "12.34", "note": "keep me"}
            for p in ("xbox_series", "xbox_one", "switch"):
                settings[f"platform_{p}"] = "true" if p in ticked else "false"
        return DekuItem(slug, f"uuid-{slug}", title, on, frozenset(ticked) if on else frozenset(),
                        frozenset(offered), settings)

    def wishlist(self, *, with_platforms=False):
        self.wishlist_calls += 1
        if self.fail_read:
            raise self.fail_read
        out = {}
        for slug, (fmt, ticked) in self.list.items():
            out[slug] = Entry(slug, self.games[slug][0], fmt, frozenset(ticked) if with_platforms else frozenset())
            self.last_items[slug] = self._item(slug)
        return out

    def search(self, q):
        self.searches.append(q)
        if q in self.fail_search:
            raise self.fail_search[q]
        if q in self.results:
            return list(self.results[q])
        words = set(norm(q).split())  # a loose full-text search, like the real one
        return [(t, s) for s, (t, _) in self.games.items() if words & set(norm(t).split())]

    def item(self, slug):
        return self._item(slug)

    def add(self, item, fmt, platforms):
        assert not item.on_wishlist
        self.writes.append(("add", item.slug, fmt, frozenset(platforms)))
        self.journal.append(("deku", "add", item.slug))
        self.list[item.slug] = (fmt, frozenset(platforms))

    def update(self, item, fmt, platforms):
        assert item.on_wishlist and item.settings["note"] == "keep me"
        self.writes.append(("update", item.slug, fmt, frozenset(platforms)))
        self.journal.append(("deku", "update", item.slug))
        self.list[item.slug] = (fmt, frozenset(platforms))

    def remove(self, item):
        self.writes.append(("remove", item.slug))
        self.journal.append(("deku", "remove", item.slug))
        del self.list[item.slug]


class FakeTA:
    def __init__(self, pages, wishlist, *, journal=None, dlcs=None, game_ids=None):
        # pages: slug -> TAGame; wishlist: key -> title
        self.pages = dict(pages)
        self.list = dict(wishlist)
        self.dlcs = dict(dlcs or {})
        self.writes = []
        self.searches = []
        self.fetched = []
        self.journal = journal if journal is not None else []
        self.fail_read = None
        self.fail_reread = None
        self.already = set()
        self.game_ids = dict(game_ids or {})
        self.wishlist_calls = 0

    def wishlist(self):
        self.wishlist_calls += 1
        if self.fail_read:
            raise self.fail_read
        if self.wishlist_calls > 1 and self.fail_reread:
            return {k: Entry(k, t) for k, t in self.fail_reread.items()}
        return {k: Entry(k, t) for k, t in self.list.items()}

    def search(self, q):
        self.searches.append(q)
        key = norm(q)
        out = []
        for slug, g in self.pages.items():
            base = norm(g.title.split(" (")[0])
            if key and (key in base or base in key):
                out.append((g.title, slug))
        return out

    def game(self, slug):
        self.fetched.append(slug)
        return self.pages[slug]

    def dlc(self, game):
        return self.dlcs.get(game, [])

    def get_html(self, url):
        raise AssertionError("D2D is faked in these tests")

    def add(self, game_id, dlc_id=0):
        key = str(game_id) if not dlc_id else f"{game_id}:{dlc_id}"
        if key in self.already:
            raise AlreadyListed("already")
        self.writes.append(("add", key))
        self.journal.append(("ta", "add", key))
        self.list[key] = key

    def remove(self, game_id, dlc_id=0):
        key = str(game_id) if not dlc_id else f"{game_id}:{dlc_id}"
        self.writes.append(("remove", key))
        self.journal.append(("ta", "remove", key))
        self.list.pop(key, None)


class FakeReporter:
    def __init__(self):
        self.problem_syncs = []
        self.reviews = []
        self.notes = []

    def sync_problems(self, problems, keep_kinds, keep_keys=frozenset()):
        self.problem_syncs.append((list(problems), set(keep_kinds)))
        self.keep_keys = set(keep_keys)

    def plan_review(self, plan, *, dry_run):
        self.reviews.append((plan, dry_run))
        return True

    def notify(self, title, body):
        self.notes.append((title, body))

    @property
    def problems(self):
        return self.problem_syncs[-1][0] if self.problem_syncs else []


def tag(slug, gid, title, platform=SERIES):
    return TAGame(gid, title, platform, slug, (platform,))


# --------------------------------------------------------------------------- harness


def feed_record(pid, title, kind="released", product_kind="game"):
    return {"productId": pid, "title": title, "productKind": product_kind, "kind": kind,
            "platforms": ["XboxSeriesX"]}


def write_feed(tmp_path, records, age=timedelta(0)):
    path = tmp_path / "feed.json"
    path.write_text(json.dumps(records))
    if age:
        old = time.time() - age.total_seconds()
        os.utime(path, (old, old))
    return path


SECRETS = dict(deku_email="me@example.invalid", deku_password="deku-pw-Zq81",
               deku_wishlist_id="wl-id-7781", ta_gamertag="Fake Gamer", ta_password="ta-pw-K22x",
               vikunja_token="vk-token-3318", apprise_url="http://apprise.invalid/notify/key-9921")


class Harness:
    def __init__(self, tmp_path, *, records, deku, ta, overrides=None, verdicts=None, **cfg):
        self.tmp = tmp_path
        self.deku, self.ta, self.reporter = deku, ta, FakeReporter()
        self.made = []
        self.verdicts = verdicts or {}
        self.verdict_error = None
        feed = write_feed(tmp_path, records) if records is not None else tmp_path / "feed.json"
        ov = tmp_path / "overrides.json"
        if overrides is not None:
            ov.write_text(json.dumps(overrides))
        self.cfg = Config(feed=feed, state=tmp_path / "state" / "state.json", overrides=ov,
                          flaresolverr_url="http://fs.invalid/v1", vikunja_url="http://vk.invalid",
                          **{**SECRETS, **cfg})
        self.clock = 0.0

    def deps(self):
        def make_deku(cfg):
            self.made.append("deku")
            return self.deku

        def make_ta(cfg, game_ids):
            self.made.append("ta")
            self.ta.game_ids.update(game_ids)
            return self.ta

        def verdicts(items, state, fetch, http_get, now, **kw):
            if self.verdict_error:
                raise self.verdict_error
            return {i.product_id: self.verdicts.get(i.product_id) for i in items}

        def mono():
            return self.clock

        return Deps(make_deku=make_deku, make_ta=make_ta, make_reporter=lambda cfg: self.reporter,
                    refresh_verdicts=verdicts, now=lambda: NOW, monotonic=mono)

    def run(self):
        return run(self.cfg, self.deps())


def basic(tmp_path, **kw):
    """Alpha (add on both), Beta (Deku update), Gamma (stale on Deku), TA 999 stale."""
    journal = []
    deku = FakeDeku(
        {"alpha-quest": ("Alpha Quest", {"xbox_series"}),
         "beta-saga": ("Beta Saga", {"xbox_series", "xbox_one"}),
         "gamma-tale": ("Gamma Tale", set())},
        {"beta-saga": ("digital", {"xbox_series"}), "gamma-tale": ("either", set())},
        journal=journal,
    )
    ta = FakeTA(
        {"alpha-quest": tag("alpha-quest", 101, "Alpha Quest"),
         "beta-saga": tag("beta-saga", 102, "Beta Saga"),
         "old-game": tag("old-game", 999, "Old Game")},
        {"999": "Old Game"},
        journal=journal,
    )
    records = kw.pop("records", [feed_record("PA", "Alpha Quest"), feed_record("PB", "Beta Saga")])
    h = Harness(tmp_path, records=records, deku=deku, ta=ta, **kw)
    h.journal = journal
    return h


# --------------------------------------------------------------------------- brief tests


def test_dry_run_writes_nothing_but_upserts_plan_review(tmp_path):
    h = basic(tmp_path, dry_run=True)
    assert h.run() == 0
    assert h.deku.writes == [] and h.ta.writes == []
    assert len(h.reporter.reviews) == 1
    plan, dry = h.reporter.reviews[0]
    assert dry is True
    assert [e.target for e in plan.deku.adds] == ["alpha-quest"]
    assert [d.target for _, d in plan.deku.updates] == ["beta-saga"]
    assert [e.target for e in plan.deku.removes] == ["gamma-tale"]
    assert sorted(e.target for e in plan.ta.adds) == ["101", "102"]
    assert [e.target for e in plan.ta.removes] == ["999"]
    assert any("plan" in t.lower() for t, _ in h.reporter.notes)


def test_apply_runs_adds_then_updates_then_removes(tmp_path):
    h = basic(tmp_path, dry_run=False)
    assert h.run() == 0
    assert [w[:2] for w in h.deku.writes] == [("add", "alpha-quest"), ("update", "beta-saga"),
                                              ("remove", "gamma-tale")]
    assert h.deku.writes[0] == ("add", "alpha-quest", "digital", frozenset({"xbox_series"}))
    assert h.deku.writes[1] == ("update", "beta-saga", "digital", frozenset({"xbox_series", "xbox_one"}))
    assert h.ta.writes == [("add", "101"), ("add", "102"), ("remove", "999")]
    assert h.reporter.reviews and h.reporter.reviews[-1][1] is False
    assert any("+1 Deku" in b for _, b in h.reporter.notes)


def test_ta_read_failure_still_applies_deku_with_no_ta_writes(tmp_path):
    h = basic(tmp_path, dry_run=False)
    h.ta.fail_read = IncompleteRead("read 0 of 3 rows")
    assert h.run() == 1
    assert [w[0] for w in h.deku.writes] == ["add", "update", "remove"]
    assert h.ta.writes == []
    problems, keep = h.reporter.problem_syncs[-1]
    assert "unmatched-ta" in keep and not any(p.kind == "unmatched-ta" for p in problems)
    assert any("fail" in t.lower() for t, _ in h.reporter.notes)


def test_skip_override_excludes_item_without_problem(tmp_path):
    h = basic(tmp_path, dry_run=True, overrides={"PB": {"deku": "skip", "ta": "skip"}})
    assert h.run() == 0
    assert "Beta Saga" not in h.deku.searches and "Beta Saga" not in h.ta.searches
    assert not any(p.product_id == "PB" for p in h.reporter.problems)
    plan = h.reporter.reviews[0][0]
    # Not mirrored: the existing Deku entry goes like any other non-Xbox entry.
    assert sorted(e.target for e in plan.deku.removes) == ["beta-saga", "gamma-tale"]
    assert [e.target for e in plan.ta.adds] == ["101"]


def test_feed_guard_failure_exits_before_any_login(tmp_path):
    h = basic(tmp_path)
    write_feed(tmp_path, [feed_record("PA", "Alpha Quest")], age=timedelta(days=3))
    assert h.run() == 1
    assert h.made == []
    assert h.deku.wishlist_calls == 0 and h.ta.wishlist_calls == 0
    assert any("fail" in t.lower() and "stale" in b for t, b in h.reporter.notes)


def test_secrets_never_reach_logs_or_reporter(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    h = basic(tmp_path, dry_run=False)
    h.ta.fail_read = TAError(f"bad page for {SECRETS['ta_gamertag']} {SECRETS['ta_password']}")
    h.deku.fail_search["Alpha Quest"] = DekuError(
        f"boom {SECRETS['deku_email']} {SECRETS['deku_password']} {SECRETS['deku_wishlist_id']} "
        f"{SECRETS['vikunja_token']} {SECRETS['apprise_url']} Fake+Gamer"
    )
    assert h.run() == 1
    blob = caplog.text + repr(h.reporter.problem_syncs) + repr(h.reporter.reviews) + repr(h.reporter.notes)
    assert h.reporter.notes  # something was reported
    for value in [*SECRETS.values(), "Fake+Gamer"]:
        assert value not in blob, value
    assert "<redacted>" in blob or "TAError" in blob


# --------------------------------------------------------------------------- rulings


def test_lookup_error_is_ambiguous_and_pauses_removes(tmp_path):
    h = basic(tmp_path, dry_run=False)
    h.deku.fail_search["Alpha Quest"] = DekuError("network error: URLError")
    # A per-item failure is a warning (ruling 6): exit 0, removals paused, no task, task kept.
    assert h.run() == 0
    assert ("remove", "gamma-tale") not in h.deku.writes
    assert ("update", "beta-saga") in [w[:2] for w in h.deku.writes]
    assert not any(p.product_id == "PA" for p in h.reporter.problems)
    assert h.reporter.keep_keys == {"PA:unmatched-deku"}
    assert not any("fail" in t.lower() for t, _ in h.reporter.notes)
    assert h.ta.writes == [("add", "101"), ("add", "102"), ("remove", "999")]


def test_transient_lookup_failure_is_retried_once(tmp_path):
    h = basic(tmp_path, dry_run=False)
    real = h.ta.search
    calls = []

    def flaky(q):
        calls.append(q)
        if len(calls) == 1:
            raise TAError("FlareSolverr request.get failed: challenge timeout")
        return real(q)

    h.ta.search = flaky
    assert h.run() == 0
    assert calls[:2] == ["Alpha Quest", "Alpha Quest"]
    assert h.ta.writes == [("add", "101"), ("add", "102"), ("remove", "999")]


def test_unrelated_search_results_do_not_pause_removes(tmp_path):
    records = [feed_record("PA", "Alpha Quest"), feed_record("PU", "Zeta Unseen", kind="unannounced")]
    h = basic(tmp_path, dry_run=True, records=records)
    h.deku.games["zeta-legacy"] = ("Zeta Legacy Remaster", {"xbox_series"})
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert plan.deku.paused_removes == [] and plan.deku.removes
    assert "Zeta Unseen" in plan.skipped_unannounced


def test_related_search_result_pauses_removes(tmp_path):
    # "Alpha Quest II" is word-related to the stale "Alpha Quest" entry: it may be the same game.
    records = [feed_record("PX", "Alpha Quest II: Return")]
    h = basic(tmp_path, dry_run=True, records=records)
    h.deku.list["alpha-quest"] = ("digital", frozenset({"xbox_series"}))
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert plan.deku.removes == []
    assert {e.target for e in plan.deku.paused_removes} >= {"alpha-quest"}


def test_unmatched_item_related_to_a_listed_entry_pauses_removes(tmp_path):
    # Search finds nothing for the item, but "Alpha Quest" on the list may be the same game.
    records = [feed_record("PB", "Beta Saga"), feed_record("PX", "Alpha Quest Reborn", kind="unannounced")]
    h = basic(tmp_path, dry_run=True, records=records)
    h.deku.results["Alpha Quest Reborn"] = []
    h.deku.list["alpha-quest"] = ("digital", frozenset({"xbox_series"}))
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert plan.deku.removes == []
    assert {e.target for e in plan.deku.paused_removes} == {"alpha-quest", "gamma-tale"}
    assert any(p.product_id == "PX" and "Alpha Quest" in p.detail for p in h.reporter.problems)


def test_deku_match_must_offer_xbox(tmp_path):
    h = basic(tmp_path, dry_run=True, records=[feed_record("PG", "Gamma Tale")])
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert plan.deku.adds == [] and plan.deku.removes == []
    assert any(p.product_id == "PG" and p.kind == "unmatched-deku" for p in h.reporter.problems)


def test_empty_deku_wishlist_is_reported_and_adds_continue(tmp_path):
    h = basic(tmp_path, dry_run=False)
    h.deku.list = {}
    assert h.run() == 1
    assert ("add", "alpha-quest") in [w[:2] for w in h.deku.writes]
    assert not any(w[0] == "remove" for w in h.deku.writes)
    assert any("empty" in b.lower() for _, b in h.reporter.notes)


def test_hidden_deku_entry_on_add_skips_deku_removes(tmp_path):
    h = basic(tmp_path, dry_run=False)
    real_wishlist = h.deku.wishlist

    def hiding(*, with_platforms=False):  # alpha is on the list but hidden from the share page
        out = real_wishlist(with_platforms=with_platforms)
        out.pop("alpha-quest", None)
        h.deku.last_items.pop("alpha-quest", None)
        return out

    h.deku.list["alpha-quest"] = ("digital", frozenset({"xbox_series"}))
    h.deku.wishlist = hiding
    assert h.run() == 0  # ruling 3: a problem task, not a run failure
    assert ("add", "alpha-quest") not in [w[:2] for w in h.deku.writes]
    assert not any(w[0] == "remove" for w in h.deku.writes)
    assert h.ta.writes  # TA unaffected
    assert [(p.product_id, p.kind, p.detail) for p in h.reporter.problems] == [
        ("PA", "unmatched-deku", "already on the Deku wishlist but hidden from the share page")]
    assert not any("fail" in t.lower() for t, _ in h.reporter.notes)


def test_ta_already_listed_stops_ta_writes(tmp_path):
    h = basic(tmp_path, dry_run=False)
    h.ta.already.add("101")
    assert h.run() == 1
    assert h.ta.writes == []
    assert [w[0] for w in h.deku.writes] == ["add", "update", "remove"]


def test_ta_auth_failure_during_matching_blocks_ta_writes(tmp_path):
    h = basic(tmp_path, dry_run=False)

    def boom(q):
        raise TAAuthError("session not accepted")

    h.ta.search = boom
    assert h.run() == 1
    assert h.ta.writes == []
    assert h.deku.writes


def test_ta_verification_mismatch_is_reported(tmp_path):
    h = basic(tmp_path, dry_run=False)
    h.ta.fail_reread = {"999": "Old Game"}  # the re-read shows nothing changed
    assert h.run() == 1
    assert any("verif" in b.lower() for _, b in h.reporter.notes)


def test_d2d_failure_keeps_formats_and_reports(tmp_path):
    h = basic(tmp_path, dry_run=False)
    h.verdict_error = TAError("lists unreadable")
    assert h.run() == 1
    assert not any(w[0] == "update" for w in h.deku.writes)
    assert ("add", "alpha-quest") in [w[:2] for w in h.deku.writes]
    _, keep = h.reporter.problem_syncs[-1]
    assert "d2d-disputed" in keep


def test_d2d_yes_verdict_sets_either(tmp_path):
    h = basic(tmp_path, dry_run=False, verdicts={"PA": "yes"})
    assert h.run() == 0
    assert h.deku.writes[0] == ("add", "alpha-quest", "either", frozenset({"xbox_series"}))


def test_only_deku_leaves_ta_alone(tmp_path):
    h = basic(tmp_path, dry_run=False, lists=("deku",))
    assert h.run() == 0
    assert h.ta.wishlist_calls == 0 and h.ta.searches == [] and h.ta.writes == []
    assert h.deku.writes
    problems, keep = h.reporter.problem_syncs[-1]
    assert "unmatched-ta" in keep


def test_mapping_is_cached_and_reused(tmp_path):
    h = basic(tmp_path, dry_run=True)
    assert h.run() == 0
    state = load_state(h.cfg.state)
    assert state.mapping["PA"]["deku_slug"] == "alpha-quest"
    assert state.mapping["PA"]["ta"]["game"] == 101
    assert state.last_feed_count == 2
    h.deku.searches.clear()
    h.ta.searches.clear()
    h.ta.fetched.clear()
    assert h.run() == 0
    assert h.deku.searches == [] and h.ta.searches == [] and h.ta.fetched == []


def test_renamed_deku_slug_in_the_cache_is_not_a_hidden_entry(tmp_path):
    # The cached slug was renamed on Deku: its page now redirects to the slug the share page lists.
    h = basic(tmp_path, dry_run=False)
    h.deku.list["alpha-quest"] = ("digital", frozenset({"xbox_series"}))
    real_item = h.deku.item
    h.deku.item = lambda slug: real_item("alpha-quest" if slug == "old-alpha" else slug)
    save_state(h.cfg.state, State(mapping={"PA": {"deku_slug": "old-alpha", "deku_key": "alpha quest"}}))
    assert h.run() == 0
    assert not any(w[1] == "alpha-quest" for w in h.deku.writes if w[0] in ("add", "remove"))
    assert not any(p.detail and "hidden" in p.detail for p in h.reporter.problems)
    assert load_state(h.cfg.state).mapping["PA"]["deku_slug"] == "alpha-quest"


def test_stale_cached_mapping_is_revalidated(tmp_path):
    h = basic(tmp_path, dry_run=True)
    save_state(h.cfg.state, State(mapping={"PA": {"deku_slug": "gamma-tale", "deku_key": "alpha quest"}}))
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert [e.target for e in plan.deku.adds] == ["alpha-quest"]
    assert "Alpha Quest" in h.deku.searches


def test_ta_picks_series_page_over_windows(tmp_path):
    h = basic(tmp_path, dry_run=True, records=[feed_record("PA", "Alpha Quest")])
    h.ta.pages["alpha-quest-windows"] = tag("alpha-quest-windows", 201, "Alpha Quest (Windows)", "Windows")
    h.ta.pages["alpha-quest"] = tag("alpha-quest", 101, "Alpha Quest", ONE)
    h.ta.pages["alpha-quest-xs"] = tag("alpha-quest-xs", 301, "Alpha Quest (Xbox Series X|S)", SERIES)
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert [e.target for e in plan.ta.adds] == ["301"]


def test_ta_placeholder_page_is_no_page_yet(tmp_path):
    # Unreleased games have a negative placeholder id on TA: not writable, so not matched.
    h = basic(tmp_path, dry_run=False, records=[feed_record("PA", "Alpha Quest", kind="concrete")])
    h.ta.pages["alpha-quest"] = tag("alpha-quest", -7827, "Alpha Quest")
    h.ta.list = {}
    assert h.run() == 0
    assert h.ta.writes == []
    assert not any(p.kind == "unmatched-ta" for p in h.reporter.problems)
    assert h.reporter.reviews[-1][0].skipped_unannounced == ["Alpha Quest"]


def test_ta_dlc_resolves_parent_and_pack(tmp_path):
    records = [feed_record("PD", "Alpha Quest - Lost Isles", product_kind="durable")]
    h = basic(tmp_path, dry_run=True, records=records)
    h.ta.dlcs["alpha-quest"] = [("Lost Isles", 55), ("Other Pack", 56)]
    h.deku.games["alpha-quest-lost-isles"] = ("Alpha Quest - Lost Isles", {"xbox_series"})
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert [e.target for e in plan.ta.adds] == ["101:55"]
    assert [(e.target, e.format) for e in plan.deku.adds] == [("alpha-quest-lost-isles", "digital")]


def test_ta_dlc_override_forces_pack(tmp_path):
    records = [feed_record("PD", "Alpha Quest Season Pass", product_kind="durable")]
    h = basic(tmp_path, dry_run=True, records=records,
              overrides={"PD": {"deku": "skip", "ta": "alpha-quest", "ta_dlc": 77}})
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert [e.target for e in plan.ta.adds] == ["101:77"]


def test_shrunk_feed_fails_unless_count_accepted(tmp_path):
    h = basic(tmp_path, dry_run=True)
    save_state(h.cfg.state, State(last_feed_count=10))
    assert h.run() == 1
    assert h.made == []
    h.cfg.accept_feed_count = 2
    assert h.run() == 0
    assert load_state(h.cfg.state).last_feed_count == 2


def test_corrupt_state_fails_before_login(tmp_path):
    h = basic(tmp_path)
    h.cfg.state.parent.mkdir(parents=True)
    h.cfg.state.write_text("{nope")
    assert h.run() == 1
    assert h.made == []
    assert h.cfg.state.read_text() == "{nope"


def test_invalid_overrides_fail_before_login(tmp_path):
    h = basic(tmp_path)
    h.cfg.overrides.write_text("[1, 2]")
    assert h.run() == 1
    assert h.made == []


def test_match_budget_defers_remaining_lookups(tmp_path):
    h = basic(tmp_path, dry_run=True, match_budget=10.0)
    real = h.deku.search

    def slow(q):
        h.clock += 60
        return real(q)

    h.deku.search = slow
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert plan.deku.removes == [] and plan.deku.paused_removes
    # Deferred items open no "add an override" task, and any existing one stays open.
    assert h.reporter.problems == []
    assert h.reporter.keep_keys == {"PB:unmatched-deku", "PA:unmatched-ta", "PB:unmatched-ta"}


def test_items_after_repeated_lookup_errors_are_transient(tmp_path):
    records = [feed_record(f"P{i}", f"Game {i}") for i in range(5)]
    h = basic(tmp_path, dry_run=True, records=records)
    for i in range(5):
        h.deku.fail_search[f"Game {i}"] = DekuError("HTTP 500")
    assert h.run() == 0
    assert not any(p.kind == "unmatched-deku" for p in h.reporter.problems)
    assert {f"P{i}:unmatched-deku" for i in range(5)} <= h.reporter.keep_keys
    assert h.deku.searches.count("Game 4") == 0  # skipped after 3 failures in a row


def test_ta_search_cut_off_by_the_page_cap_is_ambiguous(tmp_path):
    from wsync.ta import SearchResults

    h = basic(tmp_path, dry_run=True, records=[feed_record("PZ", "Zeta Unseen")])

    def capped(q):
        r = SearchResults([("Zebra Party", "zebra-party")])
        r.complete = False
        return r

    h.ta.search = capped
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert plan.ta.removes == [] and [e.target for e in plan.ta.paused_removes] == ["999"]
    assert any(p.product_id == "PZ" and p.kind == "unmatched-ta" and "truncated" in p.detail
               for p in h.reporter.problems)


def test_feed_count_recorded_only_after_a_good_run(tmp_path):
    h = basic(tmp_path, dry_run=True)
    h.ta.fail_read = IncompleteRead("rows")
    assert h.run() == 1
    assert load_state(h.cfg.state).last_feed_count is None
    h.ta.fail_read = None
    assert h.run() == 0
    assert load_state(h.cfg.state).last_feed_count == 2


def test_failure_ping_is_deduplicated_until_the_issues_change(tmp_path):
    h = basic(tmp_path, dry_run=True)
    h.ta.fail_read = IncompleteRead("read 3 of 4 rows")
    fails = lambda: [t for t, _ in h.reporter.notes if "failed" in t.lower()]
    assert h.run() == 1 and len(fails()) == 1
    h.ta.fail_read = IncompleteRead("read 2 of 4 rows")  # same issue, other numbers
    assert h.run() == 1 and len(fails()) == 1
    assert load_state(h.cfg.state).last_failure
    h.ta.fail_read = None
    assert h.run() == 0
    assert load_state(h.cfg.state).last_failure is None
    h.ta.fail_read = IncompleteRead("read 2 of 4 rows")
    assert h.run() == 1 and len(fails()) == 2  # a new failure after a clean run pings again
    h.ta.fail_read = TAError("something else")
    assert h.run() == 1 and len(fails()) == 3


def test_feed_failure_ping_is_deduplicated_too(tmp_path):
    h = basic(tmp_path)
    write_feed(tmp_path, [feed_record("PA", "Alpha Quest")], age=timedelta(days=3))
    assert h.run() == 1 and h.run() == 1
    assert len([t for t, _ in h.reporter.notes if "failed" in t.lower()]) == 1


def test_override_to_a_placeholder_page_is_no_page_yet(tmp_path):
    h = basic(tmp_path, dry_run=False, overrides={"PA": {"ta": "alpha-quest"}})
    h.ta.pages["alpha-quest"] = tag("alpha-quest", -7827, "Alpha Quest")
    assert h.run() == 0
    assert ("add", "-7827") not in h.ta.writes and not any(w[1].startswith("-") for w in h.ta.writes)
    assert ("remove", "999") not in h.ta.writes  # ambiguous: TA removals pause
    assert not any(p.product_id == "PA" and p.kind == "unmatched-ta" for p in h.reporter.problems)
    assert "alpha-quest" not in load_state(h.cfg.state).ta_game_ids  # placeholder ids aren't cached


def test_override_title_is_cached_with_the_id(tmp_path):
    h = basic(tmp_path, dry_run=True, overrides={"PA": {"ta": "alpha-quest"}})
    assert h.run() == 0
    state = load_state(h.cfg.state)
    assert state.mapping["PA"]["ta_override"] == {"slug": "alpha-quest", "title": "Alpha Quest"}
    assert state.ta_game_ids["alpha-quest"] == 101
    h.ta.fetched.clear()
    assert h.run() == 0
    assert "alpha-quest" not in h.ta.fetched


def test_no_write_starts_after_the_deadline(tmp_path):
    h = basic(tmp_path, dry_run=False, write_deadline=100.0)
    real_add = h.deku.add

    def slow_add(*a):
        h.clock += 200
        return real_add(*a)

    h.deku.add = slow_add
    assert h.run() == 0
    # The first add ran past the deadline: no update, removal or TA write starts after it.
    assert [w[0] for w in h.deku.writes] == ["add"]
    assert h.ta.writes == []
    assert any("5 writes left for the next run" in b for _, b in h.reporter.notes)


def test_deadline_reached_during_ta_adds_stops_the_rest(tmp_path):
    h = basic(tmp_path, dry_run=False, write_deadline=100.0)
    real_add = h.ta.add

    def slow_add(*a):
        h.clock += 200
        return real_add(*a)

    h.ta.add = slow_add
    assert h.run() == 0
    assert [w[0] for w in h.deku.writes] == ["add", "update", "remove"]
    assert h.ta.writes == [("add", "101")]
    assert any("2 writes left for the next run" in b for _, b in h.reporter.notes)


def test_sigterm_saves_state_and_exits_1(tmp_path):
    from wsync.run import Terminated

    h = basic(tmp_path, dry_run=False)

    def killed(*a):
        raise Terminated()

    h.deku.add = killed
    assert h.run() == 1
    assert h.deku.writes == [] and h.ta.writes == []
    state = load_state(h.cfg.state)
    assert state.mapping["PA"]["deku_slug"] == "alpha-quest"  # lookups done so far are kept
    assert state.last_failure and state.last_feed_count is None
    assert any("failed" in t.lower() and "SIGTERM" in b for t, b in h.reporter.notes)


def test_sigterm_before_the_state_loads_does_not_write_it(tmp_path):
    from wsync.run import Terminated

    h = basic(tmp_path, dry_run=False)

    def killed(cfg):
        raise Terminated()

    deps = h.deps()
    deps.make_reporter = killed
    from wsync.run import run as run_

    assert run_(h.cfg, deps) == 1
    assert not h.cfg.state.exists()


def test_main_turns_sigterm_into_a_clean_failure(monkeypatch):
    import signal

    import wsync.__main__ as main_mod
    from wsync.run import Terminated

    seen = {}

    def fake_run(cfg):
        try:
            signal.raise_signal(signal.SIGTERM)
        except Terminated:
            seen["terminated"] = True
            return 1
        return 0

    monkeypatch.setattr(main_mod, "run", fake_run)
    before = signal.getsignal(signal.SIGTERM)
    env = {"DEKU_EMAIL": "e", "TA_GAMERTAG": "g"}
    monkeypatch.setattr(main_mod.os, "environ", env)
    assert main_mod.main(["--no-report"]) == 1
    assert seen == {"terminated": True}
    assert signal.getsignal(signal.SIGTERM) == before  # restored


def test_no_change_dry_run_is_quiet(tmp_path):
    h = basic(tmp_path, dry_run=True, records=[feed_record("PB", "Beta Saga")])
    h.deku.list = {"beta-saga": ("digital", frozenset({"xbox_series", "xbox_one"}))}
    h.ta.list = {"102": "Beta Saga"}
    assert h.run() == 0
    assert h.reporter.notes == []


# --------------------------------------------------------------------------- config


def test_config_dry_run_defaults_and_flags():
    env = {"DEKU_EMAIL": "e", "TA_GAMERTAG": "g"}
    assert config_from([], env).dry_run is True
    assert config_from([], {**env, "DRY_RUN": "false"}).dry_run is False
    assert config_from([], {**env, "DRY_RUN": "no"}).dry_run is False
    assert config_from([], {**env, "DRY_RUN": "true"}).dry_run is True
    assert config_from([], {**env, "DRY_RUN": "garbage"}).dry_run is True
    assert config_from(["--apply"], env).dry_run is False
    assert config_from(["--dry-run"], {**env, "DRY_RUN": "false"}).dry_run is True
    assert config_from(["--only", "ta"], env).lists == ("ta",)
    assert config_from(["--no-report"], env).report is False
    assert config_from(["--accept-feed-count", "40"], env).accept_feed_count == 40
    assert config_from([], {**env, "WSYNC_ACCEPT_FEED_COUNT": "41"}).accept_feed_count == 41
    cfg = config_from([], env)
    assert str(cfg.feed) == "/feed/wishlist.json" and str(cfg.state) == "/state/state.json"
    assert str(cfg.overrides) == "/config/overrides.json"


def test_config_repr_hides_secrets():
    cfg = Config(**SECRETS)
    for value in SECRETS.values():
        assert value not in repr(cfg)


# --------------------------------------------------------------------------- candidate rule


@pytest.mark.parametrize("a, b, want", [
    ("Warhammer 40,000: Space Marine - Master Crafted Edition",
     "Warhammer 40K: Space Marine - Master Crafted Edition", True),
    ("Persona 4 Revival", "Persona 4 Golden", True),
    ("Final Fantasy VII Rebirth", "FINAL FANTASY Tactics", True),  # first two words agree
    ("The Expanse: Osiris Reborn", "Expanse: Osiris Reborn Deluxe", True),  # leading article skipped
    ("Grand Theft Auto VI", "Grand Theft Auto V", True),
    ("Fable Premium Edition", "Fable II", True),  # one significant word: the first must agree
    # A publisher prefix defeats the rule; resolve such cases with an override.
    ("Tom Clancy's The Division 2", "The Division 2", False),
    ("Persona 6", "Persona 5 Royal", False),
    ("Zeta Unseen", "Zeta Legacy Remaster", False),
    ("Grand Theft Auto VI", "Hitman", False),
    ("Alpha Quest (Xbox One)", "Alpha Quest II", True),
])
def test_related_candidate_rule(a, b, want):
    from wsync.run import related

    assert related(a, b) is want
    assert related(b, a) is want


def test_query_drops_trademark_symbols_without_splitting_punctuation():
    from wsync.run import _query
    # "LEGO Batman : Legacy…" (space before the colon) finds nothing on TrueAchievements.
    assert _query("LEGO® Batman™: Legacy of the Dark Knight") == "LEGO Batman: Legacy of the Dark Knight"
    assert _query("STAR WARS Zero Company™") == "STAR WARS Zero Company"


def test_ta_match_falls_back_to_the_subtitle_when_the_full_title_finds_nothing(tmp_path):
    # TrueAchievements search can't match "Δ" (or "Delta"), but finds the page by its subtitle.
    h = basic(tmp_path, dry_run=True, records=[feed_record("PD", "METAL GEAR SOLID Δ: SNAKE EATER")])
    h.ta.pages["mgs-delta"] = tag("mgs-delta", 777, "Metal Gear Solid Δ: Snake Eater")

    def search(q):
        h.ta.searches.append(q)
        return [("Metal Gear Solid Δ: Snake Eater", "mgs-delta")] if q == "SNAKE EATER" else []

    h.ta.search = search
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert [e.target for e in plan.ta.adds] == ["777"]
    assert "SNAKE EATER" in h.ta.searches


def test_unreleased_game_without_an_xbox_listing_on_deku_is_skipped_silently(tmp_path):
    # Deku has a page for the game but no Xbox listing yet (it isn't out): like "no page yet",
    # no task and no hold on removals; it's retried each run and matches once Deku lists Xbox.
    h = basic(tmp_path, dry_run=True, records=[feed_record("PU", "Zeta Rising", kind="unannounced")])
    h.deku.games["zeta-rising"] = ("Zeta Rising", set())
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert not any(p.product_id == "PU" and p.kind == "unmatched-deku" for p in h.reporter.problems)
    assert plan.deku.paused_removes == []
    assert "gamma-tale" in [e.target for e in plan.deku.removes]
    assert "Zeta Rising" in plan.skipped_unannounced


def test_released_game_without_an_xbox_listing_on_deku_is_still_a_problem(tmp_path):
    h = basic(tmp_path, dry_run=True, records=[feed_record("PR", "Zeta Rising", kind="released")])
    h.deku.games["zeta-rising"] = ("Zeta Rising", set())
    assert h.run() == 0
    plan = h.reporter.reviews[0][0]
    assert any(p.product_id == "PR" and p.kind == "unmatched-deku" for p in h.reporter.problems)
    assert plan.deku.removes == [] and plan.deku.paused_removes

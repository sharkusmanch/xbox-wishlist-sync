import json
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, time as dtime, timedelta, timezone

import pytest

from wsync import d2d
from wsync.deku import DekuError, DekuItem, OwnedDetail, OwnedEntry
from wsync.errors import TAError
from wsync.flaresolverr import FlareSolverrError
from wsync.match import base_title, norm
from wsync.plan import Entry
from wsync.run import PLACEHOLDER, Config, ConfigError, Deps, config_from, run
from wsync.state import State, load_state, save_state
from wsync.ta import AlreadyListed, AuthError as TAAuthError, BudgetSpent, ChangedOnTA, HttpError, IncompleteRead, LoginBackoff, TAGame
from wsync.ta import PageUnavailable, Unreachable
from wsync.ta_coll import OwnedRow

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
SERIES, ONE = "Xbox Series X|S", "Xbox One"


# --------------------------------------------------------------------------- fakes


class FakeDeku:
    def __init__(self, games, wishlist, *, journal=None):
        # games: slug -> (title, offered platforms[, Xbox release ISO date]); wishlist: slug -> (format, ticked platforms)
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
        # Deku collection: slug -> [OwnedDetail]; the share page is derived from it
        self.owned = {}
        self.calls = []  # every request the collection step can make, in order (plus sleeps)
        self.paced_with = []
        self.fail_collection = None
        self.fail_item = {}  # slug -> exception raised by item()
        self.fail_own = {}  # slug -> exception raised by own()
        self.redirects = {}  # old slug -> current slug (Deku redirects renamed items)

    def paced(self, seconds):
        self.paced_with.append(seconds)
        return self

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))

    def collection(self):
        self.calls.append(("collection",))
        if self.fail_collection:
            raise self.fail_collection
        return [OwnedEntry(slug, self.games[slug][0], o.platform, o.format)
                for slug, entries in self.owned.items() for o in entries]

    def own(self, item, fmt, platform):
        self.calls.append(("own", item.slug))
        if item.slug in self.fail_own:
            raise self.fail_own[item.slug]
        assert platform in item.offered and not any(o.platform == platform for o in item.owned)
        self.writes.append(("own", item.slug, fmt, platform))
        self.journal.append(("deku", "own", item.slug))
        n = len(self.owned.get(item.slug, []))
        self.owned.setdefault(item.slug, []).append(OwnedDetail(f"o-{item.slug}-{n}", platform, fmt))

    def _item(self, slug):
        title, offered, *rest = self.games[slug]
        on = slug in self.list
        fmt, ticked = self.list.get(slug, ("", frozenset()))
        settings = {}
        if on:
            settings = {"desired_format": fmt, "desired_price": "12.34", "note": "keep me"}
            for p in ("xbox_series", "xbox_one", "switch"):
                settings[f"platform_{p}"] = "true" if p in ticked else "false"
        return DekuItem(slug, f"uuid-{slug}", title, on, frozenset(ticked) if on else frozenset(),
                        frozenset(offered), settings, tuple(self.owned.get(slug, ())),
                        released=rest[0] if rest else None)

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
        self.calls.append(("search", q))
        if q in self.fail_search:
            raise self.fail_search[q]
        if q in self.results:
            return list(self.results[q])
        words = set(norm(q).split())  # a loose full-text search, like the real one
        return [(t, s) for s, (t, *_) in self.games.items() if words & set(norm(t).split())]

    def item(self, slug):
        self.calls.append(("item", slug))
        if slug in self.fail_item:
            raise self.fail_item[slug]
        return self._item(self.redirects.get(slug, slug))

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
        self.owned_rows = []  # the TA Game Collection (OwnedRow)
        self.collection_calls = 0
        self.fail_collection = None
        # the library step: every request in order (with the run's sleeps, when a test routes them here)
        self.calls = []
        self.list_ids = {}  # TA property list slug -> game ids
        self.list_pages = {}  # TA property list slug -> its number of pages (default 1)
        self.fail_list = {}  # slug -> exception from property_list()
        self.fail_page = {}  # slug -> exception from game_page_or_none()
        self.fail_add = {}  # slug -> exception from collection_add()
        self.fail_set = {}  # (game id, field) -> exception from collection_set()
        self.on_add = None  # called with the slug before an add is made
        self.on_set = None  # called with (game id, field) when a field set is asked for; may raise
        self.unlisted = set()  # TA game ids in the collection that the collection read doesn't show
        self.meter = None  # the run's TAMeter when the test meters the fake (Harness.metered): one request a call

    def _hit(self):
        """One TA request: a metered fake asks the run's meter first, which counts it or refuses it (BudgetSpent),
        as TAClient does before sending; a refused call is not made (nothing recorded)."""
        if self.meter is not None:
            self.meter.before_request()

    def collection(self):
        self._hit()
        self.collection_calls += 1
        self.calls.append(("collection",))
        if self.fail_collection:
            raise self.fail_collection
        return list(self.owned_rows)

    def property_list(self, slug, *, pause=None):
        """One ("list", slug) request a page, each after `pause()` (as the client calls it)."""
        for _ in range(self.list_pages.get(slug, 1)):
            if pause:
                pause()
            self._hit()
            self.calls.append(("list", slug))
            if slug in self.fail_list:
                raise self.fail_list[slug]
        return list(self.list_ids.get(slug, []))

    def game_page_or_none(self, slug):
        self._hit()
        self.calls.append(("page", slug))
        if slug in self.fail_page:
            raise self.fail_page[slug]
        return self.pages.get(slug)

    def collection_add(self, slug, *, game_id=None, new=False):
        self._hit()
        self.calls.append(("gc-add", slug))
        if slug in self.fail_add:
            raise self.fail_add[slug]
        g = self.pages[slug]
        assert game_id in (None, g.id)
        if g.id in self.unlisted or any(r.ta_id == g.id for r in self.owned_rows):
            if new:
                raise AlreadyListed(f"collection add of {slug!r}: game {g.id} is already in the collection")
            return g.id  # already listed: nothing posted
        if self.on_add:
            self.on_add(slug)
        self.writes.append(("gc-add", slug))
        self.journal.append(("ta", "gc-add", slug))
        icon = {"Xbox Series X|S": "xbox-series-x-s", "Xbox One": "xbox-one", "Xbox 360": "xbox-360",
                "Windows": "windows"}.get(g.platform, "xbox-one")
        self.owned_rows.append(OwnedRow(g.id, g.title, icon, "Owned", ""))  # TA's defaults for a new entry
        return g.id

    def collection_set(self, game_id, field_name, label, expect=None):
        """Compare-and-set, as the client: nothing posted when the entry already shows `label`; ChangedOnTA when it
        shows something `expect` doesn't hold."""
        self._hit()
        self.calls.append(("gc-set", game_id, field_name, label))
        if self.on_set:
            self.on_set(game_id, field_name)
        if (game_id, field_name) in self.fail_set:
            raise self.fail_set[(game_id, field_name)]
        i = next(n for n, r in enumerate(self.owned_rows) if r.ta_id == game_id)
        r = self.owned_rows[i]
        current = r.ownership if field_name == "ownership" else r.media
        if current == label:
            return False
        if expect is not None and current not in expect:
            raise ChangedOnTA(f"collection {field_name} of {game_id}: TA shows {current!r}")
        self.owned_rows[i] = replace(r, **({"ownership": label} if field_name == "ownership" else {"media": label}))
        self.writes.append(("gc-set", game_id, field_name, label))
        self.journal.append(("ta", "gc-set", game_id))
        return True

    def wishlist(self):
        self._hit()
        self.wishlist_calls += 1
        if self.fail_read:
            raise self.fail_read
        if self.wishlist_calls > 1 and self.fail_reread:
            return {k: Entry(k, t) for k, t in self.fail_reread.items()}
        return {k: Entry(k, t) for k, t in self.list.items()}

    def search(self, q):
        self._hit()
        self.searches.append(q)
        key = norm(q)
        out = []
        for slug, g in self.pages.items():
            base = norm(g.title.split(" (")[0])
            if key and (key in base or base in key):
                out.append((g.title, slug))
        return out

    def game(self, slug):
        self._hit()
        self.fetched.append(slug)
        return self.pages[slug]

    def dlc(self, game):
        self._hit()
        return self.dlcs.get(game, [])

    def get_html(self, url):
        raise AssertionError("D2D is faked in these tests")

    def add(self, game_id, dlc_id=0):
        self._hit()
        key = str(game_id) if not dlc_id else f"{game_id}:{dlc_id}"
        if key in self.already:
            raise AlreadyListed("already")
        self.writes.append(("add", key))
        self.journal.append(("ta", "add", key))
        self.list[key] = key

    def remove(self, game_id, dlc_id=0):
        self._hit()
        key = str(game_id) if not dlc_id else f"{game_id}:{dlc_id}"
        self.writes.append(("remove", key))
        self.journal.append(("ta", "remove", key))
        self.list.pop(key, None)


class FakeReporter:
    def __init__(self):
        self.problem_syncs = []
        self.reviews = []
        self.notes = []
        self.collection_reviews = []  # (body, dry_run)
        self._collection_body = None
        self.collection_360_reviews = []  # the task text, or None when closed
        self._collection_360_body = None
        self.library_tasks = []  # (key, title, body)
        self._library_bodies = {}  # key -> the open task's text
        self.budget_tasks = []  # the TA request budget task's text at each upsert, or None when it is closed

    def ta_budget_task(self, body):
        self.budget_tasks.append(body)
        return body is not None

    def sync_problems(self, problems, keep_kinds, keep_keys=frozenset()):
        self.problem_syncs.append((list(problems), set(keep_kinds)))
        self.keep_keys = set(keep_keys)

    def plan_review(self, plan, *, dry_run):
        self.reviews.append((plan, dry_run))
        return True

    def collection_review(self, body, *, dry_run):
        self.collection_reviews.append((body, dry_run))
        if not dry_run:
            self._collection_body = None
            return False
        changed = body != self._collection_body
        self._collection_body = body
        return changed

    def collection_360_review(self, body):
        self.collection_360_reviews.append(body)
        changed = body is not None and body != self._collection_360_body
        self._collection_360_body = body
        return changed

    def library_task(self, key, title, body):
        self.library_tasks.append((key, title, body))
        changed = body is not None and body != self._library_bodies.get(key)
        self._library_bodies[key] = body
        return changed

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
        self.meters = []
        self.guards = []
        self.verdicts = verdicts or {}
        self.verdict_error = None
        feed = write_feed(tmp_path, records) if records is not None else tmp_path / "feed.json"
        ov = tmp_path / "overrides.json"
        if overrides is not None:
            ov.write_text(json.dumps(overrides))
        # The library step is off unless a test opts in (library=True and a FakeXbox in `xbox`); its daily pass starts
        # after 11:30 here, so it is due at NOW (12:00; the production default is 12:30). The TA meter doesn't pace
        # here (tests/test_meter.py covers pacing), so a metered fake never sleeps.
        self.cfg = Config(feed=feed, state=tmp_path / "state" / "state.json", overrides=ov,
                          flaresolverr_url="http://fs.invalid/v1", vikunja_url="http://vk.invalid",
                          **{**SECRETS, "collection": False, "library": False, "library_after": dtime(11, 30),
                             "ta_gap": 0.0, "ta_jitter": 0.0, "xbox_token": tmp_path / "state" / "xbox-token.json",
                             **cfg})
        self.clock = 0.0
        self.now = NOW
        self.xbox = None
        self.metered = False  # True: every FakeTA call is a request the run's TA meter counts or refuses

    def make_xbox_http(self):
        if self.xbox is None:
            raise AssertionError("a Microsoft request in a test without the library step")
        return self.xbox

    def last_issues(self):
        """The issues of the last failure ping (the ping is deduplicated, so read the latest one)."""
        bodies = [b for t, b in self.reporter.notes if "failed" in t.lower()]
        return bodies[-1].splitlines() if bodies else []

    def deps(self):
        def make_deku(cfg):
            self.made.append("deku")
            return self.deku

        def make_ta(cfg, game_ids, meter=None, login_guard=None):
            self.meters.append(meter)
            self.guards.append(login_guard)
            self.made.append("ta")
            self.ta.game_ids.update(game_ids)
            self.ta.meter = meter if self.metered else None
            return self.ta

        def verdicts(items, state, fetch, http_get, now, **kw):
            if self.verdict_error:
                raise self.verdict_error
            return {i.product_id: self.verdicts.get(i.product_id) for i in items}

        def mono():
            return self.clock

        return Deps(make_deku=make_deku, make_ta=make_ta, make_reporter=lambda cfg: self.reporter,
                    refresh_verdicts=verdicts, now=lambda: self.now, monotonic=mono,
                    sleep=self.deku.sleep, rand=lambda: 0.0, make_xbox_http=self.make_xbox_http)

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
            raise FlareSolverrError("FlareSolverr request.get failed: challenge timeout")
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
    assert ("remove", "999") in h.ta.writes  # "no page yet", and the override pins PA: TA removals go ahead
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

    def fake_run(cfg, **kw):
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
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        assert main_mod.main(["--no-report"]) == 1
    finally:  # main() installs a root handler that redacts "e" and "g": never leave it to later tests' log lines
        root.handlers[:] = handlers
        root.setLevel(level)
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


# --------------------------------------------------------------------------- Deku collection step wiring


def test_collection_rate_limit_mid_step_leaves_the_wishlist_results_alone(tmp_path):
    """Review Focus 4: a 429 in the collection step stops it; exit 0; the wishlist plan and writes
    are what they are without the step."""
    from wsync.deku import DekuRateLimited
    from wsync.ta_coll import OwnedRow

    (tmp_path / "plain").mkdir()
    (tmp_path / "with").mkdir()
    plain = basic(tmp_path / "plain", dry_run=False)
    assert plain.run() == 0
    h = basic(tmp_path / "with", dry_run=False, collection=True, collection_dry_run=False)
    h.deku.games["gamma-tale"] = plain.deku.games["gamma-tale"] = ("Gamma Tale", {"xbox_series"})
    h.ta.owned_rows = [OwnedRow(1, "Gamma Tale", "xbox-series-x-s", "Owned", "Digital"),
                       OwnedRow(2, "Alpha Quest", "xbox-series-x-s", "Owned", "Digital")]
    real_collection = h.deku.collection

    def collection_then_429():  # armed once the wishlist step is over
        h.deku.fail_item["alpha-quest"] = DekuRateLimited("GET /items/alpha-quest answered HTTP 429")
        return real_collection()

    h.deku.collection = collection_then_429
    assert h.run() == 0
    assert h.deku.writes[:3] == plain.deku.writes and h.ta.writes == plain.ta.writes
    assert [r[0].deku.adds for r in h.reporter.reviews] == [r[0].deku.adds for r in plain.reporter.reviews]
    assert not any("failed" in t.lower() for t, _ in h.reporter.notes)
    after = [c for c in h.deku.calls if c[0] != "sleep"]
    assert after[-1] == ("item", "alpha-quest")  # no Deku request after the 429


def test_config_collection_switches():
    env = {"DEKU_EMAIL": "e", "TA_GAMERTAG": "g"}
    cfg = config_from([], env)
    assert cfg.collection is True and cfg.collection_dry_run is True and cfg.lists == ("deku", "ta")
    for value in ("false", "0", "no", "off", " FALSE "):
        assert config_from(["--apply"], {**env, "COLLECTION_DRY_RUN": value}).collection_dry_run is False
    for value in ("true", "", "garbage", "nope"):
        assert config_from(["--apply"], {**env, "COLLECTION_DRY_RUN": value}).collection_dry_run is True
    # A dry run is dry for the collection step too.
    assert config_from([], {**env, "COLLECTION_DRY_RUN": "false"}).collection_dry_run is True
    assert config_from(["--dry-run"], {**env, "DRY_RUN": "false", "COLLECTION_DRY_RUN": "false"}).collection_dry_run is True
    only = config_from(["--only", "collection"], env)
    assert only.lists == () and only.collection is True
    assert config_from(["--only", "deku"], env).collection is False
    assert config_from(["--only", "ta"], env).collection is False


def test_only_collection_needs_both_logins_but_no_feed():
    cfg = Config(lists=(), collection=True, flaresolverr_url="http://fs", report=False)
    assert cfg.missing() == ["DEKU_EMAIL", "DEKU_PASSWORD", "DEKU_WISHLIST_ID", "TA_GAMERTAG", "TA_PASSWORD"]


def test_config_ta_read_after():
    env = {"DEKU_EMAIL": "e", "TA_GAMERTAG": "g"}
    assert config_from([], env).ta_read_after is None
    assert config_from([], {**env, "WSYNC_TA_READ_AFTER": "12:30"}).ta_read_after == dtime(12, 30)
    assert config_from([], {**env, "WSYNC_TA_READ_AFTER": " 7:05 "}).ta_read_after == dtime(7, 5)
    for bad in ("12", "25:00", "12:60", "noon"):
        with pytest.raises(ConfigError):
            config_from([], {**env, "WSYNC_TA_READ_AFTER": bad})


def test_collection_360_dry_run_defaults_on_and_follows_the_other_switches():
    env = {"DEKU_EMAIL": "e", "TA_GAMERTAG": "g"}
    assert config_from(["--apply"], env).collection_360_dry_run is True  # on by default
    live = {**env, "COLLECTION_DRY_RUN": "false", "COLLECTION_360_DRY_RUN": "false"}
    assert config_from(["--apply"], live).collection_360_dry_run is False
    assert config_from(["--apply"], {**live, "COLLECTION_DRY_RUN": "true"}).collection_360_dry_run is True
    assert config_from([], live).collection_360_dry_run is True  # a dry run is dry for 360 games too


# --------------------------------------------------------------------------- library step switches


def test_config_library_switches():
    env = {"DEKU_EMAIL": "e", "TA_GAMERTAG": "g"}
    cfg = config_from([], env)
    assert cfg.library is True and cfg.library_dry_run is True
    # 12:30: just after TA's own daily purchase import, so the pass doesn't look up and write what TA adds itself
    assert cfg.library_after == dtime(12, 30) and cfg.library_budget == 25 and cfg.library_lookups == 10
    assert str(cfg.xbox_token) == "/state/xbox-token.json" and cfg.xbox_refresh_token == ""
    for value in ("false", "0", "no", "off", " FALSE "):
        assert config_from(["--apply"], {**env, "LIBRARY_DRY_RUN": value}).library_dry_run is False
    for value in ("true", "", "garbage"):
        assert config_from(["--apply"], {**env, "LIBRARY_DRY_RUN": value}).library_dry_run is True
    # a dry run is dry for the library step too
    assert config_from([], {**env, "LIBRARY_DRY_RUN": "false"}).library_dry_run is True
    assert config_from(["--dry-run"], {**env, "DRY_RUN": "false", "LIBRARY_DRY_RUN": "false"}).library_dry_run is True
    only = config_from(["--only", "library"], env)
    assert only.lists == () and only.collection is False and only.library is True
    for other in ("deku", "ta", "collection"):
        assert config_from(["--only", other], env).library is False
    cfg = config_from([], {**env, "WSYNC_LIBRARY_AFTER": " 9:05 ", "WSYNC_XBOX_TOKEN": "/tmp/x/token.json",
                           "XBOX_REFRESH_TOKEN": "rt-seed-value"})
    assert cfg.library_after == dtime(9, 5) and str(cfg.xbox_token) == "/tmp/x/token.json"
    assert cfg.xbox_refresh_token == "rt-seed-value"
    for bad in ("11", "24:00", "11:75", "noon"):
        with pytest.raises(ConfigError, match="WSYNC_LIBRARY_AFTER"):
            config_from([], {**env, "WSYNC_LIBRARY_AFTER": bad})


def test_config_library_budgets_from_the_environment():
    env = {"DEKU_EMAIL": "e", "TA_GAMERTAG": "g"}
    cfg = config_from([], env)
    assert (cfg.library_lookups, cfg.library_lookups_per_day, cfg.library_budget) == (10, 40, 25)
    cfg = config_from([], {**env, "WSYNC_LIBRARY_LOOKUPS": " 4 ", "WSYNC_LIBRARY_LOOKUPS_PER_DAY": "60",
                           "WSYNC_LIBRARY_BUDGET": "0"})
    assert (cfg.library_lookups, cfg.library_lookups_per_day, cfg.library_budget) == (4, 60, 0)
    assert config_from([], {**env, "WSYNC_LIBRARY_BUDGET": ""}).library_budget == 25  # empty: the default
    for name in ("WSYNC_LIBRARY_LOOKUPS", "WSYNC_LIBRARY_LOOKUPS_PER_DAY", "WSYNC_LIBRARY_BUDGET"):
        for bad in ("-1", "ten", "2.5"):
            with pytest.raises(ConfigError, match=name):
                config_from([], {**env, name: bad})


def test_the_xbox_seed_is_a_secret_but_never_required():
    cfg = Config(**SECRETS, xbox_refresh_token="rt seed/+value")
    assert {"rt seed/+value", "rt+seed%2F%2Bvalue", "rt%20seed%2F%2Bvalue", "rt+seed/+value"} <= set(cfg.secrets())
    assert "rt seed" not in repr(cfg)
    only = Config(lists=(), collection=False, library=True, flaresolverr_url="http://fs", report=False)
    assert only.missing() == ["TA_GAMERTAG", "TA_PASSWORD"]  # no Deku login, no XBOX_REFRESH_TOKEN


def test_the_scrubber_learns_secrets_during_the_run():
    from wsync.run import RedactingFilter, Scrubber

    scrub = Scrubber(["known-secret"])
    assert scrub("a known-secret b") == "a <redacted> b"
    scrub.add("tok en/1", "", None)
    assert scrub("x tok en/1 tok+en%2F1 tok%20en%2F1 TOK+EN/1 y") == "x <redacted> <redacted> <redacted> <redacted> y"
    record = logging.LogRecord("wsync", logging.INFO, __file__, 1, "said %s and %s", ("known-secret", "late-tok"), None)
    log_filter = RedactingFilter(scrub)
    scrub.add("late-tok")  # added after the filter was made: the filter sees it
    assert log_filter.filter(record) and record.getMessage() == "said <redacted> and <redacted>"


def test_main_shares_one_live_scrubber_between_the_run_and_the_log_filter(monkeypatch, capsys):
    import wsync.__main__ as main_mod

    def fake_run(cfg, *, scrub):
        scrub.add("tok-found-mid-run")
        logging.getLogger("wsync").warning("seed %s, token %s", cfg.xbox_refresh_token, "tok-found-mid-run")
        return 0

    monkeypatch.setattr(main_mod, "run", fake_run)
    monkeypatch.setattr(main_mod.os, "environ", {"DEKU_EMAIL": "me@example.invalid", "TA_GAMERTAG": "Fake Gamer",
                                                 "XBOX_REFRESH_TOKEN": "rt-seed-Kq7"})
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        assert main_mod.main(["--no-report"]) == 0
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
    err = capsys.readouterr().err
    assert "seed <redacted>, token <redacted>" in err
    assert "rt-seed-Kq7" not in err and "tok-found-mid-run" not in err


def test_one_meter_shared_by_every_client_in_a_run(tmp_path):
    # the feed is missing, so the wishlist step returns early and the library and collection steps build their own
    h = Harness(tmp_path, records=None, deku=FakeDeku({}, {}), ta=FakeTA({}, {}), library=True, collection=True)
    h.run()  # the library step then fails at its Microsoft client; its TA client was built first
    assert h.made.count("ta") == 2 and len(h.meters) == 2
    assert h.meters[0] is not None and h.meters[0] is h.meters[1]
    assert h.meters[0].counts is not None


def test_run_logs_the_meter_summary(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = basic(tmp_path, dry_run=True)
    h.run()
    lines = [r.getMessage() for r in caplog.records if "this run" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("ta r")  # "ta requests: ", once per run (the test secrets scrub letters)
    assert "this run 0; today 0 of 300" in lines[0]


def test_meter_counts_are_saved_with_the_state_and_the_budgets_come_from_the_config(tmp_path):
    h = basic(tmp_path, dry_run=True, ta_budget_wishlist=7, ta_daily_budget=9)
    h.run()
    meter = h.meters[0]
    assert meter.admit(7, "wishlist") and not meter.admit(8, "wishlist") and not meter.admit(10, "library")
    assert load_state(h.cfg.state).ta_requests == {"day": "2026-11-01", "total": 0, "by": {}}


def test_config_reads_the_ta_meter_settings_from_the_environment():
    cfg = config_from([], {"WSYNC_TA_GAP": "1.5", "WSYNC_TA_JITTER": "0", "WSYNC_TA_DAILY_BUDGET": "50",
                           "WSYNC_TA_BUDGET_WISHLIST": "5", "WSYNC_TA_BUDGET_LIBRARY": "6",
                           "WSYNC_TA_BUDGET_COLLECTION": "7", "WSYNC_TA_BUDGET_OTHER": "8"})
    assert (cfg.ta_gap, cfg.ta_jitter, cfg.ta_daily_budget) == (1.5, 0.0, 50)
    assert (cfg.ta_budget_wishlist, cfg.ta_budget_library, cfg.ta_budget_collection, cfg.ta_budget_other) == (5, 6, 7, 8)
    default = config_from([], {})
    assert (default.ta_gap, default.ta_jitter, default.ta_daily_budget) == (4.0, 3.0, 300)
    assert (default.ta_budget_wishlist, default.ta_budget_library, default.ta_budget_collection,
            default.ta_budget_other) == (80, 160, 60, 20)
    with pytest.raises(ConfigError):
        config_from([], {"WSYNC_TA_GAP": "fast"})


# --------------------------------------------------------------------------- the TA request budget

BUDGET_TITLE = "TrueAchievements request budget spent"


def spent_today(**by):
    """`state.ta_requests` for NOW's UTC day with these steps' requests already made (by earlier runs)."""
    return {"day": "2026-11-01", "total": sum(by.values()), "by": dict(by)}


def budget_pings(h):
    return [b for t, b in h.reporter.notes if t == BUDGET_TITLE]


def failure_pings(h):
    return [b for t, b in h.reporter.notes if "failed" in t.lower()]


def warnings(caplog, prefix):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and r.getMessage().startswith(prefix)]


def test_budget_spent_before_the_wishlist_read_leaves_the_ta_list_unsynced_and_its_tasks_untouched(tmp_path, caplog):
    """A refusing meter (the day's wishlist share was spent by earlier runs): no TA read, lookup or write; the TA list
    is left as --only leaves a disabled one, so its problem tasks are neither opened nor closed; the run exits 0;
    one budget task opens."""
    caplog.set_level(logging.INFO)
    records = [feed_record("PA", "Alpha Quest"), feed_record("PB", "Beta Saga"), feed_record("PN", "Nowhere Game")]
    h = basic(tmp_path, dry_run=False, records=records)
    h.metered = True
    save_state(h.cfg.state, State(ta_requests=spent_today(wishlist=80)))
    assert h.run() == 0
    assert h.ta.wishlist_calls == 0 and h.ta.searches == [] and h.ta.fetched == [] and h.ta.writes == []
    problems, keep = h.reporter.problem_syncs[-1]
    assert "unmatched-ta" in keep and not any(p.kind == "unmatched-ta" for p in problems)
    assert [w[0] for w in h.deku.writes] == ["add", "update", "remove"]  # Deku is synced as usual
    assert any(p.product_id == "PN" and p.kind == "unmatched-deku" for p in problems)
    assert failure_pings(h) == []
    assert warnings(caplog, "ta: not synced this run")
    (body,) = h.reporter.budget_tasks
    assert "wishlist 80/80" in body
    assert len(budget_pings(h)) == 1
    state = load_state(h.cfg.state)
    assert state.ta_budget_task == "2026-11-01"
    assert state.last_feed_count == 3  # a paused list is a disabled one for the shrink guard: Deku's read counts


def test_a_ta_pause_met_by_the_wishlist_read_is_a_quiet_stop(tmp_path):
    h = basic(tmp_path, dry_run=False)
    h.ta.fail_read = BudgetSpent("the daily TrueAchievements request budget is spent (wishlist step)")
    assert h.run() == 0
    assert h.ta.searches == [] and h.ta.writes == []
    problems, keep = h.reporter.problem_syncs[-1]
    assert "unmatched-ta" in keep and not any(p.kind == "unmatched-ta" for p in problems)
    assert failure_pings(h) == [] and [w[0] for w in h.deku.writes] == ["add", "update", "remove"]


def test_budget_spent_during_ta_matching_leaves_the_ta_list_unsynced(tmp_path, caplog, monkeypatch):
    """The share runs out inside a lookup (one that costs more than the 4 requests its admission asked room for): the
    refused lookup isn't retried, the rest aren't looked up, and the TA list is left as a disabled one (no TA write,
    its tasks untouched); what was looked up is kept for the next run, and the cut-short lookup is neither kept as a
    miss nor backed off."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr("wsync.run.WRITE_RESERVE", 0)  # no room kept for the writes: a lookup can outrun the share
    # the read, Alpha Quest's search and page, then Beta Saga's search and three of its four pages
    h = basic(tmp_path, dry_run=False, ta_budget_wishlist=7)
    for slug, gid, title, platform in (("beta-saga-one", 103, "Beta Saga (Xbox One)", ONE),
                                       ("beta-saga-win", 104, "Beta Saga (Windows)", "Windows"),
                                       ("beta-saga-360", 105, "Beta Saga (Xbox 360)", "Xbox 360")):
        h.ta.pages[slug] = tag(slug, gid, title, platform)
    h.metered = True
    asked = []
    real = h.ta.search

    def search(q):
        asked.append(q)
        return real(q)

    h.ta.search = search
    assert h.run() == 0
    assert asked == ["Alpha Quest", "Beta Saga"]  # Beta Saga's lookup, refused its fourth page, was not retried
    assert h.ta.fetched == ["alpha-quest", "beta-saga", "beta-saga-one", "beta-saga-win"]
    assert h.ta.writes == [] and failure_pings(h) == []
    problems, keep = h.reporter.problem_syncs[-1]
    assert "unmatched-ta" in keep and not any(p.kind == "unmatched-ta" for p in problems)
    assert not [m for m in warnings(caplog, "ta: lookup for") if "retrying" in m]
    mapping = load_state(h.cfg.state).mapping
    assert mapping["PA"]["ta"]["game"] == 101
    assert "ta_miss" not in mapping.get("PB", {}) and "ta_backoff" not in mapping.get("PB", {})
    assert len(h.reporter.budget_tasks) == 1 and "wishlist 7/7" in h.reporter.budget_tasks[0]


def test_budget_spent_during_ta_writes_stops_writes_quietly(tmp_path, caplog):
    """The share holds the read and one add (both games' matches are cached, so no lookup asks for room): the second add
    finds it spent. The other writes stay planned for the next run; no issue opens per entry, nor for the skipped
    re-read."""
    caplog.set_level(logging.INFO)
    h = basic(tmp_path, dry_run=False, ta_budget_wishlist=2)
    h.metered = True
    save_state(h.cfg.state, State(mapping={
        pid: {"ta": {"game": gid, "dlc": 0, "slug": slug}, "ta_key": base_title(title), "ta_title": title}
        for pid, gid, slug, title in (("PA", 101, "alpha-quest", "Alpha Quest"),
                                      ("PB", 102, "beta-saga", "Beta Saga"))}))
    assert h.run() == 0
    assert h.ta.writes == [("add", "101")]
    assert h.ta.wishlist_calls == 1  # the verification re-read needed a request too: skipped
    assert failure_pings(h) == []
    assert [w[0] for w in h.deku.writes] == ["add", "update", "remove"]
    assert any("2 writes left for the next run" in m for m in warnings(caplog, "ta: "))
    assert len(h.reporter.budget_tasks) == 1
    h.now = NOW + timedelta(days=1)  # a new UTC day: the rest is planned again and written
    h.cfg.ta_budget_wishlist = 6
    assert h.run() == 0
    assert h.ta.writes == [("add", "101"), ("add", "102"), ("remove", "999")]
    assert h.reporter.budget_tasks[-1] is None  # a later day with room closes the budget task
    assert failure_pings(h) == []


def test_budget_spent_before_the_d2d_lists_holds_deku_format_changes_quietly(tmp_path):
    """Without a cached copy of TA's Disc to Digital lists, a refused list read keeps the cached verdicts and holds the
    Deku format changes, as a failed refresh does, but without an issue."""
    h = basic(tmp_path, dry_run=False)
    h.verdict_error = BudgetSpent("the daily TrueAchievements request budget is spent (d2d step)")
    assert h.run() == 0
    assert failure_pings(h) == []
    assert not any(w[0] == "update" for w in h.deku.writes)
    assert ("add", "alpha-quest") in [w[:2] for w in h.deku.writes]
    _, keep = h.reporter.problem_syncs[-1]
    assert "d2d-disputed" in keep


def test_a_run_inside_the_d2d_wait_without_a_copy_changes_no_deku_format_and_keeps_the_disputed_tasks(tmp_path):
    """No copy of TA's Disc to Digital lists, and the read failed an hour ago (that run's issue): the runs inside the
    24 h wait have no lists, so nothing is judged on XCT.live alone. Cached verdicts, no Deku format change, the
    d2d-disputed tasks kept, no issue; the rest of the run goes on as usual."""
    h = basic(tmp_path, dry_run=False)
    h.deku.list["beta-saga"] = ("either", frozenset({"xbox_series", "xbox_one"}))  # set by an earlier "yes" verdict
    save_state(h.cfg.state, State(ta_lists={"tried": (NOW - timedelta(hours=1)).isoformat()},
                                  d2d={"PB": {"verdict": "yes", "checked": (NOW - timedelta(days=8)).isoformat(),
                                              "xct": None}}))
    xct = []

    def unlisted(url, params):  # XCT.live knows neither game
        xct.append(params["q"])
        return {"rows": [], "shown": 0, "total": 0}

    deps = h.deps()
    deps.refresh_verdicts, deps.http_get = d2d.refresh_verdicts, unlisted  # the real lists and XCT.live logic
    assert run(h.cfg, deps) == 0
    assert failure_pings(h) == []
    assert not any(w[0] == "update" for w in h.deku.writes)  # Beta Saga stays "either"
    assert [w[:2] for w in h.deku.writes] == [("add", "alpha-quest"), ("remove", "gamma-tale")]
    _, keep = h.reporter.problem_syncs[-1]
    assert "d2d-disputed" in keep
    assert xct == []
    assert h.ta.writes == [("add", "101"), ("add", "102"), ("remove", "999")]  # the TA side is synced as usual


@pytest.mark.parametrize("spent, text", [
    (dict(wishlist=80), "wishlist 80/80"),
    (dict(library=150, collection=30, clearance=120), "ceiling 300/300"),
], ids=["wishlist-share", "ceiling"])
def test_budget_task_opens_once_a_day_and_closes_on_the_next_day_under_budget(tmp_path, spent, text):
    h = basic(tmp_path, dry_run=False)
    save_state(h.cfg.state, State(ta_requests=spent_today(**spent)))
    assert h.run() == 0
    (body,) = h.reporter.budget_tasks
    assert text in body and "2026-11-02 00:00 UTC" in body
    assert len(budget_pings(h)) == 1 and text in budget_pings(h)[0]
    h.now = NOW + timedelta(hours=1)
    assert h.run() == 0
    assert len(h.reporter.budget_tasks) == 1 and len(budget_pings(h)) == 1  # once a UTC day
    h.now = NOW + timedelta(hours=12, minutes=7)  # 00:07 UTC: the next day's first run has room
    assert h.run() == 0
    assert h.reporter.budget_tasks == [body, None] and len(budget_pings(h)) == 1
    assert load_state(h.cfg.state).ta_budget_task is None
    assert h.run() == 0 and h.reporter.budget_tasks == [body, None]  # closed once
    state = load_state(h.cfg.state)  # spent again on that day: the day's task opens, with its ping
    state.ta_requests = {**spent_today(**spent), "day": "2026-11-02"}
    save_state(h.cfg.state, state)
    assert h.run() == 0
    assert len(h.reporter.budget_tasks) == 3 and "2026-11-03 00:00 UTC" in h.reporter.budget_tasks[-1]
    assert len(budget_pings(h)) == 2
    assert failure_pings(h) == []


def test_a_failed_budget_task_update_is_an_issue_and_retried_at_the_next_run(tmp_path):
    h = basic(tmp_path, dry_run=False)
    save_state(h.cfg.state, State(ta_requests=spent_today(wishlist=80)))
    real = h.reporter.ta_budget_task

    def down(body):
        raise RuntimeError("vikunja down")

    h.reporter.ta_budget_task = down
    assert h.run() == 1 and "Vikunja" in failure_pings(h)[0] and budget_pings(h) == []
    assert load_state(h.cfg.state).ta_budget_task is None
    h.reporter.ta_budget_task = real
    h.now = NOW + timedelta(hours=1)
    assert h.run() == 0 and len(h.reporter.budget_tasks) == 1 and len(budget_pings(h)) == 1
    assert load_state(h.cfg.state).ta_budget_task == "2026-11-01"


def test_a_run_without_a_reporter_leaves_the_budget_task_to_a_run_with_one(tmp_path):
    from wsync.run import NullReporter

    h = basic(tmp_path, dry_run=True)
    save_state(h.cfg.state, State(ta_requests=spent_today(wishlist=80)))
    deps = h.deps()
    deps.make_reporter = lambda cfg: NullReporter()  # --no-report
    assert run(h.cfg, deps) == 0
    assert load_state(h.cfg.state).ta_budget_task is None  # no task was opened, so none is recorded
    assert h.run() == 0 and len(h.reporter.budget_tasks) == 1  # the day's task opens at the next run with a reporter


def test_a_wishlist_share_of_0_stops_the_ta_side_without_a_budget_task(tmp_path, caplog):
    """WSYNC_TA_BUDGET_WISHLIST=0, a way to stop the TA wishlist traffic: the TA list sits out each run quietly, and a 0
    share is a setting, not a budget used up: no budget task, no ping, no "spent" warning."""
    caplog.set_level(logging.INFO)
    h = basic(tmp_path, dry_run=False, ta_budget_wishlist=0)
    h.metered = True
    for hours in (0, 1):
        h.now = NOW + timedelta(hours=hours)
        assert h.run() == 0
    assert h.ta.wishlist_calls == 0 and h.ta.writes == []
    assert warnings(caplog, "ta: not synced this run")
    assert not [m for m in warnings(caplog, "ta budget:") if "spent today" in m]
    assert h.reporter.budget_tasks == [] and budget_pings(h) == [] and failure_pings(h) == []


def test_login_backoff_is_a_quiet_stop_with_one_issue_per_episode(tmp_path):
    h = basic(tmp_path, dry_run=False)
    h.ta.fail_read = LoginBackoff("TrueAchievements login skipped: the last one failed less than 24 h ago")
    assert h.run() == 0  # a held-back login is a quiet stop, as a spent budget is
    assert failure_pings(h) == []
    guard = h.guards[-1]
    assert guard.allowed()
    # the first failed login of an episode raises the one issue ...
    guard.failed(HttpError("POST /login answered HTTP 403", 403))
    assert len(guard._run.issues) == 1
    state = guard._run.state.ta_login
    assert set(state) == {"failed", "fingerprint", "issued"} and SECRETS["ta_password"] not in json.dumps(state)
    assert not guard.allowed()
    # ... a later failure of the same episode (after the 24 h) raises none
    guard._run.issues.clear()
    h.now = h.now + timedelta(hours=25)
    assert guard.allowed()
    guard.failed(HttpError("POST /login answered HTTP 403", 403))
    assert guard._run.issues == []
    # a working login ends the episode: the next failure is a new one
    guard.succeeded()
    assert guard._run.state.ta_login == {}
    guard.failed(HttpError("POST /login answered HTTP 403", 403))
    assert len(guard._run.issues) == 1


# --------------------------------------------------------------------------- stored TA lookup results


def at(day, hour, minute=0):
    """A moment in November 2026 (UTC)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def run_at(h, when):
    """One run at `when`: the TA searches and game page fetches it made."""
    h.now = when
    h.ta.searches.clear()
    h.ta.fetched.clear()
    assert h.run() == 0
    return list(h.ta.searches), list(h.ta.fetched)


def ta_entry(h, pid):
    return load_state(h.cfg.state).mapping.get(pid, {})


def test_placeholder_miss_is_reused_until_due_and_keeps_its_marker(tmp_path, caplog):
    """A game with only a pre-release placeholder page on TA is looked up once, then its stored result is used without
    a request until it's due, with the placeholder marker intact: a "no page yet" skip, no task, no hold on removals."""
    caplog.set_level(logging.INFO)
    h = basic(tmp_path, dry_run=True, records=[feed_record("PP", "Delta Dawn", kind="concrete")], lists=("ta",))
    h.ta.pages["delta-dawn"] = tag("delta-dawn", -5150, "Delta Dawn")
    assert run_at(h, NOW) == (["Delta Dawn"], ["delta-dawn"])
    assert ta_entry(h, "PP")["ta_miss"] == {"status": "none", "candidates": [PLACEHOLDER], "checked": NOW.isoformat(),
                                            "key": ["delta dawn", "concrete", "game", None]}
    # PP's weekly slot is Sunday 21:00: the first one at least a day after the check (Sunday 11-01 12:00) is 11-08
    caplog.clear()
    assert run_at(h, at(8, 20, 59)) == ([], [])
    assert "ta match: Delta Dawn -> none [pre-release placeholder page]" in caplog.text
    plan = h.reporter.reviews[-1][0]
    assert plan.skipped_unannounced == ["Delta Dawn"]
    assert [e.target for e in plan.ta.removes] == ["999"] and plan.ta.paused_removes == []
    assert not any(p.product_id == "PP" for p in h.reporter.problems) and h.reporter.keep_keys == set()
    assert run_at(h, at(8, 21)) == (["Delta Dawn"], ["delta-dawn"])


def test_released_miss_rechecks_daily_prerelease_weekly_in_its_hour_slot(tmp_path):
    """A kept miss is due at the start of the product's first slot at least a day after the hour it was checked in. A
    released game's slot is its hour of the day (sha256 of the productId, mod 24, UTC), that of a game not out yet its
    hour of the week (mod 168, from Monday 00:00 UTC), in the same hour of the day. PT's daily slot is 02:00; PY's
    weekly slot is Sunday 04:00."""
    records = [feed_record("PT", "Nowhere Game"), feed_record("PY", "Delta Dawn", kind="estimated")]
    h = basic(tmp_path, dry_run=True, records=records)
    h.ta.pages["delta-dawn"] = tag("delta-dawn", -5150, "Delta Dawn")
    assert run_at(h, NOW)[0] == ["Nowhere Game", "Delta Dawn"]  # 11-01 12:00
    # released: a day after (11-02 12:00), rounded up to 02:00
    assert run_at(h, at(3, 1, 59))[0] == []
    assert run_at(h, at(3, 2, 7))[0] == ["Nowhere Game"]
    # checked in its slot: due again a day later, at the next 02:00
    assert run_at(h, at(4, 1, 59))[0] == []
    assert run_at(h, at(4, 2))[0] == ["Nowhere Game"]
    # not out yet: the first Sunday 04:00 at least a day after the check (Sunday 11-01 12:00) is 11-08
    assert "Delta Dawn" not in run_at(h, at(8, 3, 59))[0]
    assert "Delta Dawn" in run_at(h, at(8, 4, 7))[0]
    # checked in its slot: due again exactly a week later
    assert "Delta Dawn" not in run_at(h, at(15, 3, 59))[0]
    assert "Delta Dawn" in run_at(h, at(15, 4))[0]


def test_weekly_due_lands_on_the_products_slot_and_repeats_exactly_weekly():
    """A game not out yet (any kind but "released") is re-checked in its hour of the week: PP's is Sunday 21:00, PY's
    Sunday 04:00, PZ's Monday 08:00, PT's Tuesday 02:00. Re-checked in its slot (by the run at :07), it's due again
    exactly a week later."""
    from wsync.run import recheck_due

    for pid, slot in {"PP": (6, 21), "PY": (6, 4), "PZ": (0, 8), "PT": (1, 2)}.items():
        due = recheck_due(pid, "concrete", NOW)  # checked on Sunday 11-01 at 12:00
        assert (due.weekday(), due.hour, due.minute, due.second) == (*slot, 0, 0)
        assert timedelta(days=1) <= due - NOW < timedelta(days=8)
        for kind in ("unannounced", "estimated", "concrete", ""):
            nxt = recheck_due(pid, kind, due + timedelta(minutes=7))
            assert nxt - due == timedelta(days=7)
            due = nxt


def test_weekly_recheck_after_a_check_outside_the_slot_comes_1_to_8_days_later_in_the_slot():
    """Never sooner than a day after the hour of the check, so a check just before the slot waits for the week after.
    PZ's slot is Monday 08:00 (11-02 and 11-09 are Mondays)."""
    from wsync.run import recheck_due

    assert recheck_due("PZ", "estimated", at(7, 12)) == at(9, 8)  # Saturday noon: 1 day 20 h later
    assert recheck_due("PZ", "estimated", at(9, 7, 30)) == at(16, 8)  # half an hour before the slot: a week after
    assert recheck_due("PZ", "estimated", at(9, 9)) == at(16, 8)  # an hour after the slot: 6 days 23 h later
    for hours in range(2 * 7 * 24):  # a check at every hour of two weeks
        checked = at(2, 0) + timedelta(hours=hours)
        due = recheck_due("PZ", "estimated", checked)
        assert (due.weekday(), due.hour, due.minute) == (0, 8, 0)
        if (checked.weekday(), checked.hour) == (0, 8):
            assert due - checked == timedelta(days=7)
        else:
            assert timedelta(days=1) <= due - checked < timedelta(days=8)


def test_weekly_slots_spread_the_rechecks_of_one_run_over_the_week():
    """20 games not out yet, all first looked up in one run (a deploy), come back on their own weekdays, the same ones
    whatever hour that run was. 20 over 7 days is about 3 a day; these synthetic ids put at most 6 on one day (5 on
    Tuesday, 6 on Friday in their slots) and at least one on every day. Before, all 20 came back on the same day each
    week."""
    from collections import Counter

    from wsync.run import recheck_due

    pids = [f"PX{i:02d}" for i in range(20)]
    weekdays = None
    for hour in range(24):  # the hour of the run that first looked them up
        first = [recheck_due(pid, "concrete", at(1, hour, 7)) for pid in pids]
        assert max(Counter(d.date() for d in first).values()) <= 6
        days = Counter(d.weekday() for d in first)
        assert len(days) == 7 and weekdays in (None, days)
        weekdays = days


def test_released_rechecks_keep_the_daily_hour_slot():
    """A released game is re-checked daily in its hour of the day (PT's is 02:00): a day after a re-check made in the
    slot, 1-2 days after a check outside it. It's the same hour of the day as its weekly slot before release."""
    from wsync.run import recheck_due

    assert recheck_due("PT", "released", at(3, 2, 7)) == at(4, 2)
    assert recheck_due("PT", "released", at(3, 14, 7)) == at(5, 2)
    for hours in range(2 * 24):
        checked = at(3, 0, 30) + timedelta(hours=hours)
        due = recheck_due("PT", "released", checked)
        assert (due.hour, due.minute) == (2, 0)
        assert timedelta(hours=23) < due - checked < timedelta(days=2)
    for pid in ("PP", "PY", "PZ", "PT", "PA", "PN"):
        assert recheck_due(pid, "released", NOW).hour == recheck_due(pid, "concrete", NOW).hour


def test_changed_kind_title_or_override_forces_a_lookup(tmp_path):
    """A stored miss is keyed on the base title, the feed's kind and product kind, and the TA override: a change to any
    of them, an override's removal included, means a lookup at once."""
    h = basic(tmp_path, dry_run=True, records=[feed_record("PY", "Delta Dawn", kind="estimated")])
    h.ta.pages["delta-dawn"] = tag("delta-dawn", -5150, "Delta Dawn")

    def feed(title="Delta Dawn", kind="estimated", product_kind="game"):
        write_feed(tmp_path, [feed_record("PY", title, kind=kind, product_kind=product_kind)])

    hour = iter(range(1, 100))
    later = lambda: NOW + timedelta(hours=next(hour))  # noqa: E731 - one run an hour

    assert run_at(h, NOW) == (["Delta Dawn"], ["delta-dawn"])
    assert run_at(h, later()) == ([], [])
    feed(kind="concrete")  # the release state
    assert run_at(h, later()) == (["Delta Dawn"], ["delta-dawn"])
    assert run_at(h, later()) == ([], [])
    feed(title="Delta Dawn Deluxe Edition", kind="concrete")  # the same base title
    assert run_at(h, later()) == ([], [])
    feed(title="Delta Dawn II", kind="concrete")  # another title
    assert run_at(h, later())[0] == ["Delta Dawn II"]
    assert run_at(h, later()) == ([], [])
    feed(title="Delta Dawn II", kind="concrete", product_kind="bundle")  # the product kind
    assert run_at(h, later())[0] == ["Delta Dawn II"]
    assert run_at(h, later()) == ([], [])
    h.cfg.overrides.write_text(json.dumps({"PY": {"ta": "delta-dawn"}}))  # an override
    assert run_at(h, later()) == ([], ["delta-dawn"])
    assert run_at(h, later()) == ([], [])
    h.cfg.overrides.write_text(json.dumps({}))  # the override removed
    assert run_at(h, later())[0] == ["Delta Dawn II"]
    assert run_at(h, later()) == ([], [])


def test_related_to_stale_still_applies_to_reused_results(tmp_path):
    """The stored result is the raw one, from before the ambiguity rule, and the rule still runs on it every run: a game
    with no page that is related to an entry about to be removed is ambiguous while the entry is there, and a "no page
    yet" skip once it's gone, without a new search."""
    records = [feed_record("PB", "Beta Saga"), feed_record("PZ", "Zeta Unseen", kind="unannounced")]
    h = basic(tmp_path, dry_run=True, records=records)
    h.ta.list = {"102": "Beta Saga", "888": "Zeta Unseen Prologue"}
    assert run_at(h, NOW)[0] == ["Beta Saga", "Zeta Unseen"]
    miss = ta_entry(h, "PZ")["ta_miss"]
    assert (miss["status"], miss["candidates"]) == ("none", [])
    for hours in (1, 2):
        assert run_at(h, NOW + timedelta(hours=hours)) == ([], [])
        plan = h.reporter.reviews[-1][0]
        assert plan.ta.removes == [] and [e.target for e in plan.ta.paused_removes] == ["888"]
        assert any(p.product_id == "PZ" and "Zeta Unseen Prologue" in p.detail for p in h.reporter.problems)
    del h.ta.list["888"]  # removed on TrueAchievements by hand
    assert run_at(h, NOW + timedelta(hours=3)) == ([], [])
    assert not any(p.product_id == "PZ" for p in h.reporter.problems)
    assert h.reporter.reviews[-1][0].skipped_unannounced == ["Zeta Unseen"]


def test_reused_results_ignore_match_budget_and_lst_stop(tmp_path):
    """Stored and cached results need no request: they're used past the lookup time budget and after repeated lookup
    errors stopped the list's lookups, while a game that needs a lookup waits (transient)."""
    records = [feed_record("PN", "Nowhere Game"), feed_record("PA", "Alpha Quest")]
    h = basic(tmp_path, dry_run=True, records=records, lists=("ta",), match_budget=10.0)
    assert run_at(h, NOW)[0] == ["Nowhere Game", "Alpha Quest"]

    def reused_as_before():
        plan = h.reporter.reviews[-1][0]
        assert [e.target for e in plan.ta.adds] == ["101"]
        assert any(p.product_id == "PN" and p.detail == "no page found" for p in h.reporter.problems)

    # past the lookup time budget (the read takes a minute)
    write_feed(tmp_path, [feed_record("PQ", "Fresh Find"), *records])
    real_read = h.ta.wishlist

    def slow_read():
        h.clock += 60
        return real_read()

    h.ta.wishlist = slow_read
    assert run_at(h, NOW + timedelta(hours=1)) == ([], [])
    reused_as_before()
    assert h.reporter.keep_keys == {"PQ:unmatched-ta"}
    h.ta.wishlist = real_read

    # three lookup errors in a row stop the list's lookups
    broken = [feed_record(f"PF{i}", f"Broken Game {i}") for i in range(3)]
    write_feed(tmp_path, [*broken, feed_record("PQ", "Fresh Find"), *records])
    real_search = h.ta.search

    def search(q):
        if q.startswith("Broken"):
            h.ta.searches.append(q)
            raise Unreachable("network error: OSError")
        return real_search(q)

    h.ta.search = search
    assert run_at(h, NOW + timedelta(hours=2))[0] == ["Broken Game 0", "Broken Game 1", "Broken Game 2"]
    reused_as_before()
    assert h.reporter.keep_keys == {"PF0:unmatched-ta", "PF1:unmatched-ta", "PF2:unmatched-ta", "PQ:unmatched-ta"}


@pytest.mark.parametrize("error, retried", [
    (Unreachable("network error: OSError"), False),
    (PageUnavailable("game page 'x' is neither a game page nor TA's page-not-found page (no game id)"), False),
    (HttpError("GET /search-results answered HTTP 429", 429), False),
    (HttpError("GET /search-results answered HTTP 503", 503), False),
    (TAError("search page has no results area (layout changed?)"), False),
    (FlareSolverrError("FlareSolverr request.get failed: challenge timeout"), True),
], ids=["unreachable", "page-unavailable", "http-429", "http-503", "unreadable-page", "flaresolverr"])
def test_ta_lookup_failure_is_not_retried_in_the_same_run_but_flaresolverr_errors_are(tmp_path, error, retried):
    h = basic(tmp_path, dry_run=False)
    real = h.ta.search
    calls = []

    def flaky(q):
        calls.append(q)
        if len(calls) == 1:
            raise error
        return real(q)

    h.ta.search = flaky
    assert h.run() == 0
    if retried:  # FlareSolverr's clearances time out at random: one more try at once
        assert calls == ["Alpha Quest", "Alpha Quest", "Beta Saga"]
        assert h.ta.writes == [("add", "101"), ("add", "102"), ("remove", "999")]
        assert "ta_backoff" not in ta_entry(h, "PA")
    else:  # TrueAchievements isn't asked again this run: the game waits (removals pause, its task is kept)
        assert calls == ["Alpha Quest", "Beta Saga"]
        assert h.ta.writes == [("add", "102")]
        assert h.reporter.keep_keys == {"PA:unmatched-ta"}
        assert ta_entry(h, "PA")["ta_backoff"]["failures"] == 1


def test_a_failed_deku_lookup_is_still_retried_once(tmp_path):
    h = basic(tmp_path, dry_run=False)
    real = h.deku.search
    calls = []

    def flaky(q):
        calls.append(q)
        if len(calls) == 1:
            raise DekuError("HTTP 502")
        return real(q)

    h.deku.search = flaky
    assert h.run() == 0
    assert calls[:2] == ["Alpha Quest", "Alpha Quest"]
    assert ("add", "alpha-quest") in [w[:2] for w in h.deku.writes]


def test_transient_failure_backs_off_1_2_4_hours_up_to_a_day_and_stays_transient(tmp_path):
    """After each failed lookup in a row the game waits 1, 2, 4 ... hours (at most 24), to the hour, so the hourly run
    after the delay tries it. Meanwhile it's transient without a request: TA removals pause, its task is kept."""
    h = basic(tmp_path, dry_run=True, records=[feed_record("PA", "Alpha Quest")])

    def down(q):
        h.ta.searches.append(q)
        raise Unreachable("network error: OSError")

    h.ta.search = down
    tries = [(at(1, 12, 7), at(1, 13)), (at(1, 13, 7), at(1, 15)), (at(1, 15, 7), at(1, 19)), (at(1, 19, 7), at(2, 3)),
             (at(2, 3, 7), at(2, 19)), (at(2, 19, 7), at(3, 19)), (at(3, 19, 7), at(4, 19))]
    for n, (when, until) in enumerate(tries, start=1):
        assert run_at(h, when)[0] == ["Alpha Quest"]
        assert ta_entry(h, "PA")["ta_backoff"] == {"failures": n, "until": until.isoformat(),
                                                   "key": ["alpha quest", "released", "game", None]}
        assert run_at(h, until - timedelta(minutes=1)) == ([], [])
        plan = h.reporter.reviews[-1][0]
        assert plan.ta.removes == [] and [e.target for e in plan.ta.paused_removes] == ["999"]
        assert not any(p.product_id == "PA" for p in h.reporter.problems)
        assert h.reporter.keep_keys == {"PA:unmatched-ta"}


def test_a_changed_key_ends_a_back_off_at_once(tmp_path):
    """A failed lookup's back-off is kept with the item's key, as a kept miss is: a change to the base title, the feed's
    kind or product kind, or the TA override means a lookup at once, and a new key's failures count from 1."""
    h = basic(tmp_path, dry_run=True, records=[feed_record("PA", "Alpha Quest", kind="concrete")], lists=("ta",))
    real = h.ta.search

    def down(q):
        h.ta.searches.append(q)
        raise Unreachable("network error: OSError")

    h.ta.search = down
    assert run_at(h, at(1, 12, 7))[0] == ["Alpha Quest"]
    assert ta_entry(h, "PA")["ta_backoff"] == {"failures": 1, "until": at(1, 13).isoformat(),
                                               "key": ["alpha quest", "concrete", "game", None]}
    assert run_at(h, at(1, 12, 30)) == ([], [])  # backed off: no request
    write_feed(tmp_path, [feed_record("PA", "Alpha Quest", kind="released")])  # out now: another key
    assert run_at(h, at(1, 12, 40))[0] == ["Alpha Quest"]
    assert ta_entry(h, "PA")["ta_backoff"] == {"failures": 1, "until": at(1, 13).isoformat(),
                                               "key": ["alpha quest", "released", "game", None]}
    assert run_at(h, at(1, 12, 50)) == ([], [])
    h.cfg.overrides.write_text(json.dumps({"PA": {"ta": "alpha-quest"}}))  # an override: at once too
    h.ta.search = real
    assert run_at(h, at(1, 12, 55)) == ([], ["alpha-quest"])
    assert "ta_backoff" not in ta_entry(h, "PA")
    assert [e.target for e in h.reporter.reviews[-1][0].ta.adds] == ["101"]


def test_placeholder_override_reused_weekly(tmp_path):
    """An override whose page is still a pre-release placeholder is fetched once, then reused on the stored-miss
    schedule (weekly in PP's slot, Sunday 21:00, before release; daily at 21:00 after), keyed on the override and
    the release state. It is "no page yet": no task, and TA removals go ahead."""
    h = basic(tmp_path, dry_run=True, records=[feed_record("PP", "Delta Dawn", kind="concrete")], lists=("ta",),
              overrides={"PP": {"ta": "delta-dawn"}})
    h.ta.pages["delta-dawn"] = tag("delta-dawn", -5150, "Delta Dawn")
    assert run_at(h, NOW) == ([], ["delta-dawn"])
    assert ta_entry(h, "PP")["ta_miss"] == {"status": "none", "candidates": [PLACEHOLDER], "checked": NOW.isoformat(),
                                            "key": ["delta dawn", "concrete", "game", {"ta": "delta-dawn"}]}
    assert run_at(h, at(8, 20, 59)) == ([], [])
    plan = h.reporter.reviews[-1][0]
    assert [e.target for e in plan.ta.removes] == ["999"] and plan.ta.paused_removes == []
    assert not any(p.product_id == "PP" for p in h.reporter.problems) and "PP:unmatched-ta" not in h.reporter.keep_keys
    assert run_at(h, at(8, 21)) == ([], ["delta-dawn"])
    write_feed(tmp_path, [feed_record("PP", "Delta Dawn", kind="released")])  # out: looked up at once, then daily
    assert run_at(h, at(8, 21, 30)) == ([], ["delta-dawn"])
    assert run_at(h, at(9, 20, 59)) == ([], [])
    assert run_at(h, at(9, 21)) == ([], ["delta-dawn"])
    h.cfg.overrides.write_text(json.dumps({"PP": {"ta": "delta-dawn-xs"}}))  # another page: at once
    h.ta.pages["delta-dawn-xs"] = tag("delta-dawn-xs", 4242, "Delta Dawn (Xbox Series X|S)")
    assert run_at(h, at(9, 21, 30)) == ([], ["delta-dawn-xs"])
    assert [e.target for e in h.reporter.reviews[-1][0].ta.adds] == ["4242"]
    assert "ta_miss" not in ta_entry(h, "PP")


def test_placeholder_override_is_no_page_yet_and_pins_its_item(tmp_path):
    """An override names the item's TA page. A pre-release placeholder there is "no page yet", as for an unannounced
    game: no task and no removal pause. Placeholders can't be wishlisted, and the override pins the item, so a listed
    entry whose title merely looks related ("Old Game" ~ "Old Game Revelation") isn't the item's and goes."""
    h = basic(tmp_path, dry_run=True, records=[feed_record("PP", "Old Game Revelation", kind="concrete")],
              lists=("ta",), overrides={"PP": {"ta": "old-game-revelation"}})
    h.ta.pages["old-game-revelation"] = tag("old-game-revelation", -7000, "Old Game Revelation")
    for when in (NOW, at(1, 13)):  # fetched once, then the kept placeholder (no request): the same plan
        run_at(h, when)
        plan = h.reporter.reviews[-1][0]
        assert [e.target for e in plan.ta.removes] == ["999"] and plan.ta.paused_removes == []
        assert not any(p.product_id == "PP" for p in h.reporter.problems)
        assert "PP:unmatched-ta" not in h.reporter.keep_keys


def test_match_clears_stored_miss_and_backoff(tmp_path):
    h = basic(tmp_path, dry_run=True, records=[feed_record("PA", "Alpha Quest")])
    page = h.ta.pages.pop("alpha-quest")
    assert run_at(h, NOW)[0] == ["Alpha Quest"]  # no page on TA yet
    assert ta_entry(h, "PA")["ta_miss"]["status"] == "none"
    real = h.ta.search

    def down(q):
        h.ta.searches.append(q)
        raise Unreachable("network error: OSError")

    h.ta.search = down  # PA's hour slot is 18: due a day after the check (11-02 12:00), at 18:00; that lookup fails
    assert run_at(h, at(2, 18))[0] == ["Alpha Quest"]
    entry = ta_entry(h, "PA")
    assert entry["ta_backoff"] == {"failures": 1, "until": at(2, 19).isoformat(),
                                   "key": ["alpha quest", "released", "game", None]} and "ta_miss" in entry
    h.ta.search = real
    h.ta.pages["alpha-quest"] = page  # the page is there now
    assert run_at(h, at(2, 19))[0] == ["Alpha Quest"]
    entry = ta_entry(h, "PA")
    assert entry["ta"]["game"] == 101 and "ta_miss" not in entry and "ta_backoff" not in entry
    assert [e.target for e in h.reporter.reviews[-1][0].ta.adds] == ["101"]


def test_a_lookup_without_room_in_the_ta_budget_is_deferred_not_stored_or_backed_off(tmp_path, caplog):
    """Admission: a fresh lookup first asks the meter for room for its 4 requests under the wishlist step, plus the 10
    kept for the TA writes. Without it the game waits for a later run (transient: no request, nothing stored, no
    back-off); stored results are still used and the list is synced as far as it goes. A warning, no issue."""
    caplog.set_level(logging.INFO)
    h = basic(tmp_path, dry_run=True, records=[feed_record("PN", "Nowhere Game")], lists=("ta",))
    h.metered = True
    assert run_at(h, NOW)[0] == ["Nowhere Game"]
    write_feed(tmp_path, [feed_record("PN", "Nowhere Game"), feed_record("PQ", "Fresh Find")])
    h.cfg.ta_budget_wishlist = 4  # a new day: the read leaves room for 3
    assert run_at(h, at(2, 1)) == ([], [])
    state = load_state(h.cfg.state)
    assert state.ta_requests["by"] == {"wishlist": 1}
    assert "ta_miss" not in state.mapping.get("PQ", {}) and "ta_backoff" not in state.mapping.get("PQ", {})
    assert any(p.product_id == "PN" and p.detail == "no page found" for p in h.reporter.problems)  # the list synced
    assert h.reporter.keep_keys == {"PQ:unmatched-ta"}
    assert any("1 lookup deferred" in m for m in warnings(caplog, "ta: "))
    assert failure_pings(h) == [] and h.reporter.budget_tasks == []


def test_a_lookup_leaves_10_requests_of_the_wishlist_share_for_the_writes(tmp_path, caplog):
    """A fresh lookup is admitted only while 10 requests of the wishlist share stay free after its 4, so the lookups
    can't spend what the TA adds and removes need: with 13 left after the read the new game waits for a later run and
    the cached game's add still goes out; with 14 left the lookup is made."""
    caplog.set_level(logging.INFO)
    records = [feed_record("PA", "Alpha Quest"), feed_record("PQ", "Fresh Find")]
    h = basic(tmp_path, dry_run=False, records=records, lists=("ta",), ta_budget_wishlist=14)
    h.ta.pages["fresh-find"] = tag("fresh-find", 103, "Fresh Find")
    h.metered = True
    save_state(h.cfg.state, State(mapping={"PA": {"ta": {"game": 101, "dlc": 0, "slug": "alpha-quest"},
                                                  "ta_key": base_title("Alpha Quest"), "ta_title": "Alpha Quest"}}))
    assert h.run() == 0
    assert h.ta.searches == [] and h.ta.writes == [("add", "101")]  # 999's removal waits while Fresh Find does
    assert h.reporter.keep_keys == {"PQ:unmatched-ta"}
    assert any("1 lookup deferred" in m for m in warnings(caplog, "ta: "))
    assert load_state(h.cfg.state).ta_requests["by"] == {"wishlist": 3}  # the read, the add and its re-read
    h.now = NOW + timedelta(days=1)  # a new UTC day
    h.cfg.ta_budget_wishlist = 15
    assert h.run() == 0
    assert h.ta.searches == ["Fresh Find"]
    assert h.ta.writes == [("add", "101"), ("add", "103"), ("remove", "999")]
    assert failure_pings(h) == [] and h.reporter.budget_tasks == []

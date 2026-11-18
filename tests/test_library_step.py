"""The library step (Microsoft licences and orders -> TrueAchievements Game Collection), driven through `run()`
with fakes: a FakeXbox for Microsoft's endpoints and test_run's FakeTA. Every title, product ID, XUID and token
here is fictional."""

import json
import logging
import os
import stat
from datetime import time as dtime, timedelta
from urllib.parse import parse_qs, urlsplit

from test_run import NOW, FakeDeku, FakeTA, Harness, basic
from wsync import xbox
from wsync.deku import OwnedDetail
from wsync.errors import TAError
from wsync.run import Terminated
from wsync.state import State, load_state, save_state
from wsync.ta import SERIES, IncompleteRead, TAGame, Unreachable
from wsync.ta_coll import OwnedRow

XUID = "2533270000004242"
SEED = "rt-seed-Ab12"
PRICE = 47.31
SX = "xbox-series-x-s"
H, DAY = timedelta(hours=1), timedelta(days=1)
TA_REQUESTS = {"collection", "list", "page", "gc-add", "gc-set"}


class FakeXbox:
    """Microsoft's token, Xbox Live, Collections, orders and catalog endpoints for fictional games.

    Every refresh issues a new refresh token (Microsoft rotates them); orders come 10 to a page."""

    def __init__(self):
        self.licences, self.orders, self.products = [], [], {}
        self.calls = []  # (method, url)
        self.journal = None  # a list to note every Microsoft request in, beside the TA requests
        self.fail = {}  # URL substring -> HTTP status answered instead
        self.reject = False  # every refresh token is rejected: sign-in needed
        self.issued = 0
        self.refreshed_with = []  # the refresh tokens presented, in order

    def own(self, pid, title, gen=("ConsoleGen9",)):
        """A paid Store purchase with an active Collections licence."""
        oid = f"ORD-{len(self.orders) + 1:04d}-Q"
        self.licences.append({"productId": pid, "productKind": "Game", "status": "Active", "orderId": oid,
                              "acquiredDate": "2025-03-01T00:00:00Z"})
        self.orders.append({"orderId": oid, "orderState": "Purchased", "orderPlacedDate": "2025-03-01T00:00:00Z",
                            "orderLineItems": [{"productId": pid, "title": title, "productType": "Game",
                                                "totalAmount": PRICE, "billingState": "Charged",
                                                "fulfillmentState": "Fulfilled"}]})
        self.products[pid] = {"ProductId": pid, "ProductType": "Game", "LocalizedProperties": [{"ProductTitle": title}],
                              "Properties": {"XboxConsoleGenOptimized": list(gen)},
                              "DisplaySkuAvailabilities": [{"Sku": {"Properties": {"Packages": [
                                  {"PlatformDependencies": [{"PlatformName": "Windows.Xbox"}]}]}}}]}

    def request(self, method, url, *, form=None, json_body=None, headers=None):
        self.calls.append((method, url))
        if self.journal is not None:
            self.journal.append(("microsoft", url))
        for part, status in self.fail.items():
            if part in url:
                return status, {}
        if url.endswith("/token"):
            self.refreshed_with.append(form["refresh_token"])
            if self.reject:
                return 400, {"error": "invalid_grant"}
            self.issued += 1
            return 200, {"access_token": f"access-tok-{self.issued}-Qx", "refresh_token": f"rt-rotated-{self.issued}-Zp"}
        if url == xbox.XBL_URL:
            return 200, {"Token": "user-tok-Wk3", "DisplayClaims": {"xui": [{"uhs": "uhs-77"}]}}
        if url == xbox.XSTS_URL:
            token = "xsts-live-tok-Hj" if json_body["RelyingParty"] == xbox.RP_LIVE else "xsts-store-tok-Pm"
            return 200, {"Token": token, "DisplayClaims": {"xui": [{"uhs": "uhs-77", "xid": XUID,
                                                                    "gtg": "Example Gamer"}]}}
        if url == xbox.COLLECTIONS_URL:
            return 200, {"items": list(self.licences)}
        if url.startswith(xbox.ORDERS_URL):
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            body = {"items": self.orders[(page - 1) * 10:page * 10]}
            if page * 10 < len(self.orders):
                body["@nextLink"] = f"{xbox.ORDERS_URL}?page={page + 1}"
            return 200, body
        if url.startswith(xbox.CATALOG_URL):
            ids = parse_qs(urlsplit(url).query)["bigIds"][0].split(",")
            return 200, {"Products": [self.products[i] for i in ids if i in self.products]}
        raise AssertionError(f"unexpected Microsoft request {method} {url}")


def library_only(tmp_path, *, dry=True, overrides=None, **cfg):
    """A `--only library` run (no feed, no wishlists, no Deku collection step) at NOW, 12:00 UTC: past the default
    11:30 start of the daily pass. The run's sleeps are noted in `ta.calls`, in order with the TA requests."""
    ta = FakeTA({}, {})
    h = Harness(tmp_path, records=None, deku=FakeDeku({}, {}), ta=ta, overrides=overrides, lists=(), library=True,
                dry_run=dry, library_dry_run=dry, xbox_refresh_token=SEED, **cfg)
    h.xbox = FakeXbox()
    h.deku.sleep = lambda seconds: ta.calls.append(("sleep", seconds))
    return h


def pid(n):
    return f"9ZZLIB{n:06d}"


def page(ta_id, title, platform=SERIES):
    return TAGame(ta_id, title, platform, "-".join(title.split()), (platform,))


def wrong(ta_id, title, ownership="No longer have", media=""):
    """A TA entry of an owned game with an ownership the step fixes."""
    return OwnedRow(ta_id, title, SX, ownership, media)


def written(h):
    """The TA games written, in order: every write ends with the entry's field sets."""
    return list(dict.fromkeys(w[1] for w in h.ta.writes if w[0] == "gc-set"))


def looked_up(calls):
    """The games looked up in these TA requests, by the first address tried (the platform variants come after)."""
    return [c[1] for c in calls if c[0] == "page" and not c[1].endswith(("-Xbox-One", "-Xbox-Series-XS"))]


def library_state(h):
    return load_state(h.cfg.state).library


def assert_paced(calls):
    """Every TA request the step makes comes 4 s after the one before it."""
    for i, c in enumerate(calls):
        if c[0] in TA_REQUESTS:
            assert i > 0 and calls[i - 1] == ("sleep", 4.0), calls[max(0, i - 2):i + 1]


def lines(caplog, prefix="library:"):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


# --------------------------------------------------------------------------- when it runs


def test_the_pass_runs_once_a_day_after_library_after(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path)
    h.xbox.own(pid(1), "Example Mending 01")
    h.ta.owned_rows = [OwnedRow(501, "Example Mending 01", SX, "Owned", "Digital")]
    h.now = NOW.replace(hour=11, minute=7)
    assert h.run() == 0
    assert h.xbox.calls == [] and h.ta.calls == []
    assert lines(caplog) == ["library: the daily pass starts after 11:30 UTC; not due yet"]
    h.now = NOW.replace(hour=12, minute=7)
    assert h.run() == 0
    assert h.xbox.calls and h.ta.collection_calls == 1
    assert library_state(h)["last_pass"] == "2026-11-01"
    microsoft = len(h.xbox.calls)
    caplog.clear()
    h.now = NOW.replace(hour=13, minute=7)
    assert h.run() == 0
    assert len(h.xbox.calls) == microsoft and h.ta.collection_calls == 1
    assert lines(caplog) == ["library: today's pass is complete; the next starts after 11:30 UTC tomorrow"]
    h.now = NOW.replace(hour=11, minute=7) + DAY
    assert h.run() == 0 and len(h.xbox.calls) == microsoft
    h.now = NOW.replace(hour=12, minute=7) + DAY
    assert h.run() == 0 and len(h.xbox.calls) > microsoft and h.ta.collection_calls == 2
    assert library_state(h)["last_pass"] == "2026-11-02"
    h.cfg.library_after = dtime(14, 0)
    h.now = NOW.replace(hour=13, minute=7) + 2 * DAY
    assert h.run() == 0 and h.ta.collection_calls == 2


# --------------------------------------------------------------------------- dry run and writes


def test_dry_run_writes_nothing_and_plans_everything(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=True)
    for n in range(1, 31):  # more than one run's write budget
        h.xbox.own(pid(n), f"Example Mending {n:02d}")
        h.ta.owned_rows.append(wrong(500 + n, f"Example Mending {n:02d}"))
    h.xbox.own(pid(31), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.xbox.own(pid(32), "Example Arena")
    h.ta.owned_rows.append(OwnedRow(902, "Example Arena: Champions Cup", SX, "Owned", "Digital"))  # a possible match
    assert h.run() == 0
    assert h.ta.writes == [] and not [c for c in h.ta.calls if c[0] in ("gc-add", "gc-set")]
    said = "\n".join(lines(caplog))
    for n in range(1, 31):
        assert f"library: would fix Example Mending {n:02d} (Owned, Digital)" in said
    assert "library: would add Example Finding (Owned, Digital)" in said
    assert "library: check by hand (possible-match): Example Arena: possible match in TA: Example Arena: Champions Cup" in said
    assert "already right 0; would add 1; would fix 30; check by hand 1" in said
    state = load_state(h.cfg.state)
    assert state.library["last_pass"] == "2026-11-01" and not state.library.get("writing")
    assert state.ta_collection == {}  # the Deku collection step's TA cache is never written here
    assert h.reporter.notes == []
    assert_paced(h.ta.calls)


def test_live_adds_and_fixes_within_the_budget(tmp_path):
    h = library_only(tmp_path, dry=False)
    for n in range(1, 27):
        h.xbox.own(pid(n), f"Example Mending {n:02d}")
        h.ta.owned_rows.append(wrong(500 + n, f"Example Mending {n:02d}"))
    for n in range(1, 5):
        h.xbox.own(pid(100 + n), f"Example Finding {n}")
        h.ta.pages[f"Example-Finding-{n}"] = page(900 + n, f"Example Finding {n}")
    assert h.run() == 0
    run_1 = written(h)
    assert len(run_1) == 25  # 30 actions, library_budget 25
    assert run_1[:4] == [901, 902, 903, 904] and [w for w in h.ta.writes if w[0] == "gc-add"] == [
        ("gc-add", f"Example-Finding-{n}") for n in range(1, 5)]
    assert h.ta.writes[:3] == [("gc-add", "Example-Finding-1"), ("gc-set", 901, "ownership", "Owned"),
                               ("gc-set", 901, "media", "Digital")]
    assert "last_pass" not in library_state(h) and not library_state(h).get("writing")
    assert_paced(h.ta.calls)
    h.ta.writes.clear()
    h.now = NOW + H
    assert h.run() == 0
    assert len(written(h)) == 5 and not [w for w in h.ta.writes if w[0] == "gc-add"]
    assert library_state(h)["last_pass"] == "2026-11-01"
    assert all((r.ownership, r.media) == ("Owned", "Digital") for r in h.ta.owned_rows)
    assert h.reporter.notes == []
    assert_paced(h.ta.calls)


def test_a_rotated_token_is_on_the_pvc_after_the_run(tmp_path):
    h = library_only(tmp_path)
    assert h.run() == 0
    path = h.cfg.xbox_token
    assert json.loads(path.read_text())["refresh_token"] == "rt-rotated-1-Zp"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and SEED not in path.read_text()
    h.now += DAY
    assert h.run() == 0
    assert h.xbox.refreshed_with == [SEED, "rt-rotated-1-Zp"]  # the seed once, then the token from the PVC
    assert json.loads(path.read_text())["refresh_token"] == "rt-rotated-2-Zp"


# --------------------------------------------------------------------------- failed reads


def test_a_failed_orders_page_writes_nothing_and_backs_off_6h(tmp_path):
    h = library_only(tmp_path, dry=False)
    for n in range(1, 13):  # two pages of orders
        h.xbox.own(pid(n), f"Example Mending {n:02d}")
        h.ta.owned_rows.append(wrong(500 + n, f"Example Mending {n:02d}"))
    h.xbox.fail["?page=2"] = 500
    assert h.run() == 1 and h.ta.writes == []
    assert h.ta.calls == []  # nothing read from TA either
    assert "Microsoft read failed" in h.last_issues()[0] and "HTTP 500" in h.last_issues()[0]
    pings, microsoft = len(h.reporter.notes), len(h.xbox.calls)
    h.now = NOW + 5 * H
    assert h.run() == 1  # the issue repeats ...
    assert len(h.xbox.calls) == microsoft and h.ta.calls == []  # ... without a new Microsoft or TA request
    assert len(h.reporter.notes) == pings  # ... or a new ping
    del h.xbox.fail["?page=2"]
    h.now = NOW + 6 * H
    assert h.run() == 0
    assert any("?page=2" in url for _, url in h.xbox.calls[microsoft:])  # the orders are read again
    assert len(written(h)) == 12
    assert "failed" not in library_state(h) and library_state(h)["last_pass"] == "2026-11-01"


def test_an_incomplete_ta_read_writes_nothing(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Mending 01")
    h.xbox.own(pid(2), "Example Finding 1")
    h.ta.pages["Example-Finding-1"] = page(901, "Example Finding 1")
    h.ta.owned_rows = [wrong(501, "Example Mending 01")]
    h.ta.fail_collection = IncompleteRead("read 90 of 100 collection titles")
    assert h.run() == 1 and h.ta.writes == []
    assert "TrueAchievements collection" in h.last_issues()[0]
    assert not [c for c in h.ta.calls if c[0] in ("page", "gc-add", "gc-set")]
    lib = library_state(h)
    assert "90 of 100" in lib["failed"]["issue"] and "last_pass" not in lib


def test_sign_in_needed_is_not_an_issue_and_stops_the_step_for_this_run(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path)
    h.xbox.reject = True
    assert h.run() == 0
    assert h.reporter.notes == [] and h.ta.calls == []
    assert any("Microsoft sign-in needed" in m for m in lines(caplog))
    h.now += H
    assert h.run() == 0 and h.xbox.refreshed_with == [SEED, SEED]  # tried again on the next run
    assert "last_pass" not in library_state(h) and "failed" not in library_state(h)


def test_a_failed_list_refresh_keeps_the_copy_in_state_and_waits_6h(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path)
    old = (NOW - 8 * DAY).isoformat()  # older than the weekly refresh
    save_state(h.cfg.state, State(library={"lists": {"games-with-gold": {"fetched": old, "ids": [7]},
                                                     "ea-play": {"fetched": old, "ids": [8]}}}))
    for n in range(1, 12):  # 11 lookups: the pass takes two runs
        h.xbox.own(pid(n), f"Example Seeking {n:02d}")
    h.ta.fail_list["games-with-gold"] = IncompleteRead("games-with-gold list page 1 has no rows (throttled?)")
    h.ta.list_ids["ea-play"] = [8, 9]
    assert h.run() == 0 and h.reporter.notes == []
    assert any("games-with-gold list refresh failed" in m for m in lines(caplog))
    lists = library_state(h)["lists"]
    assert lists["games-with-gold"]["ids"] == [7] and lists["games-with-gold"]["tried"] == NOW.isoformat()
    assert lists["ea-play"] == {"fetched": NOW.isoformat(), "ids": [8, 9]}
    h.ta.calls.clear()
    h.now = NOW + H  # the pass goes on: no new try of the list within 6 h of the failed one
    assert h.run() == 0 and not [c for c in h.ta.calls if c[0] == "list"]
    assert library_state(h)["last_pass"] == "2026-11-01"
    h.now = NOW + DAY
    assert h.run() == 0 and ("list", "games-with-gold") in h.ta.calls


def test_a_list_read_failure_without_a_copy_is_an_issue_with_a_back_off(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Mending 01")
    h.ta.owned_rows = [wrong(501, "Example Mending 01")]
    h.ta.fail_list["ea-play"] = TAError("GET /ea-play/games answered HTTP 500")
    assert h.run() == 1 and h.ta.writes == [] and h.ta.collection_calls == 0
    assert "ea-play list read failed" in h.last_issues()[0]
    h.ta.fail_list.clear()
    h.now = NOW + 5 * H
    assert h.run() == 1 and h.ta.collection_calls == 0
    h.now = NOW + 6 * H
    assert h.run() == 0 and written(h) == [501]


# --------------------------------------------------------------------------- page lookups


def test_page_lookups_are_budgeted_and_cached(tmp_path):
    h = library_only(tmp_path, dry=True)
    for n in range(1, 12):  # no TA page at any address
        h.xbox.own(pid(n), f"Example Seeking {n:02d}")
    h.xbox.own(pid(12), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    seeking = [f"Example-Seeking-{n:02d}" for n in range(1, 12)]
    assert h.run() == 0
    assert looked_up(h.ta.calls) == ["Example-Finding"] + seeking[:9]  # 10 games, in title order
    assert len([c for c in h.ta.calls if c[0] == "page"]) == 30  # each at its 3 addresses
    assert "last_pass" not in library_state(h)
    assert library_state(h)["pages"][pid(1)] == {
        "checked": NOW.isoformat(), "pages": [],
        "slugs": ["Example-Seeking-01", "Example-Seeking-01-Xbox-One", "Example-Seeking-01-Xbox-Series-XS"]}
    assert_paced(h.ta.calls)
    h.ta.calls.clear()
    h.now = NOW + H
    assert h.run() == 0
    assert looked_up(h.ta.calls) == seeking[9:]  # the other 2
    assert library_state(h)["last_pass"] == "2026-11-01"
    h.ta.calls.clear()
    h.now = NOW + DAY
    assert h.run() == 0 and looked_up(h.ta.calls) == []  # every result is cached
    h.now = NOW + 8 * DAY
    assert h.run() == 0 and looked_up(h.ta.calls) == ["Example-Finding"]  # a found page: 7 days
    h.ta.calls.clear()
    h.now = NOW + 31 * DAY
    assert h.run() == 0
    assert looked_up(h.ta.calls) == ["Example-Finding"] + seeking[:9]  # no page: 30 days


def test_a_seen_product_keeps_its_pages_for_30_days(tmp_path):
    h = library_only(tmp_path, dry=True)
    h.xbox.own(pid(1), "Example Finding")
    h.xbox.own(pid(2), "Example Keeping")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.ta.pages["Example-Keeping"] = page(902, "Example Keeping")
    assert h.run() == 0 and looked_up(h.ta.calls) == ["Example-Finding", "Example-Keeping"]
    st = load_state(h.cfg.state)
    st.library["seen"] = [pid(2)]
    save_state(h.cfg.state, st)
    h.ta.calls.clear()
    h.now = NOW + 8 * DAY
    assert h.run() == 0 and looked_up(h.ta.calls) == ["Example-Finding"]


def test_a_new_override_is_looked_up_although_the_game_has_a_cached_result(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=True)
    h.xbox.own(pid(1), "Example Finding")
    assert h.run() == 0 and looked_up(h.ta.calls) == ["Example-Finding"]  # no page: cached for 30 days
    h.cfg.overrides.write_text(json.dumps({"library": {pid(1): {"ta": "Example-Finding-Reforged"}}}))
    h.ta.pages["Example-Finding-Reforged"] = page(905, "Example Finding Reforged")
    h.ta.calls.clear()
    caplog.clear()
    h.now = NOW + DAY
    assert h.run() == 0
    assert [c[1] for c in h.ta.calls if c[0] == "page"] == ["Example-Finding-Reforged"]
    assert "library: would add Example Finding Reforged (Owned, Digital)" in lines(caplog)


def test_lookups_go_on_past_a_missing_page_and_stop_quietly_on_a_failure(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=True)
    h.xbox.own(pid(1), "Example Finding")
    h.xbox.own(pid(2), "Example Seeking")
    h.ta.pages["Example-Finding-Xbox-Series-XS"] = page(901, "Example Finding", SERIES)
    h.ta.fail_page["Example-Seeking-Xbox-One"] = TAError("GET /game/Example-Seeking-Xbox-One answered HTTP 503")
    assert h.run() == 0 and h.reporter.notes == []
    assert [c[1] for c in h.ta.calls if c[0] == "page"] == [
        "Example-Finding", "Example-Finding-Xbox-One", "Example-Finding-Xbox-Series-XS",  # two missing, one found
        "Example-Seeking", "Example-Seeking-Xbox-One"]  # stopped at the failure
    pages = library_state(h)["pages"]
    assert pages[pid(1)]["pages"][0]["id"] == 901 and pid(2) not in pages  # not looked up: next run
    assert "last_pass" not in library_state(h)
    assert any("lookups stopped" in m for m in lines(caplog))
    h.ta.fail_page.clear()
    h.ta.calls.clear()
    h.now = NOW + H
    assert h.run() == 0 and looked_up(h.ta.calls) == ["Example-Seeking"]
    assert library_state(h)["last_pass"] == "2026-11-01"


# --------------------------------------------------------------------------- write-ahead, write failures


def test_an_add_interrupted_before_its_field_sets_is_completed_next_run(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    on_disk = []
    h.ta.on_add = lambda slug: on_disk.append(library_state(h).get("writing"))
    h.ta.fail_set[(901, "ownership")] = Terminated()  # SIGTERM after the add, before its field sets
    assert h.run() == 1
    assert list(on_disk[0]) == ["901"]  # the write-ahead was saved before the add
    assert on_disk[0]["901"]["kind"] == "add" and on_disk[0]["901"]["slug"] == "Example-Finding"
    assert h.ta.writes == [("gc-add", "Example-Finding")]
    assert list(library_state(h)["writing"]) == ["901"]
    del h.ta.fail_set[(901, "ownership")]
    h.ta.calls.clear()
    h.xbox.journal = h.ta.calls
    h.now = NOW + H
    assert h.run() == 0
    first = [c for c in h.ta.calls if c[0] != "sleep"][:3]
    assert first == [("gc-add", "Example-Finding"), ("gc-set", 901, "ownership", "Owned"),
                     ("gc-set", 901, "media", "Digital")]  # completed first, before any read
    assert h.ta.writes == [("gc-add", "Example-Finding"), ("gc-set", 901, "ownership", "Owned"),
                           ("gc-set", 901, "media", "Digital")]  # the second add posted nothing
    assert library_state(h)["writing"] == {} and library_state(h)["last_pass"] == "2026-11-01"
    assert_paced(h.ta.calls)


def test_a_ta_write_error_is_an_issue_that_stops_the_writes(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Mending 01")
    h.xbox.own(pid(2), "Example Mending 02")
    h.ta.owned_rows = [wrong(501, "Example Mending 01"), wrong(502, "Example Mending 02")]
    h.ta.fail_set[(501, "media")] = TAError("collection media of 501: TA answered 'Disc', expected 'Digital'")
    assert h.run() == 1
    issue = h.last_issues()[0]
    assert "Example Mending 01" in issue and "expected 'Digital'" in issue
    assert written(h) == [501] and not [w for w in h.ta.writes if w[1] == 502]  # the second isn't started
    assert list(library_state(h)["writing"]) == ["501"] and "last_pass" not in library_state(h)
    del h.ta.fail_set[(501, "media")]
    h.ta.writes.clear()
    h.now = NOW + H
    assert h.run() == 0
    assert h.ta.writes[:2] == [("gc-set", 501, "ownership", "Owned"), ("gc-set", 501, "media", "Digital")]
    assert written(h) == [501, 502] and library_state(h)["writing"] == {}


def test_an_add_whose_game_ta_already_lists_changes_nothing(tmp_path):
    """The page says the game is in the collection although the collection read didn't list it (TA's own import
    just added it, or the read can't see it): the plan was made without its entry, so nothing is set on it."""
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding")
    h.xbox.own(pid(2), "Example Mending 01")
    h.ta.owned_rows = [wrong(501, "Example Mending 01")]
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.ta.unlisted = {901}
    assert h.run() == 1
    assert h.ta.writes == []  # no field set on it, and the writes stopped
    assert "already has Example Finding in the collection, though the collection read didn't list it" in (
        h.last_issues()[0])
    assert library_state(h)["writing"] == {}  # not a write to complete next run


def test_cloudflare_or_network_trouble_while_writing_stops_quietly(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.ta.fail_add["Example-Finding"] = Unreachable("network error: ConnectionError")
    assert h.run() == 0 and h.reporter.notes == [] and h.ta.writes == []
    assert list(library_state(h)["writing"]) == ["901"] and "last_pass" not in library_state(h)
    assert any("retried next run" in m for m in lines(caplog))


def test_a_skip_override_drops_an_unfinished_write(tmp_path):
    h = library_only(tmp_path, dry=False, overrides={"library": {pid(1): {"ta": "skip"}}})
    h.xbox.own(pid(1), "Example Finding")
    save_state(h.cfg.state, State(library={"writing": {"901": {
        "kind": "add", "slug": "Example-Finding", "ta_id": 901, "title": "Example Finding",
        "sets": [["ownership", "Owned"], ["media", "Digital"]], "pids": [pid(1)]}}}))
    assert h.run() == 0
    assert h.ta.writes == [] and library_state(h)["writing"] == {}
    assert library_state(h)["last_pass"] == "2026-11-01"


def test_nothing_starts_past_the_write_deadline(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=False, write_deadline=100.0)
    h.xbox.own(pid(1), "Example Mending 01")
    h.xbox.own(pid(2), "Example Finding")
    h.ta.owned_rows = [wrong(501, "Example Mending 01")]
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    read = h.ta.collection

    def slow_read():
        h.clock += 200
        return read()

    h.ta.collection = slow_read
    assert h.run() == 0
    assert h.ta.writes == [] and looked_up(h.ta.calls) == []
    assert "last_pass" not in library_state(h)
    assert any("write deadline" in m for m in lines(caplog))


# --------------------------------------------------------------------------- secrets


def test_tokens_xuid_prices_and_order_ids_never_reach_the_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    h = library_only(tmp_path, dry=True)
    h.xbox.own(pid(1), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.xbox.own(pid(2), "Example Arena")
    h.ta.owned_rows = [OwnedRow(902, "Example Arena: Champions Cup", SX, "Owned", "Digital")]
    assert h.run() == 0  # a whole dry-run pass
    # the next day's run meets the seed (configured), the PVC token, its rotation and the Xbox tokens and XUID
    met = [SEED, "rt-rotated-1-Zp", "rt-rotated-2-Zp", "access-tok-2-Qx", "user-tok-Wk3", "xsts-live-tok-Hj",
           "xsts-store-tok-Pm", XUID]
    h.ta.fail_collection = TAError("boom " + " ".join(met))  # a failure quoting every secret that run met
    h.now += DAY
    assert h.run() == 1
    blob = caplog.text + json.dumps(h.reporter.notes) + json.dumps(library_state(h)["failed"])
    for value in [*met, "access-tok-1-Qx", "ORD-0001-Q", "ORD-0002-Q", str(PRICE), pid(1), pid(2)]:
        assert value not in blob, value
    assert "<redacted>" in blob and "would add Example Finding" in caplog.text


# --------------------------------------------------------------------------- wiring


def test_only_library_runs_the_step_alone(tmp_path):
    h = library_only(tmp_path)
    assert not h.cfg.feed.exists()
    assert h.run() == 0
    assert h.made == ["ta"] and h.ta.wishlist_calls == 0
    assert h.reporter.reviews == [] and h.reporter.collection_reviews == []


def test_the_library_step_runs_between_the_wishlist_and_the_collection_steps(tmp_path):
    h = basic(tmp_path, dry_run=False, library=True, library_dry_run=False, collection=True,
              collection_dry_run=False, xbox_refresh_token=SEED)
    h.xbox = FakeXbox()
    h.xbox.own(pid(1), "Example Mending 01")
    h.ta.owned_rows = [OwnedRow(1, "Gamma Tale", SX, "Owned", "Digital"), wrong(501, "Example Mending 01")]
    h.deku.games["gamma-tale"] = ("Gamma Tale", {"xbox_series"})
    assert h.run() == 0
    order = [(j[0], j[1]) for j in h.journal]
    wishlist = max(i for i, j in enumerate(order) if j[1] in ("add", "update", "remove"))
    library = [i for i, j in enumerate(order) if j[1] == "gc-set"]
    assert library and wishlist < library[0] and library[-1] < order.index(("deku", "own"))
    assert h.made == ["deku", "ta"]  # one TA client for every step
    assert h.deku.owned["gamma-tale"] == [OwnedDetail("o-gamma-tale-0", "xbox_series", "digital")]


def test_invalid_library_overrides_fail_only_the_library_step(tmp_path):
    h = basic(tmp_path, dry_run=False, library=True, library_dry_run=False,
              overrides={"library": {pid(1): {"note": "no ta key"}}})
    h.xbox = FakeXbox()
    assert h.run() == 1
    assert [w[0] for w in h.deku.writes] == ["add", "update", "remove"]  # the wishlist was synced
    assert h.ta.writes == [("add", "101"), ("add", "102"), ("remove", "999")]
    assert h.xbox.calls == [] and "library overrides rejected" in h.last_issues()[0]


def test_an_unexpected_error_in_the_step_is_an_issue_and_the_state_is_still_saved(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.ta.fail_add["Example-Finding"] = RuntimeError("boom")
    assert h.run() == 1
    assert "library step aborted by an unexpected error: RuntimeError: boom" in h.last_issues()[0]
    lib = library_state(h)
    assert pid(1) in lib["pages"] and list(lib["writing"]) == ["901"]  # what the run learned is kept


def test_a_failed_write_ahead_save_writes_nothing(tmp_path, monkeypatch):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")

    def broken(self):
        raise OSError("disk full")

    monkeypatch.setattr("wsync.run._Run.checkpoint", broken)
    assert h.run() == 1
    assert h.ta.writes == [] and not [c for c in h.ta.calls if c[0] in ("gc-add", "gc-set")]
    assert "state not saved before writing Example Finding" in h.last_issues()[0]
    assert library_state(h)["writing"] == {}  # rolled back: nothing to complete next run


def test_unfinished_writes_wait_while_the_step_is_dry(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=True)
    entry = {"kind": "add", "slug": "Example-Finding", "ta_id": 901, "title": "Example Finding",
             "sets": [["ownership", "Owned"], ["media", "Digital"]], "pids": [pid(1)]}
    save_state(h.cfg.state, State(library={"writing": {"901": entry}}))
    assert h.run() == 0
    assert h.ta.writes == [] and library_state(h)["writing"] == {"901": entry}
    assert "library: 1 unfinished TA writes wait for a live run" in lines(caplog)


def test_the_start_line_names_the_library_steps_mode(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    start = lambda: next(r.getMessage() for r in caplog.records if r.getMessage().startswith("wsync starting"))
    h = library_only(tmp_path / "dry", dry=True)
    h.run()
    assert "lists: none; library step: DRY RUN; Deku collection step: off" in start()
    caplog.clear()
    h = library_only(tmp_path / "live", dry=False)
    h.run()
    assert "library step: APPLY" in start()
    caplog.clear()
    h = library_only(tmp_path / "forced", dry=True)
    h.cfg.library_dry_run = False  # a dry run is dry for the step too
    h.run()
    assert "library step: DRY RUN" in start()
    caplog.clear()
    (tmp_path / "off").mkdir()
    basic(tmp_path / "off").run()
    assert "library step: off" in start()

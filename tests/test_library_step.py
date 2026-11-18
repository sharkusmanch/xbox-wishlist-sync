"""The library step (Microsoft licences and orders -> TrueAchievements Game Collection), driven through `run()`
with fakes: a FakeXbox for Microsoft's endpoints and test_run's FakeTA. Every title, product ID, XUID and token
here is fictional."""

import hashlib
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
from wsync.ta import ONE, SERIES, IncompleteRead, TAGame, Unreachable
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

    def own(self, pid, title, gen=("ConsoleGen9",), price=PRICE):
        """A paid Store purchase with an active Collections licence."""
        oid = f"ORD-{len(self.orders) + 1:04d}-Q"
        self.licences.append({"productId": pid, "productKind": "Game", "status": "Active", "orderId": oid,
                              "acquiredDate": "2025-03-01T00:00:00Z"})
        self.orders.append({"orderId": oid, "orderState": "Purchased", "orderPlacedDate": "2025-03-01T00:00:00Z",
                            "orderLineItems": [{"productId": pid, "title": title, "productType": "Game",
                                                "totalAmount": price, "billingState": "Charged",
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
            return 200, {"access_token": f"access-tok-{self.issued}-Qx",
                         "refresh_token": f"rt-rotated-{self.issued}-Zp"}
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
    return written_in(h.ta.writes)


def written_in(writes):
    return list(dict.fromkeys(w[1] for w in writes if w[0] == "gc-set"))


def looked_up(calls):
    """The games looked up in these TA requests, by the first address tried (the platform variants come after)."""
    return [c[1] for c in calls if c[0] == "page" and not c[1].endswith(("-Xbox-One", "-Xbox-Series-XS"))]


def library_state(h):
    return load_state(h.cfg.state).library


def issue_notes(h):
    """The failure pings (issues); the step's own pings are asserted in the reporting tests."""
    return [n for n in h.reporter.notes if n[0] == "Wishlist sync failed"]


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
    assert ("library: check by hand (possible-match): Example Arena: possible match in TA: "
            "Example Arena: Champions Cup") in said
    assert "already right 0; would add 1; would fix 30; check by hand 1" in said
    state = load_state(h.cfg.state)
    assert state.library["last_pass"] == "2026-11-01" and not state.library.get("writing")
    assert state.ta_collection == {}  # the Deku collection step's TA cache is never written here
    assert not issue_notes(h)
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
    # a new entry is already Owned on TA: only its media is posted
    assert h.ta.writes[:2] == [("gc-add", "Example-Finding-1"), ("gc-set", 901, "media", "Digital")]
    assert ("gc-set", 901, "ownership", "Owned") in h.ta.calls  # asked, compared, not posted
    assert "last_pass" not in library_state(h) and not library_state(h).get("writing")
    assert_paced(h.ta.calls)
    h.ta.writes.clear()
    h.now = NOW + H
    assert h.run() == 0
    assert len(written(h)) == 5 and not [w for w in h.ta.writes if w[0] == "gc-add"]
    assert library_state(h)["last_pass"] == "2026-11-01"
    assert all((r.ownership, r.media) == ("Owned", "Digital") for r in h.ta.owned_rows)
    assert not issue_notes(h)
    assert_paced(h.ta.calls)


def test_a_pass_spanning_three_runs_reads_the_ta_collection_once(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding")  # an add, first in title order: written by run 1
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    for n in range(1, 55):  # 55 writes, 25 a run: three runs
        h.xbox.own(pid(100 + n), f"Example Mending {n:02d}")
        h.ta.owned_rows.append(wrong(500 + n, f"Example Mending {n:02d}"))
    assert h.run() == 0
    assert written(h)[0] == 901 and len(written(h)) == 25
    assert library_state(h)["pass"]["day"] == "2026-11-01"
    run_1 = list(h.ta.writes)
    h.ta.calls.clear()
    h.now = NOW + H
    assert h.run() == 0
    # the stored rows still show Example Finding missing: not added, nor any game of run 1 written, again
    assert not [c for c in h.ta.calls if c[0] == "gc-add" or (c[0] == "gc-set" and c[1] in written_in(run_1))]
    assert len(written(h)) == 50
    h.now = NOW + 2 * H
    assert h.run() == 0
    assert len(written(h)) == 55 and len(set(written(h))) == 55
    assert h.ta.collection_calls == 1  # one TA collection read for the whole pass
    lib = library_state(h)
    assert lib["last_pass"] == "2026-11-01" and "pass" not in lib  # dropped once the pass is complete


def test_a_new_days_pass_reads_the_ta_collection_again(tmp_path):
    h = library_only(tmp_path, dry=True)
    for n in range(1, 12):  # 11 lookups: the pass doesn't finish on its last run of the day
        h.xbox.own(pid(n), f"Example Seeking {n:02d}")
    h.now = NOW.replace(hour=23, minute=7)
    assert h.run() == 0 and h.ta.collection_calls == 1
    assert library_state(h)["pass"]["day"] == "2026-11-01" and "last_pass" not in library_state(h)
    h.now = NOW.replace(hour=12, minute=7) + DAY
    assert h.run() == 0 and h.ta.collection_calls == 2  # yesterday's read is not today's
    assert library_state(h)["last_pass"] == "2026-11-02" and "pass" not in library_state(h)


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


def test_a_failed_sign_in_is_no_issue_pings_once_and_waits_for_the_next_days_pass(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path)
    h.xbox.reject = True
    h.now = NOW.replace(hour=12, minute=7)
    assert h.run() == 0
    assert [t for t, _ in h.reporter.notes] == ["Library sync: Microsoft sign-in needed"] and h.ta.calls == []
    assert any("Microsoft sign-in needed" in m for m in lines(caplog))
    lib = library_state(h)
    assert lib["signin_failed"] == {"day": "2026-11-01", "seed": hashlib.sha256(SEED.encode()).hexdigest()}
    assert SEED not in json.dumps(lib)  # the fingerprint, never the value
    assert "last_pass" not in lib and "failed" not in lib
    microsoft = len(h.xbox.calls)
    caplog.clear()
    h.now = NOW.replace(hour=13, minute=7)
    assert h.run() == 0 and len(h.xbox.calls) == microsoft  # not retried the same day
    assert len(lines(caplog)) == 1 and "XBOX_REFRESH_TOKEN" in lines(caplog)[0]
    h.xbox.reject = False
    h.now = NOW.replace(hour=12, minute=7) + DAY
    assert h.run() == 0 and h.xbox.refreshed_with == [SEED, SEED]  # retried at the next day's pass
    lib = library_state(h)
    assert "signin_failed" not in lib and lib["last_pass"] == "2026-11-02"  # a working sign-in clears it


def test_a_failed_sign_in_is_retried_at_the_next_run_once_the_seed_changes(tmp_path):
    h = library_only(tmp_path)
    h.xbox.reject = True
    h.now = NOW.replace(hour=12, minute=7)
    assert h.run() == 0 and h.xbox.refreshed_with == [SEED]
    h.cfg.xbox_refresh_token = "rt-reseeded-Mn5"  # a new seed in OpenBao
    h.xbox.reject = False
    h.now = NOW.replace(hour=13, minute=7)
    assert h.run() == 0 and h.xbox.refreshed_with == [SEED, "rt-reseeded-Mn5"]
    lib = library_state(h)
    assert "signin_failed" not in lib and lib["last_pass"] == "2026-11-01"


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
    assert h.run() == 0 and not issue_notes(h)
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
    assert h.run() == 0 and not issue_notes(h)
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
    on_disk = {}
    h.ta.on_add = lambda slug: on_disk.update(before_add=library_state(h).get("writing"))

    def sigterm(game_id, field_name):  # SIGTERM after the add, before its field sets
        on_disk["after_add"] = library_state(h)["writing"]["901"]
        raise Terminated()

    h.ta.on_set = sigterm
    assert h.run() == 1
    ahead = on_disk["before_add"]["901"]  # the write-ahead was saved before the add ...
    assert ahead["kind"] == "add" and ahead["slug"] == "Example-Finding" and not ahead.get("added")
    assert ahead["expect"] == {"ownership": ["Owned"], "media": [""]}  # TA's defaults for a new entry
    assert on_disk["after_add"]["added"] is True  # ... and its progress after it
    assert h.ta.writes == [("gc-add", "Example-Finding")]
    h.ta.on_set = None
    h.ta.calls.clear()
    h.xbox.journal = h.ta.calls
    h.now = NOW + H
    assert h.run() == 0
    first_write = next(i for i, c in enumerate(h.ta.calls) if c[0] == "gc-set")
    assert ("collection",) not in h.ta.calls  # the pass's stored read
    assert any(c[0] == "microsoft" for c in h.ta.calls[:first_write])  # completed in the write phase, after the reads
    assert not [c for c in h.ta.calls if c[0] == "gc-add"]  # already added: not asked again, nor planned again
    assert h.ta.writes == [("gc-add", "Example-Finding"), ("gc-set", 901, "media", "Digital")]
    assert library_state(h)["writing"] == {} and library_state(h)["last_pass"] == "2026-11-01"
    assert_paced(h.ta.calls)


def test_a_write_that_keeps_failing_never_holds_up_the_step(tmp_path):
    """The review's probe: a permanent TA error on 501's write."""
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Mending 01")
    h.xbox.own(pid(2), "Example Mending 02")
    h.ta.owned_rows = [wrong(501, "Example Mending 01"), wrong(502, "Example Mending 02")]
    h.ta.fail_set[(501, "ownership")] = TAError("collection ownership of 501: ownership='Owned' matches no option")
    assert h.run() == 1
    issue = h.last_issues()[0]
    assert "Example Mending 01" in issue and "matches no option" in issue
    assert f'library override "skip" for product {pid(1)}' in issue  # the product id to put in a skip override
    assert written(h) == [502]  # the run's other writes go on
    assert library_state(h)["writing"] == {}  # nothing reached TA: not a leftover
    microsoft = len(h.xbox.calls)
    h.ta.calls.clear()
    h.now = NOW + H
    assert h.run() == 0  # the pass goes on: read, planned, 501 not tried again today
    assert len(h.xbox.calls) > microsoft and not [c for c in h.ta.calls if c[0] == "gc-set"]
    assert library_state(h)["last_pass"] == "2026-11-01"
    h.xbox.own(pid(3), "Example Mending 03")  # a new purchase
    h.ta.owned_rows.append(wrong(503, "Example Mending 03"))
    h.ta.calls.clear()
    h.now = NOW + DAY  # the next day's pass: read, planned and written; 501 is tried again (an issue again)
    assert h.run() == 1
    assert ("gc-set", 501, "ownership", "Owned") in h.ta.calls and written(h) == [502, 503]
    assert library_state(h)["writing"] == {}


def test_a_partly_written_entry_is_tried_once_in_each_later_pass(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.ta.fail_set[(901, "media")] = TAError("collection media of 901: TA answered 'Disc', expected 'Digital'")
    assert h.run() == 1  # added, then the media set failed: partly written
    entry = library_state(h)["writing"]["901"]
    assert entry["added"] is True and entry["sets_done"] == ["ownership"] and entry["tried"] == "2026-11-01"
    assert pid(1) in h.last_issues()[0]
    h.ta.calls.clear()
    h.now = NOW + H
    assert h.run() == 0 and not [c for c in h.ta.calls if c[0] in ("gc-add", "gc-set")]  # not tried every run
    h.now = NOW + DAY  # the next pass tries it once: still failing, still kept
    assert h.run() == 1
    assert [c[1:] for c in h.ta.calls if c[0] == "gc-set"] == [(901, "media", "Digital")]
    assert library_state(h)["writing"]["901"]["tried"] == "2026-11-02"
    h.ta.calls.clear()
    h.now = NOW + DAY + H
    assert h.run() == 0 and not [c for c in h.ta.calls if c[0] == "gc-set"]
    del h.ta.fail_set[(901, "media")]
    h.now = NOW + 2 * DAY
    assert h.run() == 0 and library_state(h)["writing"] == {}
    assert h.ta.writes == [("gc-add", "Example-Finding"), ("gc-set", 901, "media", "Digital")]


def test_after_three_write_errors_in_a_row_the_passs_writes_wait_for_the_next_day(tmp_path, caplog):
    """The review's probe: a TA-wide write failure (every set refused) pings once, not on every run."""
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=False)
    for n in range(1, 11):
        h.xbox.own(pid(n), f"Example Mending {n:02d}")
        h.ta.owned_rows.append(wrong(500 + n, f"Example Mending {n:02d}"))
        h.ta.fail_set[(500 + n, "ownership")] = TAError(
            f"collection ownership of {500 + n}: TA offers no ownership dropdown (is the game in the collection?)")
    assert h.run() == 1
    assert len(h.last_issues()) == 3 and written(h) == []
    assert len([c for c in h.ta.calls if c[0] == "gc-set"]) == 3  # the 4th isn't started
    pings = len(h.reporter.notes)
    for hour in (13, 14, 15):
        h.ta.calls.clear()
        caplog.clear()
        h.now = NOW.replace(hour=hour)
        assert h.run() == 0  # the pass goes on (reads, plan), its writes don't: no new issue
        assert not [c for c in h.ta.calls if c[0] in ("gc-add", "gc-set")]
        assert any("writes stopped for today's pass" in m for m in lines(caplog))
    assert len(h.reporter.notes) == pings
    h.ta.fail_set.clear()  # TA works again
    h.now = NOW + DAY  # the next day's pass tries again
    assert h.run() == 0 and written(h) == list(range(501, 511))


def test_a_successful_write_resets_the_error_streak(tmp_path):
    h = library_only(tmp_path, dry=False)
    for n in range(1, 6):
        h.xbox.own(pid(n), f"Example Mending {n:02d}")
        h.ta.owned_rows.append(wrong(500 + n, f"Example Mending {n:02d}"))
    for n in (1, 3, 4):  # three errors, never three in a row
        h.ta.fail_set[(500 + n, "ownership")] = TAError(f"collection ownership of {500 + n}: HTTP 500")
    assert h.run() == 1 and len(h.last_issues()) == 3
    assert written(h) == [502, 505]  # the writes went on to the last one
    assert library_state(h)["pass"]["errors"] == 0


def test_a_partly_written_fix_is_kept_and_its_rest_tried_in_the_next_pass(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Mending 01")
    h.ta.owned_rows = [wrong(501, "Example Mending 01")]  # a fix: ownership Owned, then media Digital
    h.ta.fail_set[(501, "media")] = TAError("collection media of 501: TA answered 'Disc', expected 'Digital'")
    assert h.run() == 1
    assert "partly written" in h.last_issues()[0]
    entry = library_state(h)["writing"]["501"]
    assert entry["kind"] == "fix" and entry["sets_done"] == ["ownership"] and entry["tried"] == "2026-11-01"
    del h.ta.fail_set[(501, "media")]
    h.now = NOW + DAY  # the fresh read shows 501 Owned now: only the kept write sets its media
    assert h.run() == 0
    assert h.ta.writes == [("gc-set", 501, "ownership", "Owned"), ("gc-set", 501, "media", "Digital")]
    assert library_state(h)["writing"] == {}


def test_the_progress_of_each_field_set_is_on_disk_before_the_next_request(tmp_path):
    """What a crash right after the first set would leave behind."""
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Mending 01")
    h.ta.owned_rows = [wrong(501, "Example Mending 01")]
    on_disk = []

    def before_set(game_id, field_name):
        on_disk.append((field_name, library_state(h)["writing"]["501"].get("sets_done")))

    h.ta.on_set = before_set
    assert h.run() == 0
    assert on_disk == [("ownership", None), ("media", ["ownership"])]


def test_any_exception_from_a_write_is_that_games_write_error(tmp_path, caplog):
    """The review's probe: a TypeError from collection_set (EditOptions' answer was a dict)."""
    caplog.set_level(logging.DEBUG)
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Mending 01")
    h.xbox.own(pid(2), "Example Mending 02")
    h.ta.owned_rows = [wrong(501, "Example Mending 01"), wrong(502, "Example Mending 02")]
    h.ta.fail_set[(501, "ownership")] = TypeError("string indices must be integers, not 'str' (47.31)")
    assert h.run() == 1
    issue = h.last_issues()[0]
    assert "Example Mending 01" in issue and "TypeError" in issue and pid(1) in issue
    assert "string indices" not in caplog.text + issue  # the exception type only, never its message
    assert written(h) == [502] and library_state(h)["writing"] == {}
    h.ta.calls.clear()
    h.now = NOW + H
    assert h.run() == 0 and not [c for c in h.ta.calls if c[0] == "gc-set"]  # not retried this pass
    h.now = NOW + DAY
    assert h.run() == 1 and ("gc-set", 501, "ownership", "Owned") in h.ta.calls  # the next pass tries again


def test_a_leftover_whose_override_now_names_another_page(tmp_path):
    """Dropped when nothing of it reached TA; completed when part of it did."""
    entry = {"kind": "add", "slug": "Example-Finding", "ta_id": 901, "title": "Example Finding",
             "sets": [["ownership", "Owned"], ["media", "Digital"]], "expect": {"ownership": ["Owned"], "media": [""]},
             "pids": [pid(1)]}
    for reached in (False, True):
        (tmp_path / str(reached)).mkdir()
        h = library_only(tmp_path / str(reached), dry=False,
                         overrides={"library": {pid(1): {"ta": "Example-Finding-Reforged"}}})
        h.xbox.own(pid(1), "Example Finding")
        h.ta.pages["Example-Finding"] = page(901, "Example Finding")
        h.ta.pages["Example-Finding-Reforged"] = page(905, "Example Finding Reforged")
        if reached:
            h.ta.owned_rows = [OwnedRow(901, "Example Finding", SX, "Owned", "")]
        save_state(h.cfg.state, State(library={"writing": {"901": {**entry, "added": reached}}}))
        assert h.run() == 0
        assert library_state(h)["writing"] == {}
        done_901 = [w for w in h.ta.writes if 901 in w or "Example-Finding" in w]
        assert done_901 == ([("gc-set", 901, "media", "Digital")] if reached else [])
        assert ("gc-add", "Example-Finding-Reforged") in h.ta.writes  # the override's page, from the plan


def test_a_field_changed_by_hand_since_the_read_is_left_as_it_is(tmp_path, caplog):
    """The review's probe: 502 set to Owned / Disc by hand between two runs of a pass that reuses its read."""
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=False, library_budget=1)
    h.xbox.own(pid(1), "Example Mending 01")
    h.xbox.own(pid(2), "Example Mending 02")
    h.ta.owned_rows = [wrong(501, "Example Mending 01"), wrong(502, "Example Mending 02")]
    assert h.run() == 0 and written(h) == [501]
    h.ta.owned_rows[1] = OwnedRow(502, "Example Mending 02", SX, "Owned", "Disc")  # by hand on TA
    h.cfg.library_budget = 25
    h.now = NOW + H
    assert h.run() == 0 and not issue_notes(h)
    assert h.reporter.notes == [("Library sync", "+1 TA library\n- Example Mending 01")]  # never 502's title
    assert not [w for w in h.ta.writes if w[1] == 502]  # nothing posted for 502
    assert h.ta.owned_rows[1].media == "Disc"
    assert any("Example Mending 02 changed on TrueAchievements since the read; left as is" in m
               for m in lines(caplog))
    assert library_state(h)["writing"] == {} and library_state(h)["last_pass"] == "2026-11-01"


def test_an_add_whose_game_ta_already_lists_changes_nothing(tmp_path, caplog):
    """The page says the game is in the collection although the collection read didn't list it (TA's own import
    just added it, or the read can't see it): the plan was made without its entry, so nothing is set on it."""
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding")
    h.xbox.own(pid(2), "Example Mending 01")
    h.ta.owned_rows = [wrong(501, "Example Mending 01")]
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.ta.unlisted = {901}
    caplog.set_level(logging.INFO)
    assert h.run() == 0 and not issue_notes(h)  # a warning, never an issue
    assert h.reporter.notes == [("Library sync", "+1 TA library\n- Example Mending 01")]  # not the listed game
    assert any("already lists Example Finding" in m for m in lines(caplog))
    assert not [w for w in h.ta.writes if 901 in w or "Example-Finding" in w]  # no field set on it
    assert written(h) == [501]  # the other write goes on
    assert library_state(h)["writing"] == {}  # not a write to complete next run
    assert library_state(h)["last_pass"] == "2026-11-01"  # the next day's pass plans it from a fresh read


def test_an_add_ta_listed_since_the_pass_read_the_collection_is_left_alone_quietly(tmp_path, caplog):
    """TA's own import adds a game after the pass's one collection read: the stored rows still show it missing."""
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=False, library_budget=1)
    h.xbox.own(pid(1), "Example Finding 1")
    h.xbox.own(pid(2), "Example Finding 2")
    h.xbox.own(pid(3), "Example Finding 3")
    for n in (1, 2, 3):
        h.ta.pages[f"Example-Finding-{n}"] = page(900 + n, f"Example Finding {n}")
    assert h.run() == 0 and written(h) == [901]
    h.ta.owned_rows.append(OwnedRow(902, "Example Finding 2", SX, "Xbox Game Pass Ultimate", ""))  # TA's import
    h.cfg.library_budget = 25
    h.now = NOW + H
    assert h.run() == 0 and not issue_notes(h)
    assert h.reporter.notes == [("Library sync", "+1 TA library\n- Example Finding 1"),
                                ("Library sync", "+1 TA library\n- Example Finding 3")]
    assert written(h) == [901, 903]  # Example Finding 2 is left as TA has it; the next write goes on
    assert any("already lists Example Finding 2" in m for m in lines(caplog))
    assert library_state(h)["last_pass"] == "2026-11-01"


def test_a_malformed_price_is_a_read_failure_that_never_quotes_it(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding", price="47,31 EUR")
    assert h.run() == 1
    issue = h.last_issues()[0]
    assert "Microsoft" in issue and "ValueError" in issue
    assert "47,31" not in caplog.text + json.dumps(h.reporter.notes) + json.dumps(library_state(h))
    assert library_state(h)["failed"]["at"] == NOW.isoformat()  # the 6 h back-off


def test_page_not_found_is_no_page_and_any_other_odd_page_caches_nothing(tmp_path):
    """Through the real client: TA's page-not-found page (HTTP 200) is cached as no page; a page that is neither a
    game nor that (maintenance, throttling) stops the lookups quietly, nothing cached."""
    from test_ta import NOT_FOUND, logged_in, resp

    h = library_only(tmp_path, dry=True)
    h.xbox.own(pid(1), "Example Finding")
    h.xbox.own(pid(2), "Example Seeking")
    client, _ = logged_in({("GET", "/game/Example-Finding"): resp(NOT_FOUND),
                           ("GET", "/game/Example-Seeking"): resp("<html><title>Down for maintenance</title></html>")})
    h.ta.game_page_or_none = client.game_page_or_none
    assert h.run() == 0 and not issue_notes(h)
    pages = library_state(h)["pages"]
    assert pages[pid(1)]["pages"] == [] and pid(2) not in pages
    assert "last_pass" not in library_state(h)


def test_cloudflare_or_network_trouble_while_writing_stops_quietly(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.ta.fail_add["Example-Finding"] = Unreachable("network error: ConnectionError")
    assert h.run() == 0 and not issue_notes(h) and h.ta.writes == []
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
    h.ta.list_ids = {"games-with-gold": [7]}
    h.ta.fail_collection = RuntimeError("boom")  # not a TA error: a bug, not a failed read
    assert h.run() == 1
    assert "library step aborted by an unexpected error: RuntimeError: boom" in h.last_issues()[0]
    assert library_state(h)["lists"]["games-with-gold"]["ids"] == [7]  # what the run learned is kept


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


# --------------------------------------------------------------------------- reporting (Task 7)


def tasks(h, key):
    return [b for k, _, b in h.reporter.library_tasks if k == key]


def review_pids(n):
    """n owned games that each need a hand check (a possible match in TA), titles Example Arena 1..n."""
    return [f"Example Arena {i}" for i in range(1, n + 1)]


def own_checks(h, titles, start=1):
    for i, t in enumerate(titles, start):
        h.xbox.own(pid(start * 0 + i), t)
        h.ta.owned_rows.append(OwnedRow(700 + i, f"{t}: Champions Cup", SX, "Owned", "Digital"))


def test_dry_run_review_lists_adds_fixes_and_checks(tmp_path):
    h = library_only(tmp_path, dry=True)
    h.xbox.own(pid(1), "Example Mending")
    h.ta.owned_rows.append(wrong(501, "Example Mending", "No longer have", "Disc"))
    h.xbox.own(pid(2), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    h.xbox.own(pid(3), "Example Lost Game")  # no TA page: still listed in the dry run
    own_checks(h, ["Example Arena"], start=4)
    assert h.run() == 0
    body = tasks(h, "library-review:dry-run")[-1]
    assert "## Would add (1)" in body and "- Example Finding (`Example-Finding`, Owned, Digital)" in body
    assert "## Would fix (1)" in body and "- Example Mending: No longer have, Disc -> Owned" in body
    assert f"(`{pid(4)}`, possible-match)" in body and f"Example Lost Game (`{pid(3)}`, no-page)" in body
    assert "already right 0" in body
    assert [t for t, _ in h.reporter.notes] == ["Library sync plan ready"]
    assert tasks(h, "library-check") == [None]  # the live task stays closed in a dry run
    assert library_state(h).get("seen") is None
    h.now = NOW + DAY  # a new day's pass over the same plan
    h.run()
    assert len(tasks(h, "library-review:dry-run")) == 2 and library_state(h)["last_pass"] == "2026-11-02"
    assert [t for t, _ in h.reporter.notes] == ["Library sync plan ready"]  # unchanged lists: no second ping


def test_the_first_live_pass_marks_every_review_item_seen(tmp_path):
    h = library_only(tmp_path, dry=False, library_budget=1, library_lookups=1)
    own_checks(h, review_pids(3))
    for n in range(1, 4):  # writes beyond the budget of 1
        h.xbox.own(pid(50 + n), f"Example Mending {n}")
        h.ta.owned_rows.append(wrong(500 + n, f"Example Mending {n}"))
    h.xbox.own(pid(90), "Example Waiting")  # never looked up this run (lookup budget 1 is spent first)
    h.xbox.own(pid(91), "Example Waiting Too")
    assert h.run() == 0
    lib = library_state(h)
    assert {pid(1), pid(2), pid(3)} <= set(lib["seen"]) and lib["seen"] == sorted(lib["seen"])
    assert {pid(90), pid(91)} <= set(lib["seen"])  # games still waiting for a lookup are baselined too
    assert tasks(h, "library-check") == [None]  # all three are seen: nothing to check
    assert tasks(h, "library-review:dry-run") == [None]
    before = lib["seen"]
    h.xbox.own(pid(95), "Example Later")
    h.now = NOW + H
    h.run()
    assert library_state(h)["seen"] == before  # later runs never change it


def test_only_unseen_items_reach_the_check_task_and_no_page_games_never_do(tmp_path):
    h = library_only(tmp_path, dry=False)
    own_checks(h, ["Example Arena"])
    assert h.run() == 0
    assert tasks(h, "library-check") == [None] and h.reporter.notes == []
    h.xbox.own(pid(20), "Example Newcomer")  # a new purchase with a possible match
    h.ta.owned_rows.append(OwnedRow(720, "Example Newcomer: Deluxe", SX, "Owned", "Digital"))
    h.xbox.own(pid(21), "Example Nopage")  # a new purchase with no TA page
    h.now = NOW + DAY
    assert h.run() == 0
    body = tasks(h, "library-check")[-1]
    assert f"Example Newcomer (`{pid(20)}`)" in body and "Example Nopage" not in body and "Example Arena" not in body
    assert [t for t, _ in h.reporter.notes] == ["Library sync: 1 to check by hand"]
    h.now = NOW + 2 * DAY
    assert h.run() == 0
    assert [t for t, _ in h.reporter.notes] == ["Library sync: 1 to check by hand"]  # same set: no ping


def test_the_check_task_closes_when_empty(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Existing")
    assert h.run() == 0
    h.xbox.own(pid(20), "Example Newcomer")
    h.ta.owned_rows.append(OwnedRow(720, "Example Newcomer: Deluxe", SX, "Owned", "Digital"))
    h.now = NOW + DAY
    h.run()
    assert tasks(h, "library-check")[-1] is not None
    h.ta.owned_rows[-1] = OwnedRow(720, "Example Newcomer", SX, "Owned", "Digital")  # now it matches: settled
    h.now = NOW + 2 * DAY
    h.run()
    assert tasks(h, "library-check")[-1] is None and "check_sig" not in library_state(h)


def test_sign_in_needed_pings_once_opens_a_task_and_exits_0(tmp_path):
    h = library_only(tmp_path)
    h.xbox.reject = True
    h.now = NOW.replace(hour=12, minute=7)
    assert h.run() == 0
    body = tasks(h, "library-signin")[-1]
    assert "scripts/xbox-library.py --token-file <tmp> login" in body and "microsoft.com/link" in body
    assert "secret/apps/tools/xbox-wishlist-sync" in body and "XBOX_REFRESH_TOKEN" in body
    assert SEED not in body and h.reporter.problems == []
    h.now = NOW.replace(hour=13, minute=7)
    assert h.run() == 0
    h.now = NOW.replace(hour=12, minute=7) + DAY  # the next day's pass fails again: same episode
    assert h.run() == 0
    assert [t for t, _ in h.reporter.notes] == ["Library sync: Microsoft sign-in needed"]
    assert len(tasks(h, "library-signin")) == 3 and None not in tasks(h, "library-signin")


def test_a_working_sign_in_closes_the_sign_in_task(tmp_path):
    h = library_only(tmp_path)
    h.xbox.reject = True
    h.now = NOW.replace(hour=12, minute=7)
    h.run()
    h.xbox.reject = False
    h.now = NOW.replace(hour=12, minute=7) + DAY
    assert h.run() == 0
    assert tasks(h, "library-signin")[-1] is None and "signin_failed" not in library_state(h)
    h.xbox.reject = True  # a later failure is a new episode: pinged again
    h.xbox.refreshed_with.clear()
    h.now = NOW.replace(hour=12, minute=7) + 2 * DAY
    h.run()
    assert [t for t, _ in h.reporter.notes] == ["Library sync: Microsoft sign-in needed"] * 2


def test_the_live_notification_names_what_was_written_and_nothing_changed_is_silent(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(2), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")
    assert h.run() == 0
    assert h.reporter.notes == [("Library sync", "+1 TA library\n- Example Finding")]
    h.now = NOW + DAY
    h.run()
    assert len(h.reporter.notes) == 1  # a run that changed nothing: no ping


def test_a_failing_reporter_is_an_issue_and_never_breaks_the_step(tmp_path):
    h = library_only(tmp_path, dry=True)
    h.xbox.own(pid(2), "Example Finding")
    h.ta.pages["Example-Finding"] = page(901, "Example Finding")

    def boom(*a):
        raise RuntimeError("vikunja down")
    h.reporter.library_task = boom
    assert h.run() == 1  # an issue of its own, nothing else broken
    assert library_state(h)["last_pass"] == "2026-11-01"
    assert [t for t, _ in h.reporter.notes] == ["Wishlist sync failed"]
    assert "Vikunja library task" in h.reporter.notes[0][1] and "RuntimeError" in h.reporter.notes[0][1]


def test_a_failed_close_of_the_sign_in_task_is_retried_until_it_works(tmp_path):
    h = library_only(tmp_path)
    h.xbox.reject = True
    h.now = NOW.replace(hour=12, minute=7)
    h.run()
    assert library_state(h)["signin_task"] is True
    h.xbox.reject = False
    real, calls = h.reporter.library_task, []

    def down(key, title, body):
        calls.append(body)
        raise RuntimeError("vikunja down")
    h.reporter.library_task = down
    h.now = NOW.replace(hour=12, minute=7) + DAY
    assert h.run() == 1  # an issue for the failed close, like other reporting failures
    assert calls[:1] == [None] and library_state(h)["signin_task"] is True
    assert "signin_failed" not in library_state(h)
    h.reporter.library_task = real
    h.now = NOW.replace(hour=12, minute=7) + 2 * DAY
    h.run()
    assert tasks(h, "library-signin")[-1] is None and "signin_task" not in library_state(h)


def test_a_working_sign_in_without_the_flag_makes_no_close_call(tmp_path):
    h = library_only(tmp_path)
    assert h.run() == 0
    assert tasks(h, "library-signin") == []


def test_a_dry_pass_over_several_runs_pings_once_when_the_plan_is_complete(tmp_path):
    h = library_only(tmp_path, dry=True, library_lookups=10)
    for n in range(1, 26):  # 25 found pages, 10 lookups a run: three runs
        h.xbox.own(pid(n), f"Example Finding {n:02d}")
        h.ta.pages[f"Example-Finding-{n:02d}"] = page(900 + n, f"Example Finding {n:02d}")
    pings = []
    for i in range(3):
        h.now = NOW + i * H
        assert h.run() == 0
        pings.append(len(h.reporter.notes))
    assert pings == [0, 0, 1] and h.reporter.notes[0][0] == "Library sync plan ready"
    assert "would add 25" in h.reporter.notes[0][1]
    assert "waiting for a page lookup 5" in tasks(h, "library-review:dry-run")[1]  # the task text still updates
    assert "Example Finding 01" in tasks(h, "library-review:dry-run")[1]  # earlier runs' items stay listed


def test_a_weekly_relookup_cycle_does_not_ping_an_unchanged_plan(tmp_path):
    h = library_only(tmp_path, dry=True, library_lookups=1)
    for n in range(1, 4):
        h.xbox.own(pid(n), f"Example Finding {n}")
        h.ta.pages[f"Example-Finding-{n}"] = page(900 + n, f"Example Finding {n}")
    for i in range(3):
        h.now = NOW + i * H
        h.run()
    assert len(h.reporter.notes) == 1 and library_state(h)["last_pass"] == "2026-11-01"
    first = tasks(h, "library-review:dry-run")[-1]
    for i in range(3):  # day 9: every page expired, re-looked up one a run
        h.now = NOW + 8 * DAY + i * H
        h.run()
    assert library_state(h)["last_pass"] == "2026-11-09"
    assert len(h.reporter.notes) == 1  # the plan never changed
    assert tasks(h, "library-review:dry-run")[-1].count("Example Finding") == first.count("Example Finding")


def generation_item(h, n=20):
    """A purchase whose only TA page is the Xbox One one: a "generation" review item that needs its lookup."""
    h.xbox.own(pid(n), "Example Saga", gen=("ConsoleGen9",))
    h.ta.pages["Example-Saga"] = page(950, "Example Saga", ONE)


def test_a_blocked_relookup_of_a_listed_item_keeps_the_check_task_and_pings_once(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Existing")
    assert h.run() == 0
    generation_item(h)
    h.now = NOW + DAY
    assert h.run() == 0
    assert "Example Saga" in tasks(h, "library-check")[-1]
    assert [t for t, _ in h.reporter.notes] == ["Library sync: 1 to check by hand"]
    h.ta.fail_page["Example-Saga"] = Unreachable("network error: ConnectionError")  # its weekly re-lookup is blocked
    h.now = NOW + 8 * DAY
    assert h.run() == 0 and not issue_notes(h)
    assert tasks(h, "library-check")[1:] and None not in tasks(h, "library-check")[1:]
    assert "Example Saga" in tasks(h, "library-check")[-1]
    assert [t for t, _ in h.reporter.notes] == ["Library sync: 1 to check by hand"]  # no second ping


def test_the_check_ping_has_titles_and_the_count_but_no_product_ids(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Existing")
    h.run()
    generation_item(h)
    h.now = NOW + DAY
    h.run()
    title, body = h.reporter.notes[0]
    assert title == "Library sync: 1 to check by hand" and "Example Saga" in body and pid(20) not in body
    assert pid(20) in tasks(h, "library-check")[-1]


def test_a_mistyped_override_on_a_seen_game_is_listed_and_pinged(tmp_path):
    h = library_only(tmp_path, dry=False)
    h.xbox.own(pid(1), "Example Lost Game")  # no TA page: a seen review item at go-live
    assert h.run() == 0 and pid(1) in library_state(h)["seen"]
    assert tasks(h, "library-check") == [None]
    h.cfg.overrides.write_text(json.dumps({"library": {pid(1): {"ta": "Example-Nowhere"}}}))
    h.now = NOW + DAY
    assert h.run() == 0
    assert f"Example Lost Game (`{pid(1)}`)" in tasks(h, "library-check")[-1]
    assert [t for t, _ in h.reporter.notes] == ["Library sync: 1 to check by hand"]


def test_the_sign_in_task_closes_as_soon_as_the_sign_in_works(tmp_path):
    h = library_only(tmp_path)
    h.xbox.reject = True
    h.now = NOW.replace(hour=12, minute=7)
    h.run()
    h.xbox.reject = False
    h.xbox.fail["collections"] = 500  # the read after the sign-in fails
    h.now = NOW.replace(hour=12, minute=7) + DAY
    h.run()
    assert tasks(h, "library-signin")[-1] is None and "signin_task" not in library_state(h)
    assert "delete the temp file" in tasks(h, "library-signin")[0]

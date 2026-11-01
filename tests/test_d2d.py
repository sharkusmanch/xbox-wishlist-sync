import copy
import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wsync import d2d
from wsync.feed import XboxItem
from wsync.state import State

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def item(pid, title, product_kind="game"):
    return XboxItem(pid, title, product_kind, "released", ("xbox",))


def ta_page(titles, path="/disc-to-digital/games", last=1):
    rows = "".join(
        f'<tr class="even" id="tr{i}"><td class="game"><a href="/game/x{i}/achievements">{t}</a></td>'
        f"<td>08 Sep 26</td></tr>"
        for i, t in enumerate(titles)
    )
    links = "".join(f'<a href="{path}?page={n}">{n}</a>' for n in range(2, last + 1))
    return (
        f'<link rel="canonical" href="https://www.trueachievements.com{path}">'
        f"<table>{rows}</table><ul>{links}</ul>"
    )


# ---------------------------------------------------------------- verdict


def expected(ta_yes, ta_no, xct):
    if xct in ("varies", "unconfirmed", "retest"):
        return "disputed"
    pos = ta_yes or xct == "yes"
    neg = ta_no or xct == "no"
    if pos and neg:
        return "disputed"
    if pos:
        return "yes"
    if neg:
        return "no"
    return None


@pytest.mark.parametrize(
    "ta_yes,ta_no,xct",
    list(itertools.product([False, True], [False, True],
                           [None, "yes", "no", "varies", "unconfirmed", "retest"])),
)
def test_verdict_truth_table(ta_yes, ta_no, xct):
    assert d2d.verdict(ta_yes, ta_no, xct) == expected(ta_yes, ta_no, xct)


def test_verdict_named_cases():
    assert d2d.verdict(True, False, None) == "yes"
    assert d2d.verdict(False, False, "yes") == "yes"
    assert d2d.verdict(True, False, "no") == "disputed"
    assert d2d.verdict(False, True, "yes") == "disputed"
    assert d2d.verdict(False, True, None) == "no"
    assert d2d.verdict(False, False, "no") == "no"
    assert d2d.verdict(False, False, "varies") == "disputed"
    assert d2d.verdict(False, False, None) is None


# ---------------------------------------------------------------- TA list parsing


def test_parse_ta_list_real_fixture():
    html = (FIX / "ta_d2d_list_page.html").read_text(encoding="utf-8")
    assert d2d.parse_ta_list(html) == [
        "007 First Light",
        "7 Days to Die (Xbox One)",
        "Asterix & Obelix XXL 2",
        "Assassin's Creed Black Flag Resynced",
        "Baldur's Gate 3",
        "Call of Duty: Vanguard",
    ]
    assert d2d.ta_last_page(html, "/disc-to-digital/games") == 7


def test_parse_ta_list_ignores_non_rows():
    assert d2d.parse_ta_list("<html>nothing</html>") == []


# ---------------------------------------------------------------- fetch_ta_lists


class FakeFetch:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get_html(self, url):
        self.urls.append(url)
        return self.pages[url]


def build_pages(n_yes=120, n_no=60, per_page=50):
    base = "https://www.trueachievements.com"
    pages = {}
    for path, n, tag in (("/disc-to-digital/games", n_yes, "Y"),
                         ("/disc-to-digital-unsupported/games", n_no, "N")):
        titles = [f"{tag} Game {i}" for i in range(n)]
        chunks = [titles[i:i + per_page] for i in range(0, n, per_page)]
        for p, chunk in enumerate(chunks, 1):
            url = base + path + ("" if p == 1 else f"?page={p}")
            pages[url] = ta_page(chunk, path, last=len(chunks))
    return pages


def test_fetch_ta_lists_follows_pages():
    fetch = FakeFetch(build_pages())
    lists = d2d.fetch_ta_lists(fetch, pause=0)
    assert len(lists["yes"]) == 120 and len(lists["no"]) == 60
    assert lists["yes"][0] == "Y Game 0" and lists["no"][-1] == "N Game 59"
    assert fetch.urls == [
        "https://www.trueachievements.com/disc-to-digital/games",
        "https://www.trueachievements.com/disc-to-digital/games?page=2",
        "https://www.trueachievements.com/disc-to-digital/games?page=3",
        "https://www.trueachievements.com/disc-to-digital-unsupported/games",
        "https://www.trueachievements.com/disc-to-digital-unsupported/games?page=2",
    ]


def test_fetch_ta_lists_rejects_short_list():
    with pytest.raises(d2d.D2DError, match="only 10"):
        d2d.fetch_ta_lists(FakeFetch(build_pages(n_yes=10)), pause=0)
    with pytest.raises(d2d.D2DError, match="only 49"):
        d2d.fetch_ta_lists(FakeFetch(build_pages(n_no=49)), pause=0)


def test_fetch_ta_lists_rejects_wrong_page():
    pages = build_pages()
    pages["https://www.trueachievements.com/disc-to-digital/games"] = "<html>Just a moment...</html>"
    with pytest.raises(d2d.D2DError, match="did not return"):
        d2d.fetch_ta_lists(FakeFetch(pages), pause=0)


# ---------------------------------------------------------------- xct_lookup


def xct_fixture():
    return json.loads((FIX / "xct_search.json").read_text(encoding="utf-8"))


class FakeXct:
    def __init__(self, data=None, raises=None):
        self.data = data if data is not None else xct_fixture()
        self.raises = raises
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if self.raises:
            raise self.raises
        return copy.deepcopy(self.data.get(params["q"], {"rows": [], "shown": 0, "total": 0}))


def test_xct_lookup_request_shape():
    http = FakeXct()
    assert d2d.xct_lookup("Alan Wake 2", http) == "yes"
    url, params = http.calls[0]
    assert url == "https://xct.live/api/v1/d2d/list"
    assert params == {"sort": "title", "dir": "asc", "limit": 50, "offset": 0,
                      "status": "", "q": "Alan Wake 2"}


def test_xct_lookup_not_found_is_none():
    assert d2d.xct_lookup("Alien Isolation", FakeXct()) is None


def test_xct_lookup_combines_sx_and_x1_prints():
    # Both prints are "yes".
    assert d2d.xct_lookup("Battlefield 2042", FakeXct()) == "yes"


def row(title, status="yes", varies=False):
    return {"title": title, "status": status, "varies": varies}


def one_query(rows):
    return FakeXct({"Foo": {"rows": rows, "shown": len(rows), "total": len(rows)}})


def test_xct_lookup_prints_differ_is_varies():
    http = one_query([row("Foo (SX)", "yes"), row("Foo (X1)", "no")])
    assert d2d.xct_lookup("Foo", http) == "varies"


def test_xct_lookup_row_flagged_varies():
    assert d2d.xct_lookup("Foo", one_query([row("Foo", "yes", varies=True)])) == "varies"
    # An unconfirmed row stays unconfirmed even when flagged varies.
    assert d2d.xct_lookup("Foo", one_query([row("Foo", "unconfirmed", varies=True)])) == "unconfirmed"


@pytest.mark.parametrize("status", ["yes", "no", "unconfirmed", "retest"])
def test_xct_lookup_single_status(status):
    assert d2d.xct_lookup("Foo", one_query([row("Foo", status)])) == status


def test_xct_lookup_unknown_status_is_unconfirmed():
    assert d2d.xct_lookup("Foo", one_query([row("Foo", "banana")])) == "unconfirmed"


def test_xct_lookup_prefers_plain_title_over_prints():
    http = one_query([row("Foo", "yes"), row("Foo (China)", "no"), row("Foo (Japan)", "no")])
    assert d2d.xct_lookup("Foo", http) == "yes"


def test_xct_lookup_region_print_only_when_needed():
    assert d2d.xct_lookup("Foo", one_query([row("Foo (China)", "no")])) == "no"


def test_xct_lookup_ignores_other_titles():
    # A literal substring search returns sequels and editions; only the exact title counts.
    http = one_query([row("Foo 2", "yes"), row("Foo: Remastered", "no")])
    assert d2d.xct_lookup("Foo", http) is None


def test_xct_lookup_matches_trademark_and_apostrophe_forms():
    http = FakeXct({"Assassin’s Creed Unity": {"rows": [row("Assassin's Creed Unity", "no")]}})
    assert d2d.xct_lookup("Assassin’s Creed™ Unity", http) == "no"


def test_xct_lookup_retries_other_apostrophe_form_when_not_found():
    http = FakeXct({"Assassin’s Creed Unity": {"rows": [row("Assassin’s Creed Unity", "yes")]}})
    assert d2d.xct_lookup("Assassin's Creed Unity", http) == "yes"
    assert [c[1]["q"] for c in http.calls] == ["Assassin's Creed Unity", "Assassin’s Creed Unity"]


def test_xct_lookup_strips_trademark_signs_from_query():
    http = FakeXct({"Diablo IV": {"rows": [row("Diablo IV", "yes")]}})
    assert d2d.xct_lookup("Diablo® IV", http) == "yes"
    assert http.calls[0][1]["q"] == "Diablo IV"


def test_xct_lookup_follows_pages_until_total():
    pages = {0: [row(f"Foo {i}") for i in range(50)], 50: [row("Foo", "no")]}

    def http(url, params):
        return {"rows": pages[params["offset"]], "shown": 51, "total": 51}

    assert d2d.xct_lookup("Foo", http) == "no"


def test_xct_lookup_bad_response_raises():
    with pytest.raises(d2d.D2DError):
        d2d.xct_lookup("Foo", lambda url, params: {"error": "nope"})


def test_default_http_get_uses_specified_user_agent(monkeypatch):
    seen = {}

    class Resp:
        headers = {}

        def read(self):
            return b'{"rows": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["ua"] = req.get_header("User-agent")
        seen["url"] = req.full_url
        return Resp()

    monkeypatch.setattr(d2d.urllib.request, "urlopen", fake_urlopen)
    assert d2d.default_http_get("https://xct.live/api/v1/d2d/list", {"q": "a b"}) == {"rows": []}
    assert seen["ua"] == "xbox-wishlist-sync/1.0 (personal use)"
    assert seen["url"].endswith("/list?q=a+b")


# ---------------------------------------------------------------- refresh_verdicts


def fresh_state(yes=("Alan Wake 2",), no=("Bad Game (Xbox One)",), age=timedelta(hours=1)):
    return State(ta_lists={"fetched": (NOW - age).isoformat(), "yes": list(yes), "no": list(no)})


def never_fetch():
    class F:
        def get_html(self, url):
            raise AssertionError("TA must not be fetched")

    return F()


def never_http(url, params):
    raise AssertionError("XCT must not be queried")


def test_refresh_uses_fresh_ta_lists_without_fetching():
    state = fresh_state()
    out = d2d.refresh_verdicts([item("A", "Alan Wake 2")], state, never_fetch(), FakeXct(), NOW)
    assert out == {"A": "yes"}


def test_refresh_ta_membership_strips_platform_suffix_and_edition():
    state = fresh_state(yes=("Foo (Xbox Series X|S)",), no=("Bar (Xbox One)",))
    items = [item("A", "Foo"), item("B", "Bar Deluxe Edition"), item("C", "Baz")]
    http = FakeXct({})
    out = d2d.refresh_verdicts(items, state, never_fetch(), http, NOW)
    assert out == {"A": "yes", "B": "no", "C": None}


def test_refresh_refetches_stale_ta_lists_and_stores_them():
    state = fresh_state(age=timedelta(hours=25))
    fetch = FakeFetch(build_pages())
    out = d2d.refresh_verdicts([item("A", "Y Game 3")], state, fetch, FakeXct({}), NOW, pause=0)
    assert out == {"A": "yes"}
    assert fetch.urls
    assert state.ta_lists["fetched"] == NOW.isoformat()
    assert len(state.ta_lists["yes"]) == 120 and len(state.ta_lists["no"]) == 60


def test_refresh_stale_lists_used_when_refetch_fails():
    state = fresh_state(age=timedelta(hours=30))

    class Broken:
        def get_html(self, url):
            raise OSError("boom")

    out = d2d.refresh_verdicts([item("A", "Alan Wake 2")], state, Broken(), FakeXct({}), NOW, pause=0)
    assert out == {"A": "yes"}
    assert state.ta_lists["fetched"] != NOW.isoformat()


def test_refresh_no_lists_and_fetch_fails_raises():
    class Broken:
        def get_html(self, url):
            raise OSError("boom")

    with pytest.raises(OSError):
        d2d.refresh_verdicts([item("A", "X")], State(), Broken(), FakeXct({}), NOW, pause=0)


def test_refresh_dlc_skipped():
    state = fresh_state(yes=("Alan Wake 2 Expansion",))
    out = d2d.refresh_verdicts([item("D", "Alan Wake 2 Expansion", "durable")], state,
                               never_fetch(), never_http, NOW)
    assert out == {"D": None}
    assert "D" not in state.d2d


def test_refresh_caches_xct_result_in_state():
    state = fresh_state()
    http = FakeXct()
    out = d2d.refresh_verdicts([item("B", "Battlefield 2042")], state, never_fetch(), http, NOW)
    assert out == {"B": "yes"}
    assert state.d2d["B"]["verdict"] == "yes"
    assert state.d2d["B"]["checked"] == NOW.isoformat()
    assert len(http.calls) == 1


def test_refresh_xct_cache_hit_within_7_days_no_lookup():
    state = fresh_state()
    d2d.refresh_verdicts([item("B", "Battlefield 2042")], state, never_fetch(), FakeXct(), NOW)
    later = NOW + timedelta(days=6, hours=23)
    state.ta_lists["fetched"] = later.isoformat()
    out = d2d.refresh_verdicts([item("B", "Battlefield 2042")], state, never_fetch(), never_http, later)
    assert out == {"B": "yes"}


def test_refresh_xct_cache_expires_after_7_days():
    state = fresh_state()
    d2d.refresh_verdicts([item("B", "Battlefield 2042")], state, never_fetch(), FakeXct(), NOW)
    later = NOW + timedelta(days=7, minutes=1)
    state.ta_lists["fetched"] = later.isoformat()
    http = FakeXct({"Battlefield 2042": {"rows": [row("Battlefield 2042", "no")]}})
    out = d2d.refresh_verdicts([item("B", "Battlefield 2042")], state, never_fetch(), http, later)
    assert out == {"B": "no"}
    assert len(http.calls) == 1
    assert state.d2d["B"]["checked"] == later.isoformat()


def test_refresh_cached_xct_is_recombined_with_current_ta_lists():
    state = fresh_state(yes=())
    d2d.refresh_verdicts([item("B", "Battlefield 2042")], state, never_fetch(), FakeXct(), NOW)
    assert state.d2d["B"]["verdict"] == "yes"
    state.ta_lists["no"] = ["Battlefield 2042"]
    out = d2d.refresh_verdicts([item("B", "Battlefield 2042")], state, never_fetch(), never_http,
                               NOW + timedelta(days=1))
    assert out == {"B": "disputed"}
    assert state.d2d["B"]["verdict"] == "disputed"


def test_refresh_not_in_xct_is_cached_too():
    state = fresh_state()
    http = FakeXct()
    d2d.refresh_verdicts([item("A", "Alien Isolation")], state, never_fetch(), http, NOW)
    d2d.refresh_verdicts([item("A", "Alien Isolation")], state, never_fetch(), never_http, NOW)
    assert state.d2d["A"]["verdict"] is None
    assert len(http.calls) == 1


def test_refresh_budget_caps_xct_lookups_at_10():
    state = fresh_state(yes=(), no=())
    items = [item(f"P{i}", f"Game {i}") for i in range(25)]
    http = FakeXct({})
    out = d2d.refresh_verdicts(items, state, never_fetch(), http, NOW)
    assert len(http.calls) == 10
    assert set(state.d2d) == {f"P{i}" for i in range(10)}
    assert set(out) == {i.product_id for i in items}
    # The rest are picked up on later runs, in order.
    d2d.refresh_verdicts(items, state, never_fetch(), http, NOW)
    assert len(http.calls) == 20
    assert set(state.d2d) == {f"P{i}" for i in range(20)}


def test_refresh_budget_counts_apostrophe_retry_as_one_lookup():
    state = fresh_state(yes=(), no=())
    items = [item(f"P{i}", f"It's {i}") for i in range(12)]
    http = FakeXct({})
    d2d.refresh_verdicts(items, state, never_fetch(), http, NOW)
    assert len({c[1]["q"] for c in http.calls}) == 20  # two forms per lookup
    assert len(state.d2d) == 10


def test_refresh_over_budget_item_gets_ta_only_verdict_uncached():
    state = fresh_state(yes=("Late Game",), no=())
    items = [item(f"P{i}", f"Game {i}") for i in range(10)] + [item("L", "Late Game")]
    out = d2d.refresh_verdicts(items, state, never_fetch(), FakeXct({}), NOW)
    assert out["L"] == "yes"
    assert "L" not in state.d2d


def test_refresh_expired_entry_over_budget_keeps_stale_xct():
    state = fresh_state(yes=(), no=())
    old = (NOW - timedelta(days=30)).isoformat()
    state.d2d["S"] = {"verdict": "no", "checked": old, "xct": "no"}
    items = [item(f"P{i}", f"Game {i}") for i in range(10)] + [item("S", "Stale Game")]
    out = d2d.refresh_verdicts(items, state, never_fetch(), FakeXct({}), NOW)
    assert out["S"] == "no"
    assert state.d2d["S"]["checked"] == old


def test_refresh_xct_failure_is_not_cached_and_uses_ta():
    state = fresh_state()
    out = d2d.refresh_verdicts([item("A", "Alan Wake 2")], state, never_fetch(),
                               FakeXct(raises=OSError("down")), NOW)
    assert out == {"A": "yes"}
    assert state.d2d["A"] == {"attempted": NOW.isoformat()}  # no answer cached, only the attempt


def test_refresh_legacy_cache_entry_without_xct_is_requeried():
    state = fresh_state()
    state.d2d["A"] = {"verdict": "yes", "checked": NOW.isoformat()}
    http = FakeXct()
    d2d.refresh_verdicts([item("A", "Alan Wake 2")], state, never_fetch(), http, NOW)
    assert len(http.calls) == 1


# ---------------------------------------------------------------- review round 1


def test_strip_edition_noise_keeps_case_and_punctuation():
    from wsync.match import strip_edition_noise

    assert strip_edition_noise("Alan Wake 2 Deluxe Edition") == "Alan Wake 2"
    assert strip_edition_noise("CONTROL Resonant Digital Deluxe Edition") == "CONTROL Resonant"
    assert strip_edition_noise("Assassin’s Creed: Unity - Gold Edition") == "Assassin’s Creed: Unity"
    assert strip_edition_noise("Foo Deluxe Edition for Xbox One") == "Foo"
    assert strip_edition_noise("Foo (Xbox Series X|S)") == "Foo"
    assert strip_edition_noise("Alan Wake 2") == "Alan Wake 2"
    assert strip_edition_noise("Deluxe Edition") == "Deluxe Edition"  # never strip to nothing


def test_xct_lookup_edition_title_falls_back_to_stripped_query():
    http = FakeXct()
    assert d2d.xct_lookup("Alan Wake 2 Deluxe Edition", http) == "yes"
    assert [c[1]["q"] for c in http.calls] == ["Alan Wake 2 Deluxe Edition", "Alan Wake 2"]


def test_xct_lookup_fallback_query_keeps_original_case_and_apostrophe_retry():
    http = FakeXct({"Assassin’s Creed Unity": {"rows": [row("Assassin’s Creed Unity", "no")]}})
    assert d2d.xct_lookup("Assassin's Creed Unity GOLD EDITION", http) == "no"
    assert [c[1]["q"] for c in http.calls] == [
        "Assassin's Creed Unity GOLD EDITION",
        "Assassin’s Creed Unity GOLD EDITION",
        "Assassin's Creed Unity",
        "Assassin’s Creed Unity",
    ]


def test_xct_lookup_no_fallback_query_without_noise():
    http = FakeXct()
    assert d2d.xct_lookup("Alien Isolation", http) is None
    assert [c[1]["q"] for c in http.calls] == ["Alien Isolation"]


def test_xct_lookup_literal_hit_skips_fallback():
    http = FakeXct()
    assert d2d.xct_lookup("Alan Wake 2", http) == "yes"
    assert len(http.calls) == 1


def test_xct_lookup_rebuilding_without_match_is_incomplete():
    http = FakeXct({"Foo": {"rows": [], "rebuilding": True}})
    with pytest.raises(d2d.XctIncomplete):
        d2d.xct_lookup("Foo", http)


def test_xct_lookup_rebuilding_with_match_is_definitive():
    http = FakeXct({"Foo": {"rows": [row("Foo", "yes")], "rebuilding": True}})
    assert d2d.xct_lookup("Foo", http) == "yes"


def test_refresh_rebuilding_result_not_cached_and_retried():
    state = fresh_state()
    http = FakeXct({"Zed": {"rows": [], "rebuilding": True}})
    out = d2d.refresh_verdicts([item("Z", "Zed")], state, never_fetch(), http, NOW)
    assert out == {"Z": None}
    assert state.d2d["Z"] == {"attempted": NOW.isoformat()}
    http2 = FakeXct({"Zed": {"rows": [row("Zed", "no")], "rebuilding": False}})
    out = d2d.refresh_verdicts([item("Z", "Zed")], state, never_fetch(), http2, NOW)
    assert out == {"Z": "no"} and state.d2d["Z"]["xct"] == "no"


def test_refresh_rebuilding_does_not_trip_circuit_breaker():
    state = fresh_state(yes=(), no=())
    http = FakeXct({f"G{i}": {"rows": [], "rebuilding": True} for i in range(5)})
    d2d.refresh_verdicts([item(f"P{i}", f"G{i}") for i in range(5)], state, never_fetch(), http, NOW)
    assert len(http.calls) == 5


def test_refresh_circuit_breaker_after_two_consecutive_errors():
    state = fresh_state(yes=("Game 4",), no=())
    http = FakeXct(raises=OSError("down"))
    items = [item(f"P{i}", f"Game {i}") for i in range(6)]
    out = d2d.refresh_verdicts(items, state, never_fetch(), http, NOW)
    assert len(http.calls) == 2
    assert out["P4"] == "yes" and out["P5"] is None
    assert state.d2d == {"P0": {"attempted": NOW.isoformat()}, "P1": {"attempted": NOW.isoformat()}}


def test_refresh_circuit_breaker_needs_consecutive_errors():
    state = fresh_state(yes=(), no=())
    calls = []

    def http(url, params):
        calls.append(params["q"])
        if len(calls) % 2:
            raise OSError("flaky")
        return {"rows": [], "shown": 0, "total": 0}

    items = [item(f"P{i}", f"Game {i}") for i in range(10)]
    d2d.refresh_verdicts(items, state, never_fetch(), http, NOW)
    assert len(calls) == 10


# ---------------------------------------------------------------- task 8 review: attempts and order


def test_refresh_failed_attempt_keeps_stale_answer_and_records_attempt():
    state = fresh_state(yes=(), no=())
    old = (NOW - timedelta(days=30)).isoformat()
    state.d2d["S"] = {"verdict": "no", "checked": old, "xct": "no"}
    out = d2d.refresh_verdicts([item("S", "Stale Game")], state, never_fetch(), FakeXct(raises=OSError("x")), NOW)
    assert out == {"S": "no"}
    assert state.d2d["S"] == {"verdict": "no", "checked": old, "xct": "no", "attempted": NOW.isoformat()}


def test_refresh_looks_up_oldest_attempt_first(monkeypatch):
    monkeypatch.setattr(d2d, "XCT_BUDGET", 3)
    state = fresh_state(yes=(), no=())
    day = lambda n: (NOW - timedelta(days=n)).isoformat()
    state.d2d["RECENT"] = {"attempted": day(1)}
    state.d2d["OLD"] = {"attempted": day(5)}
    state.d2d["STALE"] = {"verdict": None, "checked": day(9), "xct": None}  # last answer 9 days ago
    items = [item("RECENT", "Recent"), item("OLD", "Old"), item("STALE", "Stale"), item("NEW", "New")]
    http = FakeXct({})
    d2d.refresh_verdicts(items, state, never_fetch(), http, NOW)
    assert [c[1]["q"] for c in http.calls] == ["New", "Stale", "Old"]


def test_refresh_logs_outcome_counts(caplog, monkeypatch):
    import logging

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(d2d, "XCT_BUDGET", 4)
    state = fresh_state(yes=(), no=())
    data = {"Hit": {"rows": [row("Hit", "yes")], "shown": 1, "total": 1}, "Busy": {"rows": [], "rebuilding": True}}

    def http(url, params):
        if params["q"] == "Broken":
            raise OSError("down")
        return copy.deepcopy(data.get(params["q"], {"rows": [], "shown": 0, "total": 0}))

    items = [item(f"P{i}", t) for i, t in enumerate(["Hit", "Missing", "Busy", "Broken", "Later", "Later 2"])]
    d2d.refresh_verdicts(items, state, never_fetch(), http, NOW)
    assert "hit 1, unlisted 1, incomplete 1, error 1, deferred 2" in caplog.text

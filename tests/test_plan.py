from wsync.feed import XboxItem
from wsync.match import Match
from wsync.plan import Entry, build_plan, desired_format

XBOX = frozenset({"xbox_series", "xbox_one"})


def item(pid, title="Game", product_kind="game", kind="released"):
    return XboxItem(pid, title, product_kind, kind, ("xbox",))


def deku(target, fmt="digital", platforms=XBOX, title="Game"):
    return Entry(target, title, fmt, frozenset(platforms))


def ta(target, title="Game"):
    return Entry(target, title, None, frozenset())


def matched(target):
    return Match("matched", target, [])


NONE = Match("none", None, [])


def plan(items, dm=None, tm=None, verdicts=None, dc=None, tc=None, plats=None):
    return build_plan(items, dm or {}, tm or {}, verdicts or {}, dc or {}, tc or {}, plats or {})


def test_adds_missing_entries():
    p = plan(
        [item("A", "Alpha")],
        dm={"A": matched("alpha")},
        tm={"A": matched("123")},
        plats={"alpha": frozenset({"xbox_series", "pc"})},
    )
    assert p.deku.adds == [Entry("alpha", "Alpha", "digital", frozenset({"xbox_series"}))]
    assert p.ta.adds == [Entry("123", "Alpha", None, frozenset())]
    assert p.deku.removes == [] and p.ta.removes == []


def test_add_with_unknown_platforms_is_planned_with_empty_set():
    p = plan([item("A", "Alpha")], dm={"A": matched("alpha")})
    assert p.deku.adds[0].platforms == frozenset()


def test_removes_entry_not_on_xbox():
    p = plan(
        [item("A")],
        dm={"A": matched("a")},
        tm={"A": matched("1")},
        dc={"a": deku("a"), "gone": deku("gone")},
        tc={"1": ta("1"), "9": ta("9")},
        plats={"a": XBOX},
    )
    assert [e.target for e in p.deku.removes] == ["gone"]
    assert [e.target for e in p.ta.removes] == ["9"]
    assert p.deku.paused_removes == [] and p.deku.pause_reason is None


def test_ambiguous_item_pauses_removals_but_not_adds():
    items = [item("A", "Alpha"), item("B", "Beta")]
    p = plan(
        items,
        dm={"A": Match("ambiguous", None, ["Alpha 1", "Alpha 2"]), "B": matched("beta")},
        tm={"A": matched("1"), "B": matched("2")},
        dc={"gone": deku("gone")},
        tc={"9": ta("9")},
        plats={"beta": XBOX},
    )
    assert [e.target for e in p.deku.adds] == ["beta"]
    assert p.deku.removes == []
    assert [e.target for e in p.deku.paused_removes] == ["gone"]
    assert "Alpha" in p.deku.pause_reason
    # TA has no ambiguity, so its removal proceeds.
    assert [e.target for e in p.ta.removes] == ["9"]
    assert p.ta.pause_reason is None
    kinds = [(x.kind, x.product_id) for x in p.problems]
    assert ("unmatched-deku", "A") in kinds
    assert "Alpha 1" in next(x for x in p.problems if x.kind == "unmatched-deku").detail


def test_none_match_announced_creates_problem_without_pausing():
    p = plan([item("A", "Alpha")], dm={"A": NONE}, tm={"A": NONE}, dc={"gone": deku("gone")})
    assert {x.kind for x in p.problems} == {"unmatched-deku", "unmatched-ta"}
    assert all("no page found" in x.detail for x in p.problems)
    assert [e.target for e in p.deku.removes] == ["gone"]
    assert p.deku.pause_reason is None


def test_unannounced_none_is_skipped_silently():
    p = plan([item("A", "Alpha", kind="unannounced")], dm={"A": NONE}, tm={"A": NONE})
    assert p.problems == []
    assert p.skipped_unannounced == ["Alpha"]


def test_missing_from_matches_is_treated_as_none():
    p = plan([item("A", "Alpha")])
    assert {x.kind for x in p.problems} == {"unmatched-deku", "unmatched-ta"}


def test_format_either_for_d2d_yes_and_disputed():
    assert desired_format(item("A"), "yes") == "either"
    assert desired_format(item("A"), "disputed") == "either"


def test_format_digital_for_no_and_unknown_and_dlc():
    assert desired_format(item("A"), "no") == "digital"
    assert desired_format(item("A"), "digital-only") == "digital"
    assert desired_format(item("A"), None) == "digital"
    assert desired_format(item("A", product_kind="dlc"), "yes") == "digital"
    assert desired_format(item("A", product_kind="dlc"), "disputed") == "digital"


def test_update_when_format_or_platform_differs():
    cur = deku("a", fmt="physical", platforms={"xbox_one"})
    p = plan(
        [item("A")],
        dm={"A": matched("a")},
        dc={"a": cur},
        plats={"a": XBOX},
    )
    assert p.deku.adds == []
    assert len(p.deku.updates) == 1
    current, desired = p.deku.updates[0]
    assert current is cur
    assert desired.format == "digital" and desired.platforms == XBOX


def test_update_keeps_current_platforms_when_site_platforms_unknown():
    cur = deku("a", fmt="physical", platforms={"xbox_one"})
    p = plan([item("A")], dm={"A": matched("a")}, dc={"a": cur})
    (_, desired), = p.deku.updates
    assert desired.platforms == frozenset({"xbox_one"})


def test_disputed_verdict_creates_d2d_problem():
    p = plan(
        [item("A", "Alpha"), item("D", "Dlc", product_kind="dlc")],
        dm={"A": matched("a"), "D": matched("d")},
        verdicts={"A": "disputed", "D": "disputed"},
        plats={"a": XBOX, "d": XBOX},
    )
    probs = [x for x in p.problems if x.kind == "d2d-disputed"]
    assert [x.product_id for x in probs] == ["A"]
    assert probs[0].detail == "Disc to Digital sources disagree"
    assert p.deku.adds[0].format == "either"


def test_matched_entry_with_correct_settings_is_untouched():
    p = plan(
        [item("A")],
        dm={"A": matched("a")},
        tm={"A": matched("1")},
        dc={"a": deku("a")},
        tc={"1": ta("1")},
        plats={"a": XBOX},
    )
    for lp in (p.deku, p.ta):
        assert lp.adds == [] and lp.removes == [] and lp.updates == []


def test_ta_list_has_no_format_updates():
    p = plan(
        [item("A")],
        tm={"A": matched("1")},
        tc={"1": Entry("1", "Game", "physical", frozenset({"xbox_one"}))},
        verdicts={"A": "yes"},
    )
    assert p.ta.updates == [] and p.ta.adds == []


def test_two_items_same_target_planned_once_either_wins():
    p = plan(
        [item("A", "Alpha"), item("B", "Alpha Deluxe")],
        dm={"A": matched("a"), "B": matched("a")},
        verdicts={"A": "no", "B": "yes"},
        plats={"a": XBOX},
    )
    assert len(p.deku.adds) == 1 and p.deku.adds[0].format == "either"


def test_shared_target_is_not_removed():
    p = plan(
        [item("A"), item("B")],
        dm={"A": matched("a"), "B": matched("a")},
        dc={"a": deku("a")},
        plats={"a": XBOX},
    )
    assert p.deku.removes == []


def test_matched_with_empty_target_pauses_removals():
    for target in ("", None):
        p = plan(
            [item("A", "Alpha")],
            dm={"A": Match("matched", target, [])},
            dc={"alpha": deku("alpha")},
        )
        assert p.deku.removes == []
        assert [e.target for e in p.deku.paused_removes] == ["alpha"]
        assert "Alpha" in p.deku.pause_reason
        (prob,) = [x for x in p.problems if x.kind == "unmatched-deku"]
        assert prob.detail == "unrecognized match result"


def test_unrecognized_status_pauses_removals():
    p = plan(
        [item("A", "Alpha")],
        dm={"A": Match("error", None, [])},
        tm={"A": Match("error", None, [])},
        dc={"alpha": deku("alpha")},
        tc={"1": ta("1")},
    )
    assert p.deku.removes == [] and p.ta.removes == []
    assert [e.target for e in p.deku.paused_removes] == ["alpha"]
    assert [e.target for e in p.ta.paused_removes] == ["1"]
    assert {x.kind for x in p.problems} == {"unmatched-deku", "unmatched-ta"}
    assert p.skipped_unannounced == []


def test_updates_still_happen_while_removals_paused():
    p = plan(
        [item("A", "Alpha"), item("B", "Beta")],
        dm={"A": Match("ambiguous", None, ["x"]), "B": matched("beta")},
        dc={"beta": deku("beta", fmt="physical"), "gone": deku("gone")},
        plats={"beta": XBOX},
    )
    assert len(p.deku.updates) == 1 and p.deku.updates[0][1].format == "digital"
    assert p.deku.removes == [] and [e.target for e in p.deku.paused_removes] == ["gone"]


def test_ambiguous_unannounced_pauses_and_is_not_skipped():
    p = plan(
        [item("A", "Alpha", kind="unannounced")],
        dm={"A": Match("ambiguous", None, ["Alpha 1", "Alpha 2"])},
        tm={"A": NONE},
        dc={"gone": deku("gone")},
    )
    assert p.deku.removes == [] and [e.target for e in p.deku.paused_removes] == ["gone"]
    assert "Alpha" in p.deku.pause_reason
    assert [x.kind for x in p.problems] == ["unmatched-deku"]
    assert p.skipped_unannounced == []


def test_non_xbox_checkbox_is_not_stripped():
    cur = deku("a", fmt="digital", platforms={"xbox_series", "xbox_one", "switch"})
    p = plan([item("A")], dm={"A": matched("a")}, dc={"a": cur}, plats={"a": XBOX | {"switch"}})
    assert p.deku.updates == []


def test_update_desired_platforms_stay_xbox_only():
    cur = deku("a", fmt="physical", platforms={"xbox_series", "switch"})
    p = plan([item("A")], dm={"A": matched("a")}, dc={"a": cur}, plats={"a": XBOX | {"switch"}})
    ((_, desired),) = p.deku.updates
    assert desired.platforms == XBOX and desired.format == "digital"


def test_skipped_unannounced_is_deduplicated_and_per_item():
    items = [
        item("A", "Same", kind="unannounced"),
        item("B", "Same", kind="unannounced"),
        item("C", "Other"),
    ]
    p = plan(items, dm={"A": NONE, "B": NONE, "C": matched("c")}, tm={"A": NONE, "B": NONE, "C": matched("3")},
             plats={"c": XBOX})
    assert p.skipped_unannounced == ["Same"]


def test_skipped_on_one_list_only_is_still_listed_once():
    p = plan([item("A", "Alpha", kind="unannounced")], dm={"A": NONE}, tm={"A": matched("1")})
    assert p.skipped_unannounced == ["Alpha"]


def test_transient_lookups_pause_removals_without_naming_the_titles():
    from wsync.plan import transient

    p = plan(
        [item("A", "Alpha"), item("B", "Beta"), item("C", "Gamma")],
        dm={"A": Match("ambiguous", None, ["Alpha One", "Alpha Two"]), "B": transient("lookup failed (TAError)"),
            "C": transient("deferred to the next run (lookup time budget)")},
        dc={"stale": deku("stale")},
    )
    assert [e.target for e in p.deku.paused_removes] == ["stale"]
    assert p.deku.pause_reason == "ambiguous match for: Alpha; 2 lookups retried next run"
    assert "Beta" not in p.deku.pause_reason and "Gamma" not in p.deku.pause_reason


def test_only_transient_lookups_still_pause_removals():
    from wsync.plan import transient

    p = plan([item("B", "Beta")], dm={"B": transient("lookup failed (OSError)")}, dc={"stale": deku("stale")})
    assert [e.target for e in p.deku.paused_removes] == ["stale"] and p.deku.removes == []
    assert p.deku.pause_reason == "1 lookup retried next run"

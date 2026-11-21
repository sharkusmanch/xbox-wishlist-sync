import pytest

from wsync.meter import TAMeter
from wsync.ta import BudgetSpent, PageUnavailable, Unreachable

BUDGETS = {"wishlist": 2, "library": 5, "collection": 5, "other": 5}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make(counts=None, *, budgets=None, ceiling=100, gap=4.0, jitter=0.0, rand=lambda: 0.0, day="2026-11-20"):
    clock = Clock()
    days = {"day": day}
    meter = TAMeter({} if counts is None else counts, budgets=budgets or BUDGETS, ceiling=ceiling, gap=gap,
                    jitter=jitter, clock=lambda: clock.now, sleep=clock.sleep, rand=rand,
                    today=lambda: days["day"])
    return meter, clock, days


def test_counts_each_request_under_the_current_step_and_persists_into_the_state_dict():
    counts = {}
    meter, _, _ = make(counts)
    with meter.step_as("library"):
        meter.before_request()
        meter.before_request()
    with meter.step_as("wishlist"):
        meter.before_request()
    assert counts == {"day": "2026-11-20", "total": 3, "by": {"library": 2, "wishlist": 1}}
    assert meter.counts is counts


def test_the_default_step_is_other_and_step_as_restores_the_previous_step():
    meter, _, _ = make()
    meter.before_request()
    with meter.step_as("library"):
        with meter.step_as("d2d"):
            meter.before_request()
        meter.before_request()
    assert meter.counts["by"] == {"other": 1, "d2d": 1, "library": 1}


def test_paces_from_request_start_with_jitter():
    meter, clock, _ = make(gap=4, jitter=2, rand=lambda: 0.5)
    meter.before_request()
    assert clock.slept == []  # the first request of a run waits for nothing
    meter.before_request()
    assert clock.slept == [5.0]
    clock.now += 6  # a request that itself took longer than the gap: no wait
    meter.before_request()
    assert clock.slept == [5.0]


def test_new_utc_day_resets_counts():
    counts = {"day": "2026-11-19", "total": 99, "by": {"library": 99}}
    meter, _, days = make(counts)
    meter.before_request()
    assert counts == {"day": "2026-11-20", "total": 1, "by": {"other": 1}}
    days["day"] = "2026-11-21"
    meter.before_request()
    assert counts == {"day": "2026-11-21", "total": 1, "by": {"other": 1}}


def test_step_share_and_ceiling_raise_budget_spent():
    meter, _, _ = make(ceiling=4)
    with meter.step_as("wishlist"):
        meter.before_request()
    with meter.step_as("d2d"):  # d2d draws on the wishlist share
        meter.before_request()
        with pytest.raises(BudgetSpent):
            meter.before_request()
    assert meter.counts["total"] == 2  # a refused request is not counted
    with meter.step_as("library"):
        meter.before_request()
        meter.before_request()
        with pytest.raises(BudgetSpent):  # the library share has room (5), the ceiling (4) does not
            meter.before_request()
    assert issubclass(BudgetSpent, Unreachable) and not issubclass(BudgetSpent, PageUnavailable)


def test_a_refused_request_does_not_wait():
    meter, clock, _ = make(budgets={**BUDGETS, "wishlist": 1})
    with meter.step_as("wishlist"):
        meter.before_request()
        with pytest.raises(BudgetSpent):
            meter.before_request()
    assert clock.slept == []


def test_admit_checks_room_without_counting():
    meter, _, _ = make()
    assert meter.admit(2, "wishlist") and not meter.admit(3, "wishlist")
    with meter.step_as("library"):
        assert meter.admit(5) and not meter.admit(6)
    with meter.step_as("wishlist"):
        meter.before_request()
    assert meter.admit(1, "d2d") and not meter.admit(2, "d2d")
    assert meter.counts["total"] == 1


def test_clearance_counts_toward_the_ceiling_only():
    meter, clock, _ = make(ceiling=3)
    with meter.step_as("library"):
        meter.before_request()
        meter.count_clearance()
    assert meter.counts["by"] == {"library": 1, "clearance": 1} and meter.counts["total"] == 2
    assert clock.slept == [4.0]  # a clearance is paced like any request
    assert meter.spent() == []  # no share was charged
    with meter.step_as("library"):
        meter.before_request()
        with pytest.raises(BudgetSpent):
            meter.before_request()
    assert meter.spent() == ["ceiling"]


def test_spent_lists_the_shares_and_the_ceiling():
    meter, _, _ = make(budgets={**BUDGETS, "wishlist": 1, "collection": 1}, ceiling=2)
    assert meter.spent() == []
    with meter.step_as("d2d"):
        meter.before_request()
    assert meter.spent() == ["wishlist"]
    with meter.step_as("collection"):
        meter.before_request()
    assert meter.spent() == ["wishlist", "collection", "ceiling"]


def test_a_limit_of_0_refuses_every_request_but_is_never_reported_as_spent():
    """A share (or ceiling) of 0 is a setting, "no TA requests here", not a budget used up: its requests are refused,
    but `spent()` leaves it out, so it opens no budget task and logs no spent warning at every run."""
    meter, _, _ = make(budgets={**BUDGETS, "wishlist": 0, "other": 0})
    with meter.step_as("wishlist"):
        assert not meter.admit(1)
        with pytest.raises(BudgetSpent):
            meter.before_request()
    with pytest.raises(BudgetSpent):
        meter.before_request()  # the "other" step
    assert meter.spent() == []
    meter, _, _ = make(ceiling=0)
    with meter.step_as("library"):
        with pytest.raises(BudgetSpent):
            meter.before_request()
    assert meter.spent() == []


def test_summary_line():
    counts = {"day": "2026-11-20", "total": 0, "by": {}}
    meter, _, _ = make(counts, ceiling=300)
    with meter.step_as("library"):
        meter.before_request()
        meter.before_request()
    with meter.step_as("wishlist"):
        meter.before_request()
    assert meter.summary() == "this run 3 (wishlist 1, library 2); today 3 of 300"


def test_summary_separates_this_run_from_the_days_total():
    meter, _, _ = make({"day": "2026-11-20", "total": 40, "by": {"library": 40}}, ceiling=300)
    assert meter.summary() == "this run 0; today 40 of 300"


def test_summary_lists_steps_in_step_order_with_clearance_last():
    meter, _, _ = make({}, budgets={k: 9 for k in BUDGETS}, ceiling=300)
    for step in ("other", "collection", "library", "d2d", "wishlist"):
        with meter.step_as(step):
            meter.before_request()
    meter.count_clearance()
    assert meter.summary() == ("this run 6 (wishlist 1, d2d 1, library 1, collection 1, other 1, clearance 1); "
                               "today 6 of 300")


def test_usage_gives_a_share_or_the_ceiling_as_used_and_limit():
    meter, _, days = make({"day": "2026-11-20", "total": 7, "by": {"wishlist": 1, "d2d": 1, "library": 5}},
                          ceiling=50)
    assert meter.usage("wishlist") == (2, 2)  # the wishlist share covers the d2d step too
    assert meter.usage("library") == (5, 5) and meter.usage("collection") == (0, 5)
    assert meter.usage("ceiling") == (7, 50)
    days["day"] = "2026-11-21"
    assert meter.usage("ceiling") == (0, 50)  # a new UTC day

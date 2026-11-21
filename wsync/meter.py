"""The run's one TrueAchievements request meter.

TA limits the owner's home IP, so every request to TA (each redirect hop, each login page, each Cloudflare
clearance) goes through one meter. It paces the requests (a gap plus jitter from one request's start to the
next), counts them per UTC day in the persisted state, and refuses a request once the step's share of the day's
budget, or the day's ceiling, is spent. One meter serves every client of a run, so a second client can't restart
the pacing or the counts.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable

from wsync.ta import BudgetSpent

STEPS = ("wishlist", "d2d", "library", "collection", "other")
CLEARANCE = "clearance"  # counted toward the ceiling only, never against a share
_SHARE = {"wishlist": "wishlist", "d2d": "wishlist", "library": "library", "collection": "collection",
          "other": "other"}


class TAMeter:
    def __init__(self, counts: dict, *, budgets: dict[str, int], ceiling: int, gap: float, jitter: float,
                 clock: Callable[[], float], sleep: Callable[[float], None], rand: Callable[[], float],
                 today: Callable[[], str]):
        self.counts = counts  # state.ta_requests: {"day", "total", "by": {step: n}}, mutated in place
        self._budgets, self._ceiling = budgets, ceiling
        self._gap, self._jitter = gap, jitter
        self._clock, self._sleep, self._rand, self._today = clock, sleep, rand, today
        self._step = "other"
        self._last: float | None = None  # when the last request started
        self._run: dict[str, int] = {}  # this run's requests per step

    # ---- the day's counts

    def _roll(self) -> None:
        day = self._today()
        if self.counts.get("day") != day:
            self.counts.clear()
            self.counts.update({"day": day, "total": 0, "by": {}})
        self.counts.setdefault("total", 0)
        self.counts.setdefault("by", {})

    def _used(self, share: str) -> int:
        by = self.counts["by"]
        return sum(by.get(s, 0) for s, sh in _SHARE.items() if sh == share)

    def _room(self, step: str, n: int) -> bool:
        share = _SHARE.get(step, "other")
        return (self.counts["total"] + n <= self._ceiling
                and self._used(share) + n <= self._budgets.get(share, 0))

    # ---- steps

    @contextmanager
    def step_as(self, name: str):
        """Count the requests made inside the block under this step."""
        before, self._step = self._step, name
        try:
            yield
        finally:
            self._step = before

    # ---- requests

    def _pace(self) -> None:
        now = self._clock()
        if self._last is not None:
            due = self._last + self._gap + self._rand() * self._jitter
            if due > now:
                self._sleep(due - now)
                now = due
        self._last = now

    def _count(self, name: str) -> None:
        self.counts["total"] += 1
        self.counts["by"][name] = self.counts["by"].get(name, 0) + 1
        self._run[name] = self._run.get(name, 0) + 1

    def before_request(self) -> None:
        """Called before every TA request: refuse when the budget is spent, wait out the pace, count it."""
        self._roll()
        if not self._room(self._step, 1):
            raise BudgetSpent(f"the daily TrueAchievements request budget is spent ({self._step} step)")
        self._pace()
        self._count(self._step)

    def count_clearance(self) -> None:
        """A FlareSolverr clearance also reaches TA: paced and counted, but never refused (the request that
        needed it is the one the budget governs)."""
        self._roll()
        self._pace()
        self._count(CLEARANCE)

    def admit(self, n: int, step: str | None = None) -> bool:
        """Is there room for n more requests under the step (default: the current one)? Counts nothing."""
        self._roll()
        return self._room(step or self._step, n)

    # ---- reporting

    def spent(self) -> list[str]:
        """The shares and the ceiling that are spent today. A limit of 0 is never spent: it is a setting ("no TA
        requests here"), whose requests are refused quietly, not a budget used up."""
        self._roll()
        out = [s for s in dict.fromkeys(_SHARE.values())
               if (limit := self._budgets.get(s, 0)) > 0 and self._used(s) >= limit]
        if 0 < self._ceiling <= self.counts["total"]:
            out.append("ceiling")
        return out

    def usage(self, name: str) -> tuple[int, int]:
        """(requests made today, the day's limit) for a share, or for "ceiling"."""
        self._roll()
        if name == "ceiling":
            return self.counts["total"], self._ceiling
        return self._used(name), self._budgets.get(name, 0)

    def summary(self) -> str:
        self._roll()
        order = (*STEPS, CLEARANCE)
        mine = ", ".join(f"{k} {self._run[k]}" for k in order
                         if self._run.get(k))
        run = sum(self._run.values())
        return (f"this run {run}{f' ({mine})' if mine else ''}; "
                f"today {self.counts['total']} of {self._ceiling}")

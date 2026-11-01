"""Exception types shared by the TrueAchievements client and its FlareSolverr transport."""


class TAError(Exception):
    """TrueAchievements (or the FlareSolverr path to it) answered with something we can't treat as success."""


class ReportError(Exception):
    """Vikunja (or the network path to it) answered with something we can't treat as success."""

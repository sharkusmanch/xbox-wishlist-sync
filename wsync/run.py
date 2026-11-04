"""One sync run: Xbox wishlist feed -> Deku Deals and TrueAchievements wishlists.

Order (each list is handled independently; a failure on one never blocks the other):
  1. load state, the feed (with its staleness / shrink guards) and the overrides: no login
     happens unless all three are sound;
  2. read both wishlists (logging in when the stored session has expired);
  3. refresh the Disc to Digital verdicts (Deku formats);
  4. resolve every Xbox item on every list: override, then the cached mapping (re-validated),
     then search plus a confident pick;
  5. plan; 6. apply (dry run: nothing) adds, then updates, then removes, none started past the
     write deadline; 7. save state, report. SIGTERM stops the run, saves state and exits 1.
  8. the Deku collection step (`wsync.collection`), after the wishlist step has finished and saved
     its state: TrueAchievements "Owned" Xbox games -> Deku collection, add-only. Its failures
     are issues of their own; nothing in it can change what the wishlist step did.

Safety rules beyond the planner's:
  - every Xbox item gets a Match on every list it is mirrored to; a failed lookup is
    "ambiguous", which pauses that list's removals, never "none";
  - a list whose read, login or matching phase fails makes no writes at all that run;
  - "candidates" for the ambiguity rule are results related to the item (`related`: their first
    two significant words agree, or the first one when either title has only one; leading
    articles skipped, numbers compared by value), plus any entry about to be removed that is
    related; unrelated full-text search noise doesn't count as a candidate;
  - nothing secret is logged or reported: every message passes through a scrubber that knows
    the configured secrets, and lookup failures are logged by exception class only.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, quote_plus

from wsync import d2d, report
from wsync.collection import run_collection_step
from wsync.deku import AuthError as DekuAuthError
from wsync.deku import DekuClient, DekuItem
from wsync.feed import FeedError, XboxItem, check_count, is_dlc, load_feed
from wsync.flaresolverr import FlareSolverr
from wsync.match import Match, base_title, norm, pick, strip_edition_noise
from wsync.overrides import SKIP, OverridesError, load_collection_overrides, load_overrides
from wsync.plan import XBOX_PLATFORMS, Entry, ListPlan, Plan, Problem, build_plan, is_transient, transient
from wsync.state import State, load_state, save_state
from wsync.ta import AlreadyListed
from wsync.ta import AuthError as TAAuthError
from wsync.ta import TAClient, TAGame, choose_ta_page, excluded_page

log = logging.getLogger("wsync")

LISTS = ("deku", "ta")
COLLECTION = "collection"  # `--only collection`: the Deku collection step alone
LABEL = {"deku": "Deku Deals", "ta": "TrueAchievements"}
KIND = {"deku": "unmatched-deku", "ta": "unmatched-ta"}
MAX_LOOKUP_ERRORS = 3  # consecutive failed lookups on a list before the rest are skipped
MAX_PAGES = 4  # candidate pages fetched per item (TA pages cost 3-30 s each)
WRITE_DEADLINE = 45 * 60.0  # the job's hard limit is 50 min: no write starts later than this
_FALSE = {"false", "0", "no", "off"}
_TA_SUFFIX = re.compile(r"\s*\((?:Xbox One|Xbox Series X\|S|Xbox 360|Windows|PC|Xbox)\)\s*$", re.I)
_DLC_WORDS = re.compile(
    r"[\s\-:–—]*\b(?:expansion pass|season pass|expansion|dlc|add-on|add on)\s*$", re.I
)
PLACEHOLDER = "pre-release placeholder page"  # TA: unreleased games have negative, unwritable ids
NO_XBOX_YET = "no Xbox listing on Deku Deals yet"  # Deku: an unreleased game listed for other platforms only
_PARENT_SPLIT = re.compile(r"\s+[-–—]\s+|:\s+")


class ConfigError(Exception):
    """The command line or environment can't be used."""


# --------------------------------------------------------------------------- configuration


@dataclass
class Config:
    feed: Path = Path("/feed/wishlist.json")
    state: Path = Path("/state/state.json")
    overrides: Path = Path("/config/overrides.json")
    deku_email: str = field(default="", repr=False)
    deku_password: str = field(default="", repr=False)
    deku_wishlist_id: str = field(default="", repr=False)
    ta_gamertag: str = field(default="", repr=False)
    ta_password: str = field(default="", repr=False)
    flaresolverr_url: str = ""
    vikunja_url: str = ""
    vikunja_token: str = field(default="", repr=False)
    apprise_url: str = field(default="", repr=False)
    dry_run: bool = True
    lists: tuple[str, ...] = LISTS
    collection: bool = True  # run the Deku collection step (off with --only deku|ta)
    collection_dry_run: bool = True  # COLLECTION_DRY_RUN; a dry run is always dry here too
    report: bool = True
    accept_feed_count: int | None = None
    feed_max_age: timedelta = timedelta(hours=6)
    match_budget: float = 35 * 60.0  # seconds of lookups before the rest wait for the next run
    write_deadline: float = WRITE_DEADLINE  # seconds into the run after which no write starts
    # WSYNC_TA_READ_AFTER (UTC): read the TA collection at the first run past this time each day
    # (just after TA's own daily purchase import); unset = at most once every 24 h.
    ta_read_after: dtime | None = None

    @property
    def state_dir(self) -> Path:
        return Path(self.state).parent

    def secrets(self) -> list[str]:
        """Every configured secret, in the forms it might take in a URL or message."""
        out: set[str] = set()
        for value in (self.deku_email, self.deku_password, self.deku_wishlist_id, self.ta_gamertag,
                      self.ta_password, self.vikunja_token, self.apprise_url):
            if value:
                out |= {value, quote(value, safe=""), quote_plus(value), value.replace(" ", "+")}
        return sorted(out, key=len, reverse=True)

    def missing(self) -> list[str]:
        need = []
        if "deku" in self.lists or self.collection:
            need += [("DEKU_EMAIL", self.deku_email), ("DEKU_PASSWORD", self.deku_password),
                     ("DEKU_WISHLIST_ID", self.deku_wishlist_id)]
        if "ta" in self.lists or self.collection:
            need += [("TA_GAMERTAG", self.ta_gamertag), ("TA_PASSWORD", self.ta_password)]
        need.append(("FLARESOLVERR_URL", self.flaresolverr_url))
        if self.report:
            need += [("VIKUNJA_URL", self.vikunja_url), ("VIKUNJA_TOKEN", self.vikunja_token),
                     ("APPRISE_URL", self.apprise_url)]
        return [name for name, value in need if not value]


def _int_env(env, name: str) -> int | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None


def config_from(argv: list[str], env) -> Config:
    p = argparse.ArgumentParser(
        prog="python -m wsync",
        description="Mirror the Xbox wishlist feed onto Deku Deals and TrueAchievements.",
    )
    p.add_argument("--only", choices=(*LISTS, COLLECTION),
                   help="sync one list only, or run only the Deku collection step")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="mode", action="store_const", const="dry",
                      help="plan only, write nothing (overrides DRY_RUN)")
    mode.add_argument("--apply", dest="mode", action="store_const", const="apply",
                      help="write the changes (overrides DRY_RUN)")
    p.add_argument("--no-report", action="store_true",
                   help="no Vikunja tasks or Apprise notifications; the plan is only logged")
    p.add_argument("--accept-feed-count", type=int, metavar="N",
                   help="accept a feed of exactly N items despite the shrink guard")
    a = p.parse_args(argv)

    if a.mode:
        dry = a.mode == "dry"
    else:
        dry = (env.get("DRY_RUN") or "true").strip().lower() not in _FALSE
    collection_dry = dry or (env.get("COLLECTION_DRY_RUN") or "true").strip().lower() not in _FALSE
    accept = a.accept_feed_count if a.accept_feed_count is not None else _int_env(env, "WSYNC_ACCEPT_FEED_COUNT")
    hours = (env.get("WSYNC_FEED_MAX_AGE_HOURS") or "").strip()
    try:
        max_age = timedelta(hours=float(hours)) if hours else timedelta(hours=6)
    except ValueError:
        raise ConfigError("WSYNC_FEED_MAX_AGE_HOURS must be a number") from None
    read_after = (env.get("WSYNC_TA_READ_AFTER") or "").strip()
    ta_read_after = None
    if read_after:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", read_after)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ConfigError("WSYNC_TA_READ_AFTER must be HH:MM (UTC)")
        ta_read_after = dtime(int(m.group(1)), int(m.group(2)))
    return Config(
        feed=Path(env.get("WSYNC_FEED") or "/feed/wishlist.json"),
        state=Path(env.get("WSYNC_STATE") or "/state/state.json"),
        overrides=Path(env.get("WSYNC_OVERRIDES") or "/config/overrides.json"),
        deku_email=env.get("DEKU_EMAIL", ""),
        deku_password=env.get("DEKU_PASSWORD", ""),
        deku_wishlist_id=env.get("DEKU_WISHLIST_ID", ""),
        ta_gamertag=env.get("TA_GAMERTAG", ""),
        ta_password=env.get("TA_PASSWORD", ""),
        flaresolverr_url=env.get("FLARESOLVERR_URL", ""),
        vikunja_url=env.get("VIKUNJA_URL", ""),
        vikunja_token=env.get("VIKUNJA_TOKEN", ""),
        apprise_url=env.get("APPRISE_URL", ""),
        dry_run=dry,
        lists=() if a.only == COLLECTION else (a.only,) if a.only else LISTS,
        collection=a.only in (None, COLLECTION),
        collection_dry_run=collection_dry,
        report=not a.no_report,
        accept_feed_count=accept,
        feed_max_age=max_age,
        ta_read_after=ta_read_after,
    )


# --------------------------------------------------------------------------- redaction


class Scrubber:
    def __init__(self, secrets: list[str]):
        self._re = re.compile("|".join(re.escape(s) for s in secrets), re.I) if secrets else None

    def __call__(self, text) -> str:
        text = str(text)
        return self._re.sub("<redacted>", text) if self._re else text


class RedactingFilter(logging.Filter):
    """Handler filter: scrub configured secrets from every formatted record (defence in depth)."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._scrub = Scrubber(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = self._scrub(record.getMessage()), None
        return True


# --------------------------------------------------------------------------- reporting


class NullReporter:
    """--no-report: nothing leaves the process; the plan is only logged."""

    def sync_problems(self, problems, keep_kinds, keep_keys=frozenset()) -> None:
        pass

    def plan_review(self, plan, *, dry_run) -> bool:
        return False

    def collection_review(self, body, *, dry_run) -> bool:
        return False

    def notify(self, title, body) -> None:
        pass


class LiveReporter:
    def __init__(self, cfg: Config):
        self._vk = report.Vikunja(cfg.vikunja_url, cfg.vikunja_token)
        self._apprise = cfg.apprise_url

    def sync_problems(self, problems, keep_kinds, keep_keys=frozenset()) -> None:
        report.sync_problem_tasks(self._vk, problems, keep_kinds, keep_keys)

    def plan_review(self, plan, *, dry_run) -> bool:
        return report.plan_review_task(self._vk, plan, dry_run=dry_run)

    def collection_review(self, body, *, dry_run) -> bool:
        return report.collection_review_task(self._vk, body, dry_run=dry_run)

    def notify(self, title, body) -> None:
        report.notify(self._apprise, title, body)


# --------------------------------------------------------------------------- dependencies


def _make_deku(cfg: Config) -> DekuClient:
    return DekuClient(cfg.deku_email, cfg.deku_password, cfg.deku_wishlist_id, cfg.state_dir / "deku-session")


def _make_ta(cfg: Config, game_ids: dict[str, int]) -> TAClient:
    return TAClient(cfg.ta_gamertag, cfg.ta_password, FlareSolverr(cfg.flaresolverr_url),
                    cfg.state_dir / "ta-session", game_ids=game_ids)


def _make_reporter(cfg: Config):
    return LiveReporter(cfg) if cfg.report else NullReporter()


@dataclass
class Deps:
    make_deku: Callable[[Config], Any] = _make_deku
    make_ta: Callable[[Config, dict], Any] = _make_ta
    make_reporter: Callable[[Config], Any] = _make_reporter
    refresh_verdicts: Callable = d2d.refresh_verdicts
    http_get: Callable | None = None  # XCT transport; None = d2d's default
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep  # the collection step's pacing
    d2d_pause: float = 1.0


# --------------------------------------------------------------------------- title helpers


def _query(title: str) -> str:
    # Drop the symbols outright: replacing them with a space turns "Batman™:" into "Batman :",
    # which TrueAchievements' search no longer matches.
    return " ".join(re.sub("[™®©]", "", title).split())


def _subtitles(title: str) -> list[str]:
    """The colon/dash-separated parts of a title with at least two words, for a fallback search
    when the full title finds nothing (TrueAchievements' search can't match "Δ" or "Delta",
    but "Snake Eater" finds "Metal Gear Solid Δ: Snake Eater")."""
    parts = [p.strip() for p in re.split(r"\s*(?::|\s[-–—]\s)\s*", _query(title))]
    return [p for p in parts if len(p.split()) >= 2 and norm(p) != norm(title)]


def _strip_suffix(title: str) -> str:
    return _TA_SUFFIX.sub("", title)


_ARTICLES = frozenset({"the", "a", "an"})
_ROMAN = {r: str(n) for n, r in enumerate(
    "i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split(), start=1)}


def _canon(word: str) -> str:
    """Numbers compare by value: "vii" ~ "7", "40k" ~ "40000"."""
    if word in _ROMAN:
        return _ROMAN[word]
    m = re.fullmatch(r"(\d+)k", word)
    return str(int(m.group(1)) * 1000) if m else word


def significant_words(title: str) -> list[str]:
    """`base_title` words without leading articles, numbers canonical ("40,000" is one word)."""
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", _strip_suffix(title))
    words = base_title(text).split()
    while words and words[0] in _ARTICLES:
        words = words[1:]
    return [_canon(w) for w in words]


def related(a: str, b: str) -> bool:
    """Candidate test for the ambiguity rule: the first two significant words agree (the first
    one when either title has only one). "Persona 4 Revival" ~ "Persona 4 Golden" and "Warhammer
    40,000: ..." ~ "Warhammer 40K: ...", but not "Persona 6" ~ "Persona 5 Royal". A publisher
    prefix defeats it: "Tom Clancy's The Division 2" is not related to "The Division 2".
    """
    x, y = significant_words(a), significant_words(b)
    k = min(2, len(x), len(y))
    return k > 0 and x[:k] == y[:k]


def exact_hits(title: str, results: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Results equal to the title by `norm`, else by `base_title` (pick's two tiers)."""
    for key in (norm, base_title):
        want = key(title)
        hits = [(t, s) for t, s in results if want and key(_strip_suffix(t)) == want]
        if hits:
            return hits
    return []


def parent_titles(title: str) -> list[str]:
    """Plausible base-game titles for a DLC title, most specific first."""
    out: list[str] = []
    bare = _DLC_WORDS.sub("", title).strip(" -:–—")
    if bare and norm(bare) != norm(title):
        out.append(bare)
    for m in reversed(list(_PARENT_SPLIT.finditer(title))):
        out.append(title[: m.start()].strip())
    seen: set[str] = set()
    return [t for t in out if t and not (norm(t) in seen or seen.add(norm(t)))]


def _ta_key(game_id: int, dlc_id: int = 0) -> str:
    return str(game_id) if not dlc_id else f"{game_id}:{dlc_id}"


def _ta_ids(key: str) -> tuple[int, int]:
    game, _, dlc = key.partition(":")
    return int(game), int(dlc or 0)


def _err(e: BaseException) -> str:
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _fmt_plats(platforms) -> str:
    return "+".join(sorted(platforms)) or "none"


def _pause(lp: ListPlan, reason: str) -> None:
    if lp.removes:
        lp.paused_removes = lp.paused_removes + lp.removes
        lp.removes = []
    lp.pause_reason = f"{lp.pause_reason}; {reason}" if lp.pause_reason else reason


HIDDEN_DETAIL = "already on the Deku wishlist but hidden from the share page"


def failure_digest(issues: list[str]) -> str:
    """Identity of a failure: the sorted issue texts with numbers blanked ("7.3 h old" ~ "8.3 h old")."""
    text = "\n".join(sorted({re.sub(r"\d+", "#", i) for i in issues}))
    return hashlib.sha256(text.encode()).hexdigest()


def _is_empty(plan: Plan) -> bool:
    return not any(lp.adds or lp.removes or lp.updates or lp.paused_removes for lp in (plan.deku, plan.ta))


# --------------------------------------------------------------------------- the run


class _Deferred(Exception):
    """A lookup that needs a search, after the run's lookup time budget is spent."""


@dataclass
class _List:
    name: str
    enabled: bool
    items: list[XboxItem]
    client: Any = None
    current: dict[str, Entry] | None = None
    matches: dict[str, Match] = field(default_factory=dict)
    failed: str | None = None  # set: no writes at all on this list this run
    pause: list[str] = field(default_factory=list)  # reasons to make no removals
    errors: int = 0
    stop: str | None = None  # set: remaining lookups are skipped (ambiguous)
    deferred: int = 0

    @property
    def active(self) -> bool:
        return self.enabled and self.failed is None and self.client is not None and self.current is not None

    @property
    def label(self) -> str:
        return LABEL[self.name]


class Terminated(BaseException):
    """SIGTERM arrived (the job's deadline, or the pod is being stopped).

    A BaseException, so the per-item `except Exception` handlers never swallow it.
    """


class _Run:
    def __init__(self, cfg: Config, deps: Deps):
        self.cfg, self.deps = cfg, deps
        self.scrub = Scrubber(cfg.secrets())
        self.issues: list[str] = []
        self.reporter: Any = NullReporter()
        self.start = deps.monotonic()
        self.state = State()
        self.overrides: dict[str, dict] = {}
        self.deku_items: dict[str, DekuItem] = {}
        self._clients: dict[str, Any] = {}
        self.state_ok = False  # the state file loaded; only then may it be written back
        self.feed_count: int | None = None
        self.keep_keys: set[str] = set()
        self.late_writes = 0
        self.ta_client: Any = None

    # ---- logging

    def say(self, msg: str, level: int = logging.INFO) -> None:
        log.log(level, "%s", self.scrub(msg))

    def issue(self, msg: str) -> None:
        msg = self.scrub(msg)
        self.issues.append(msg)
        log.error("%s", msg)

    def notify(self, title: str, body: str) -> None:
        try:
            self.reporter.notify(self.scrub(title), self.scrub(body))
        except Exception as e:  # noqa: BLE001 - a notification must never take the run down
            self.say(f"notification failed ({type(e).__name__})", logging.WARNING)

    def fail(self) -> int:
        return self._finish()

    def sleep(self, seconds: float) -> None:
        self.deps.sleep(seconds)

    def checkpoint(self) -> None:
        """Save the state now (the collection step's write-ahead before an add). Raises on failure."""
        save_state(self.cfg.state, self.state)

    def past_deadline(self) -> bool:
        return self.deps.monotonic() - self.start > self.cfg.write_deadline

    def write_time_left(self) -> float:
        """Seconds until the write deadline (negative once past it)."""
        return self.cfg.write_deadline - (self.deps.monotonic() - self.start)

    @property
    def collection_dry(self) -> bool:
        return self.cfg.dry_run or self.cfg.collection_dry_run

    def _finish(self) -> int:
        """Exit code; the failure ping goes out only when the set of issues changed since last run."""
        digest = failure_digest(self.issues) if self.issues else None
        if digest is not None:
            if self.state_ok and self.state.last_failure == digest:
                self.say("failure unchanged since the last run; not notifying again", logging.WARNING)
            else:
                self.notify("Wishlist sync failed", "\n".join(f"- {i}" for i in self.issues))
        if self.state_ok and self.state.last_failure != digest:
            self.state.last_failure = digest
            try:
                save_state(self.cfg.state, self.state)
            except Exception as e:  # noqa: BLE001
                self.say(f"state not saved ({type(e).__name__})", logging.ERROR)
        if digest is None:
            self.say("wsync finished: ok")
            return 0
        return 1

    # ---- entry

    def go(self) -> int:
        try:
            try:
                self.reporter = self.deps.make_reporter(self.cfg)
            except Exception as e:  # noqa: BLE001
                self.issue(f"reporting setup failed ({type(e).__name__}); continuing without it")
                self.reporter = NullReporter()
            try:
                return self._go()
            except Exception as e:  # noqa: BLE001 - anything unexpected still fails loudly, once
                self.issue(f"run aborted by an unexpected error: {_err(e)}")
                return self.fail()
        except Terminated:
            return self._terminated()

    def _terminated(self) -> int:
        """SIGTERM: no further writes; keep what this run learned (lookups, TA game ids), exit 1."""
        self.issue("run stopped by SIGTERM (job deadline or shutdown); remaining work waits for the next run")
        if self.state_ok:
            if self.ta_client is not None:
                ids = getattr(self.ta_client, "game_ids", {}) or {}
                self.state.ta_game_ids = {k: v for k, v in ids.items() if isinstance(v, int) and v > 0}
            try:
                save_state(self.cfg.state, self.state)
            except Exception as e:  # noqa: BLE001
                self.say(f"state not saved ({type(e).__name__})", logging.ERROR)
        return self.fail()

    def _go(self) -> int:
        cfg = self.cfg
        step = ("off" if not cfg.collection else "DRY RUN" if self.collection_dry else "APPLY")
        self.say(f"wsync starting: {'DRY RUN (no writes)' if cfg.dry_run else 'APPLY'}, "
                 f"lists: {', '.join(cfg.lists) or 'none'}; Deku collection step: {step}")
        missing = cfg.missing()
        if missing:
            self.issue(f"missing configuration: {', '.join(missing)}")
            return self.fail()

        try:
            self.state = load_state(cfg.state)
            self.state_ok = True
        except Exception as e:  # noqa: BLE001 - corrupt JSON surfaces as several error types
            self.issue(f"state file unreadable ({type(e).__name__}); fix or delete {cfg.state}")
            return self.fail()
        if cfg.lists:
            self._wishlist()
        if cfg.collection:
            self._collection()
        return self._finish()

    def _wishlist(self) -> None:
        """Steps 1-7: the wishlist mirror. Its failures are recorded as issues; it never raises them."""
        cfg = self.cfg
        # 1. feed, overrides: both sound before any login (the state is already loaded)
        try:
            items = self._load_feed()
        except FeedError as e:
            self.issue(f"feed rejected, nothing written: {e}")
            return
        try:
            self.overrides = load_overrides(cfg.overrides)
        except OverridesError as e:
            self.issue(f"overrides rejected, nothing written: {e}")
            return
        self.say(f"feed: {len(items)} items; overrides: {len(self.overrides)}")

        lists = {
            name: _List(name, name in cfg.lists,
                        [i for i in items if self.overrides.get(i.product_id, {}).get(name) != SKIP])
            for name in LISTS
        }
        for name, lst in lists.items():
            for i in items:
                if self.overrides.get(i.product_id, {}).get(name) == SKIP:
                    self.say(f"{name}: skipped by override: {i.title}")

        # 2. clients and wishlists
        deku, ta = lists["deku"], lists["ta"]
        if deku.enabled:
            try:
                deku.client = self.deps.make_deku(cfg)
            except Exception as e:  # noqa: BLE001
                deku.failed = f"client setup failed ({type(e).__name__})"
                self.issue(f"{deku.label}: {deku.failed}")
        ta_client = None
        if ta.enabled or deku.enabled:  # Deku needs TA's D2D lists
            try:
                ta_client = self.deps.make_ta(cfg, dict(self.state.ta_game_ids))
            except Exception as e:  # noqa: BLE001
                self.issue(f"{ta.label}: client setup failed ({type(e).__name__})")
                ta.failed = "client setup failed"
        self.ta_client = ta_client
        ta.client = ta_client if ta.enabled else None
        self._clients = {"deku": deku.client, "ta": ta.client}
        self._read(deku, with_platforms=True)
        self._read(ta)

        # 3. Disc to Digital verdicts (only Deku formats use them)
        verdicts: dict[str, str | None] = {}
        d2d_ok = True
        if deku.active:
            verdicts, d2d_ok = self._verdicts(deku.items, ta_client)

        # 4. matching
        if deku.active:
            self._match(deku, self._deku_resolve)
        if ta.active:
            self._match(ta, self._ta_resolve)

        # 5. plan
        plan = self._plan(lists, verdicts, d2d_ok)
        self._log_plan(plan)

        # 6. apply
        applied = Plan()
        if not cfg.dry_run:
            if deku.active:
                self._apply_deku(deku, plan.deku, applied.deku)
            if ta.active:
                self._apply_ta(ta, plan.ta, applied.ta)

        # 7. state, then reporting
        self._save_state(items, lists, ta_client)
        keep = {KIND[n] for n, lst in lists.items() if not lst.active}
        if not deku.active or not d2d_ok:
            keep.add("d2d-disputed")
        self._report(plan, applied, keep)

    # ---- 8. the Deku collection step

    def _collection(self) -> None:
        """Runs after the wishlist step finished and saved its state; whatever happens here is an
        issue of its own (or a quiet stop), never a change to the wishlist step's results."""
        cfg = self.cfg
        if self.past_deadline():
            self.say("collection: step not started, past the write deadline; next run", logging.WARNING)
            return
        try:
            overrides = load_collection_overrides(cfg.overrides)
        except OverridesError as e:
            self.issue(f"Deku collection step skipped: collection overrides rejected: {e}")
            return
        try:
            deku = self._clients.get("deku") or self.deps.make_deku(cfg)
            ta = self.ta_client or self.deps.make_ta(cfg, dict(self.state.ta_game_ids))
        except Exception as e:  # noqa: BLE001
            self.issue(f"Deku collection step: client setup failed ({type(e).__name__})")
            return
        try:
            run_collection_step(self, deku, ta, self.state, overrides, self.deps.now())
        except Exception as e:  # noqa: BLE001 - never let the step take the run down
            self.issue(f"Deku collection step aborted by an unexpected error: {_err(e)}")
        try:
            save_state(cfg.state, self.state)
        except Exception as e:  # noqa: BLE001
            self.issue(f"state not saved after the Deku collection step ({type(e).__name__})")

    # ---- 1. feed

    def _load_feed(self) -> list[XboxItem]:
        items = load_feed(self.cfg.feed, max_age=self.cfg.feed_max_age, last_count=None)
        last, accept = self.state.last_feed_count, self.cfg.accept_feed_count
        if accept is not None and len(items) == accept:
            if last is not None:
                self.say(f"feed: accepting {len(items)} items (last run had {last}) "
                         "because WSYNC_ACCEPT_FEED_COUNT / --accept-feed-count says so")
        else:
            check_count(len(items), last)
        self.feed_count = len(items)  # recorded as the new baseline only after a good run
        return items

    # ---- 2. reads

    def _read(self, lst: _List, *, with_platforms: bool = False) -> None:
        if not lst.enabled:
            self.say(f"{lst.name}: not synced this run (--only)")
            return
        if lst.failed or lst.client is None:
            return
        try:
            if with_platforms:
                lst.current = lst.client.wishlist(with_platforms=True)
            else:
                lst.current = lst.client.wishlist()
        except Exception as e:  # noqa: BLE001 - any read failure: no writes on this list
            lst.failed = "wishlist read failed"
            self.issue(f"{lst.label} wishlist read failed, no {lst.label} writes this run: {_err(e)}")
            return
        self.say(f"{lst.name}: read {len(lst.current)} wishlist entries")
        if lst.name == "deku":
            self.deku_items.update(getattr(lst.client, "last_items", {}) or {})
            if not lst.current:
                lst.pause.append("the Deku Deals wishlist read as empty")
                self.issue("Deku Deals wishlist read as empty (0 entries): no Deku removals this run")

    # ---- 3. verdicts

    def _verdicts(self, items: list[XboxItem], fetch) -> tuple[dict[str, str | None], bool]:
        now = self.deps.now()
        try:
            if fetch is None:
                raise RuntimeError("no TrueAchievements client for the D2D lists")
            verdicts = self.deps.refresh_verdicts(items, self.state, fetch, self.deps.http_get, now,
                                                  pause=self.deps.d2d_pause)
        except Exception as e:  # noqa: BLE001
            self.issue(f"Disc to Digital refresh failed ({_err(e)}): using cached verdicts and "
                       "making no Deku format changes this run")
            return {i.product_id: (self.state.d2d.get(i.product_id) or {}).get("verdict") for i in items}, False
        pending = 0
        for i in items:
            if is_dlc(i):
                continue
            entry = self.state.d2d.get(i.product_id)
            try:
                checked = datetime.fromisoformat(entry["checked"])
                checked = checked if checked.tzinfo else checked.replace(tzinfo=timezone.utc)
                fresh = "xct" in entry and now - checked < d2d.XCT_TTL
            except (TypeError, KeyError, ValueError):
                fresh = False
            pending += not fresh
        counts: dict[str, int] = {}
        for v in verdicts.values():
            counts[str(v)] = counts.get(str(v), 0) + 1
        self.say(f"d2d: verdicts {dict(sorted(counts.items()))}; XCT lookups pending (no fresh answer "
                 f"yet, retried on later runs): {pending}")
        return verdicts, True

    # ---- 4. matching

    def _over_budget(self) -> bool:
        return self.deps.monotonic() - self.start > self.cfg.match_budget

    def _match(self, lst: _List, resolve) -> None:
        for item in lst.items:
            pid = item.product_id
            if lst.failed or lst.stop:
                lst.matches[pid] = transient(lst.stop or "list failed")
                continue
            try:
                m = self._resolve(lst, resolve, item)
                lst.errors = 0
            except _Deferred:
                lst.deferred += 1
                m = transient("deferred to the next run (lookup time budget)")
            except (DekuAuthError, TAAuthError) as e:
                lst.failed = "login rejected during matching"
                self.issue(f"{lst.label}: {lst.failed} ({type(e).__name__}); no {lst.label} writes this run")
                m = transient(lst.failed)
            except Exception as e:  # noqa: BLE001 - never let a failed lookup read as "none"
                # A per-item failure is a warning: the item is ambiguous (removals pause) and is
                # retried next run; it isn't a run failure and opens no task.
                lst.errors += 1
                self.say(f"{lst.label}: lookup failed for {item.title!r} ({_err(e)}); "
                         "removals paused, retried next run", logging.WARNING)
                m = transient(f"lookup failed ({type(e).__name__})")
                if lst.errors >= MAX_LOOKUP_ERRORS:
                    lst.stop = "lookup skipped after repeated errors"
                    self.say(f"{lst.label}: {MAX_LOOKUP_ERRORS} lookups failed in a row; skipping the rest "
                             "this run", logging.WARNING)
            lst.matches[pid] = m
            self.say(f"{lst.name} match: {item.title} -> {m.status}"
                     + (f" {m.target}" if m.target else "")
                     + (f" [{'; '.join(m.candidates)}]" if m.status != "matched" and m.candidates else ""))
        if lst.deferred:
            self.say(f"{lst.name}: {lst.deferred} lookups deferred to the next run (time budget)")
        self._related_to_stale(lst)

    def _resolve(self, lst: _List, resolve, item: XboxItem) -> Match:
        """One lookup, retried once after a failure (FlareSolverr challenges time out at random)."""
        for attempt in (1, 2):
            try:
                return resolve(item, not self._over_budget())
            except (_Deferred, DekuAuthError, TAAuthError):
                raise
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise
                self.say(f"{lst.name}: lookup for {item.title!r} failed ({type(e).__name__}); retrying once",
                         logging.WARNING)
        raise AssertionError("unreachable")

    def _related_to_stale(self, lst: _List) -> None:
        """An unmatched item whose title is related to an entry we'd remove may own that entry."""
        desired = {m.target for m in lst.matches.values() if m.status == "matched"}
        stale = [e for t, e in (lst.current or {}).items() if t not in desired]
        for item in lst.items:
            m = lst.matches.get(item.product_id)
            if m is None or m.status != "none":
                continue
            clash = sorted({e.title for e in stale if related(item.title, e.title)})
            if clash:
                lst.matches[item.product_id] = Match("ambiguous", None, clash)
                self.say(f"{lst.name} match: {item.title} -> ambiguous (related to a listed entry: "
                         f"{'; '.join(clash)})")

    # Deku

    def _deku_item(self, slug: str) -> DekuItem:
        if slug not in self.deku_items:
            it = self._client("deku").item(slug)
            # A renamed item answers under its current slug: keep it under both.
            self.deku_items[slug] = it
            self.deku_items.setdefault(it.slug, it)
        return self.deku_items[slug]

    def _client(self, name: str):
        return self._clients[name]

    def _deku_valid(self, item: XboxItem, it: DekuItem) -> bool:
        return bool(it.offered & XBOX_PLATFORMS) and pick(item.title, [(it.title, it.slug)]).status == "matched"

    def _deku_resolve(self, item: XboxItem, allow_search: bool) -> Match:
        pid = item.product_id
        override = self.overrides.get(pid, {}).get("deku")
        if override:
            it = self._deku_item(override)
            return Match("matched", it.slug, [it.title])
        cached = self.state.mapping.get(pid) or {}
        slug = cached.get("deku_slug")
        if slug and cached.get("deku_key") == base_title(item.title):
            try:
                it = self._deku_item(slug)
                if self._deku_valid(item, it):
                    if it.slug != slug:  # renamed on Deku Deals: cache the current slug
                        cached["deku_slug"] = it.slug
                    return Match("matched", it.slug, [it.title])
                self.say(f"deku: cached page for {item.title} no longer matches; searching again")
            except (DekuAuthError, _Deferred):
                raise
            except Exception as e:  # noqa: BLE001 - a gone page: search again
                self.say(f"deku: cached page for {item.title} unreadable ({type(e).__name__}); searching again")
        if not allow_search:
            raise _Deferred
        deku = self._client("deku")
        results = deku.search(_query(item.title))
        bare = strip_edition_noise(item.title)
        if not exact_hits(item.title, results) and norm(bare) != norm(item.title):
            seen = {s for _, s in results}
            results += [(t, s) for t, s in deku.search(_query(bare)) if s not in seen]
        hits = exact_hits(item.title, results)
        if not hits:
            near = [t for t, _ in results if related(item.title, t)]
            return Match("ambiguous", None, near) if near else Match("none")
        if len(hits) > MAX_PAGES:
            return Match("ambiguous", None, [t for t, _ in hits])
        xbox = []
        for title, slug in hits:
            it = self._deku_item(slug)
            if it.offered & XBOX_PLATFORMS:
                xbox.append(it)
        if not xbox and item.kind != "released":
            # Not out yet: Deku lists the Xbox version when it has one. Like "no page yet" --
            # no task, no hold on removals (an entry it might own is still protected by
            # _related_to_stale); retried each run.
            return Match("none", None, [NO_XBOX_YET])
        if len(xbox) != 1:
            why = "" if xbox else " (no Xbox platform on Deku Deals)"
            return Match("ambiguous", None, [f"{t}{why}" for t, _ in hits])
        it = xbox[0]
        self.state.mapping.setdefault(pid, {}).update(
            deku_slug=it.slug, deku=it.uuid, deku_key=base_title(item.title))
        return Match("matched", it.slug, [it.title])

    # TrueAchievements

    def _ta_game_id(self, pid: str, slug: str) -> tuple[int, str]:
        """(game id, title) for an override slug; positive ids and the title are cached in state.

        A placeholder (pre-release, id < 1) is never cached: its id changes on release.
        """
        ta = self._client("ta")
        cached = (self.state.mapping.get(pid) or {}).get("ta_override") or {}
        if (ta.game_ids.get(slug, 0) > 0 and cached.get("slug") == slug and cached.get("title")):
            return ta.game_ids[slug], cached["title"]
        page = ta.game(slug)
        if page.id > 0:
            ta.game_ids[slug] = page.id
            self.state.mapping.setdefault(pid, {})["ta_override"] = {"slug": slug, "title": page.title}
        return page.id, page.title

    def _ta_game(self, title: str) -> tuple[Match, TAGame | None]:
        ta = self._client("ta")
        first = ta.search(_query(title))
        results, complete = list(first), getattr(first, "complete", True)
        bare = strip_edition_noise(title)
        if not exact_hits(title, results) and norm(bare) != norm(title):
            more = ta.search(_query(bare))
            complete = complete and getattr(more, "complete", True)
            seen = {s for _, s in results}
            results += [(t, s) for t, s in more if s not in seen]
        if complete and not exact_hits(title, results):
            for part in _subtitles(title):
                more = ta.search(part)
                complete = complete and getattr(more, "complete", True)
                seen = {s for _, s in results}
                results += [(t, s) for t, s in more if s not in seen]
                if exact_hits(title, results):
                    break
        if not complete:
            # More result pages than we read: the page we want may be on one we didn't.
            return Match("ambiguous", None, [f"TrueAchievements search results truncated for {title!r}"]), None
        hits = exact_hits(title, results)
        if not hits:
            near = [t for t, _ in results if related(title, t)]
            return (Match("ambiguous", None, near) if near else Match("none")), None
        if len(hits) > MAX_PAGES:
            return Match("ambiguous", None, [t for t, _ in hits]), None
        fetched = [ta.game(slug) for _, slug in hits]
        pages = [p for p in fetched if p.id > 0]
        for p in pages:
            ta.game_ids[p.slug] = p.id
        if not pages:
            # Only pre-release placeholder pages (negative ids, not writable): no page yet.
            self.say(f"ta: {title} has only a pre-release placeholder page on TrueAchievements; retried each run")
            return Match("none", None, [PLACEHOLDER]), None
        chosen = choose_ta_page(pages)
        described = [f"{p.title} [{p.platform or 'no platform'}]" for p in pages]
        if chosen is None:
            return Match("ambiguous", None, described), None
        twins = [p for p in pages if p.platform == chosen.platform and not excluded_page(p) and p.id != chosen.id]
        if twins:
            return Match("ambiguous", None, described), None
        return Match("matched", str(chosen.id), [chosen.title]), chosen

    def _ta_pack(self, item: XboxItem, parent: str, game: TAGame) -> Match:
        packs = [(t, str(i)) for t, i in self._client("ta").dlc(game.slug)]
        if not packs:
            return Match("none")
        m = pick(item.title, packs)
        if m.status != "matched" and norm(item.title).startswith(norm(parent)):
            rest = item.title[len(parent):].strip(" -:–—") if item.title.startswith(parent) else ""
            if rest:
                m = pick(rest, packs)
        if m.status == "matched":
            return Match("matched", _ta_key(game.id, int(m.target)), m.candidates)
        return Match("ambiguous", None, [f"{game.title} DLC: {t}" for t, _ in packs])

    def _ta_dlc(self, item: XboxItem) -> tuple[Match, TAGame | None]:
        for parent in parent_titles(item.title):
            pm, game = self._ta_game(parent)
            if pm.status == "none":
                continue
            if pm.status != "matched":
                return Match("ambiguous", None, [f"parent game: {c}" for c in pm.candidates] or ["parent game unclear"]), None
            m = self._ta_pack(item, parent, game)
            return m, (game if m.status == "matched" else None)
        return Match("none"), None

    def _ta_override(self, item: XboxItem, ov: dict) -> Match:
        slug, dlc = ov.get("ta"), ov.get("ta_dlc")
        if slug:
            game_id, title = self._ta_game_id(item.product_id, slug)
            if game_id < 1:
                self.say(f"ta: override page for {item.title} is a pre-release placeholder; retried each run")
                return transient(f"override page {title} is a pre-release placeholder")
            if dlc:
                return Match("matched", _ta_key(game_id, dlc), [title])
            if is_dlc(item):
                game = TAGame(game_id, title, "", slug)
                return self._ta_pack(item, title, game)
            return Match("matched", _ta_key(game_id), [title])
        for parent in parent_titles(item.title):  # ta_dlc alone: find the parent game
            pm, game = self._ta_game(parent)
            if pm.status == "matched":
                return Match("matched", _ta_key(game.id, dlc), [game.title])
            if pm.status == "ambiguous":
                return Match("ambiguous", None, [f"parent game: {c}" for c in pm.candidates])
        return Match("ambiguous", None, ["ta_dlc override, but the parent game wasn't found"])

    def _ta_resolve(self, item: XboxItem, allow_search: bool) -> Match:
        pid = item.product_id
        ov = self.overrides.get(pid, {})
        if ov.get("ta") or ov.get("ta_dlc"):
            return self._ta_override(item, ov)
        cached = self.state.mapping.get(pid) or {}
        ta_map = cached.get("ta")
        if (isinstance(ta_map, dict) and cached.get("ta_key") == base_title(item.title)
                and isinstance(ta_map.get("game"), int) and isinstance(ta_map.get("dlc", 0), int)):
            return Match("matched", _ta_key(ta_map["game"], ta_map.get("dlc", 0)),
                         [cached.get("ta_title") or item.title])
        if not allow_search:
            raise _Deferred
        m, game = self._ta_dlc(item) if is_dlc(item) else self._ta_game(item.title)
        if m.status == "matched" and game is not None:
            gid, dlc = _ta_ids(m.target)
            self.state.mapping.setdefault(pid, {}).update(
                ta={"game": gid, "dlc": dlc, "slug": game.slug}, ta_key=base_title(item.title),
                ta_title=m.candidates[0] if m.candidates else game.title)
        return m

    # ---- 5. plan

    def _plan(self, lists: dict[str, _List], verdicts, d2d_ok: bool) -> Plan:
        deku, ta = lists["deku"], lists["ta"]
        platforms = {s: it.offered for s, it in self.deku_items.items()}
        dp = build_plan(deku.items if deku.active else [], deku.matches, {}, verdicts,
                        deku.current if deku.active else {}, {}, platforms)
        tp = build_plan(ta.items if ta.active else [], {}, ta.matches, {}, {},
                        ta.current if ta.active else {}, {})
        plan = Plan(deku=dp.deku, ta=tp.ta)
        # A TA placeholder page is "no page yet", like an unannounced game: no task, nothing to fix.
        placeholder = {pid for pid, m in ta.matches.items() if m.status == "none" and m.candidates == [PLACEHOLDER]}
        # Likewise an unreleased game whose Deku page has no Xbox listing yet.
        no_xbox_yet = {pid for pid, m in deku.matches.items() if m.status == "none" and m.candidates == [NO_XBOX_YET]}
        plan.problems = ([p for p in dp.problems if p.kind == "d2d-disputed"
                          or (p.kind == "unmatched-deku" and p.product_id not in no_xbox_yet)]
                         + [p for p in tp.problems if p.kind == "unmatched-ta" and p.product_id not in placeholder])
        # Transient lookup gaps stay ambiguous (removals pause) but open no "add an override" task;
        # an existing task for the item is kept open, since nothing was checked.
        self.keep_keys = {f"{pid}:{KIND[name]}" for name, lst in lists.items()
                          for pid, m in lst.matches.items() if is_transient(m)}
        plan.problems = [p for p in plan.problems if f"{p.product_id}:{p.kind}" not in self.keep_keys]
        unmatched = {p.product_id for p in plan.problems if p.kind != "d2d-disputed"}
        for lst in (deku, ta):
            if not lst.active:
                continue
            for item in lst.items:
                m = lst.matches.get(item.product_id)
                if ((item.kind == "unannounced" or item.product_id in placeholder or item.product_id in no_xbox_yet)
                        and m is not None and m.status == "none"
                        and item.product_id not in unmatched and item.title not in plan.skipped_unannounced):
                    plan.skipped_unannounced.append(item.title)

        # Deku entries hidden from the share page can't be added and make our read incomplete.
        if deku.active:
            keep = []
            for e in plan.deku.adds:
                it = self.deku_items.get(e.target)
                if it is not None and it.on_wishlist:
                    # Not a run failure (ruling 3): a problem task, and no Deku removals this run.
                    self.say(f"deku: {e.title} ({e.target}) is on the wishlist but hidden from the share "
                             "page; not added, no Deku removals this run", logging.WARNING)
                    for item in deku.items:
                        m = deku.matches.get(item.product_id)
                        if m is not None and m.status == "matched" and m.target == e.target:
                            plan.problems.append(Problem(item.product_id, item.title, "unmatched-deku",
                                                         HIDDEN_DETAIL))
                    deku.pause.append("an entry is hidden from the share page")
                else:
                    keep.append(e)
            plan.deku.adds = keep
            if not d2d_ok and plan.deku.updates:
                self.say(f"deku: {len(plan.deku.updates)} format/platform updates held (no D2D verdicts)")
                plan.deku.updates = []
        for lst, lp in ((deku, plan.deku), (ta, plan.ta)):
            for reason in lst.pause:
                _pause(lp, reason)
        return plan

    def _log_plan(self, plan: Plan) -> None:
        self.say(f"plan: {report.summary(plan, True) or 'no changes'}; problems: {len(plan.problems)}; "
                 f"paused removals: Deku {len(plan.deku.paused_removes)}, TA {len(plan.ta.paused_removes)}")
        for p in plan.problems:
            self.say(f"problem: {p.kind}: {p.title} ({p.product_id}): {p.detail}")
        if self.cfg.dry_run:
            for line in report.plan_review_body(plan).splitlines():
                self.say(f"plan | {line}")

    # ---- 6. apply

    def _apply_deku(self, lst: _List, lp: ListPlan, done: ListPlan) -> None:
        deku = lst.client
        block = False
        try:
            for n, e in enumerate(lp.adds):
                if self._too_late("deku", len(lp.adds) - n + len(lp.updates) + len(lp.removes)):
                    return
                try:
                    it = deku.item(e.target)
                    if it.on_wishlist:
                        self.say(f"deku: {e.title} ({e.target}) is already on the wishlist but hidden from the "
                                 "share page; not added, no Deku removals this run", logging.WARNING)
                        block = True
                        continue
                    plats = e.platforms or (it.offered & XBOX_PLATFORMS)
                    deku.add(it, e.format, plats)
                    done.adds.append(e)
                    self.say(f"deku add: {e.title} ({e.target}) format={e.format} platforms={_fmt_plats(plats)}")
                except DekuAuthError:
                    raise
                except Exception as ex:  # noqa: BLE001
                    block = True
                    self.issue(f"Deku Deals add failed: {e.title} ({e.target}): {_err(ex)}")
            for n, (cur, want) in enumerate(lp.updates):
                if self._too_late("deku", len(lp.updates) - n + len(lp.removes)):
                    return
                try:
                    it = deku.item(want.target)
                    if not it.on_wishlist:
                        block = True
                        self.issue(f"Deku Deals update skipped: {want.title} ({want.target}) is no longer on the wishlist")
                        continue
                    deku.update(it, want.format, want.platforms)
                    done.updates.append((cur, want))
                    self.say(f"deku update: {want.title} ({want.target}) format {cur.format or 'unset'} -> "
                             f"{want.format}; platforms {_fmt_plats(cur.platforms)} -> {_fmt_plats(want.platforms)}")
                except DekuAuthError:
                    raise
                except Exception as ex:  # noqa: BLE001
                    block = True
                    self.issue(f"Deku Deals update failed: {want.title} ({want.target}): {_err(ex)}")
            if block:
                if lp.removes:
                    self.say(f"deku: {len(lp.removes)} removals skipped this run after a failed write")
                return
            for n, e in enumerate(lp.removes):
                if self._too_late("deku", len(lp.removes) - n):
                    break
                try:
                    it = deku.item(e.target)
                    if not it.on_wishlist:
                        self.say(f"deku remove: {e.title} ({e.target}) already gone")
                        continue
                    deku.remove(it)
                    done.removes.append(e)
                    self.say(f"deku remove: {e.title} ({e.target})")
                except DekuAuthError:
                    raise
                except Exception as ex:  # noqa: BLE001 - stop the sweep at the first surprise
                    self.issue(f"Deku Deals remove failed: {e.title} ({e.target}): {_err(ex)}; "
                               "remaining Deku removals skipped")
                    return
        except DekuAuthError as ex:
            self.issue(f"Deku Deals login rejected while writing ({type(ex).__name__}); Deku writes stopped")

    def _apply_ta(self, lst: _List, lp: ListPlan, done: ListPlan) -> None:
        ta = lst.client
        block = False
        stopped = False
        for n, e in enumerate(lp.adds):
            if self._too_late("ta", len(lp.adds) - n + len(lp.removes)):
                stopped = True
                break
            try:
                ta.add(*_ta_ids(e.target))
                done.adds.append(e)
                self.say(f"ta add: {e.title} ({e.target})")
            except AlreadyListed:
                self.issue(f"TrueAchievements says {e.title} ({e.target}) is already on the wishlist: the "
                           "wishlist read looks incomplete; TA writes stopped, no TA removals this run")
                stopped = True
                break
            except TAAuthError as ex:
                self.issue(f"TrueAchievements login rejected while writing ({type(ex).__name__}); TA writes stopped")
                stopped = True
                break
            except Exception as ex:  # noqa: BLE001
                block = True
                self.issue(f"TrueAchievements add failed: {e.title} ({e.target}): {_err(ex)}")
        if not stopped and block and lp.removes:
            self.say(f"ta: {len(lp.removes)} removals skipped this run after a failed write")
        elif not stopped:
            for n, e in enumerate(lp.removes):
                if self._too_late("ta", len(lp.removes) - n):
                    break
                try:
                    ta.remove(*_ta_ids(e.target))
                    done.removes.append(e)
                    self.say(f"ta remove: {e.title} ({e.target})")
                except Exception as ex:  # noqa: BLE001 - stop the sweep at the first surprise
                    self.issue(f"TrueAchievements remove failed: {e.title} ({e.target}): {_err(ex)}; "
                               "remaining TA removals skipped")
                    break
        if done.adds or done.removes:
            self._verify_ta(ta, done)

    def _too_late(self, name: str, remaining: int) -> bool:
        """No write starts after the write deadline; the rest wait for the next run."""
        if self.deps.monotonic() - self.start <= self.cfg.write_deadline:
            return False
        self.late_writes += remaining
        self.say(f"{name}: {remaining} writes left for the next run (past the "
                 f"{self.cfg.write_deadline / 60:.0f}-minute write deadline)", logging.WARNING)
        return True

    def _verify_ta(self, ta, done: ListPlan) -> None:
        try:
            after = ta.wishlist()
        except Exception as e:  # noqa: BLE001
            self.issue(f"TrueAchievements verification re-read failed: {_err(e)}")
            return
        missing = [e.title for e in done.adds if e.target not in after]
        lingering = [e.title for e in done.removes if e.target in after]
        if missing or lingering:
            self.issue("TrueAchievements verification failed after writing: "
                       + "; ".join(([f"not added: {', '.join(missing)}"] if missing else [])
                                   + ([f"still listed: {', '.join(lingering)}"] if lingering else [])))
        else:
            self.say(f"ta: verified {len(done.adds)} adds and {len(done.removes)} removes on re-read")

    # ---- 7. state and reporting

    def _save_state(self, items: list[XboxItem], lists: dict[str, _List], ta_client) -> None:
        pids = {i.product_id for i in items}
        self.state.mapping = {pid: m for pid, m in self.state.mapping.items() if pid in pids and m}
        if ta_client is not None:
            ids = getattr(ta_client, "game_ids", {}) or {}
            self.state.ta_game_ids = {k: v for k, v in ids.items() if isinstance(v, int) and v > 0}
        # A good run (a list read and synced, no list read failed) sets the shrink-guard baseline.
        enabled = [lst for lst in lists.values() if lst.enabled]
        if any(lst.active for lst in enabled) and all(lst.current is not None for lst in enabled):
            self.state.last_feed_count = self.feed_count
        else:
            self.say("feed count not recorded: no complete read of the lists this run")
        try:
            save_state(self.cfg.state, self.state)
        except Exception as e:  # noqa: BLE001
            self.issue(f"state not saved ({type(e).__name__})")

    def _report(self, plan: Plan, applied: Plan, keep: set[str]) -> None:
        try:
            self.reporter.sync_problems(plan.problems, keep, self.keep_keys)
        except Exception as e:  # noqa: BLE001
            self.issue(f"Vikunja problem tasks not updated ({type(e).__name__})")
        changed = False
        try:
            changed = self.reporter.plan_review(plan, dry_run=self.cfg.dry_run)
        except Exception as e:  # noqa: BLE001
            self.issue(f"Vikunja plan review task not updated ({type(e).__name__})")
        if self.cfg.dry_run:
            if changed and not _is_empty(plan):
                text = report.summary(plan, False) or "Dry run: no adds, removes or format changes"
                paused = len(plan.deku.paused_removes) + len(plan.ta.paused_removes)
                if paused:
                    text += f"; {paused} removals paused"
                self.notify("Wishlist sync: dry-run plan ready",
                            f"{text}\nReview the plan in Vikunja (Wishlist Sync).")
        else:
            text = report.summary(applied, True)
            if self.late_writes:
                text = (f"{text}; " if text else "") + (
                    f"{self.late_writes} writes left for the next run (time limit)")
            if text:
                self.notify("Wishlist sync", text)


def run(cfg: Config, deps: Deps | None = None) -> int:
    """One sync run; returns the process exit code (0 ok, 1 any failure)."""
    return _Run(cfg, deps or Deps()).go()

# xbox-wishlist-sync

An hourly Kubernetes CronJob that mirrors an Xbox wishlist, one way, onto a Deku Deals wishlist
and a TrueAchievements wishlist. The Xbox wishlist is never written. See `docs/design.md` for the
design, and `docs/ta-protocol.md` / `docs/deku-protocol.md` for how each site is driven.

The Xbox wishlist arrives as a JSON feed file (a list of records with `productId`, `title`,
`productKind`, `platforms` and so on). The run aborts without writing anything if the feed is
missing, stale, empty or has shrunk sharply since the last run.

## Running

```
python -m wsync [--only deku|ta] [--dry-run | --apply] [--no-report] [--accept-feed-count N]
```

- `--dry-run` / `--apply` override `DRY_RUN`. With neither, `DRY_RUN` decides, and anything but
  `false`/`0`/`no`/`off` (including unset) means a dry run.
- `--only deku|ta` syncs one list. Problem tasks for the other list are left as they are.
- `--no-report` sends nothing to Vikunja or Apprise; the plan is only logged. Use it for local
  runs.
- Exit code 0 means a clean run; 1 means a list-level failure (feed, state or overrides
  rejected, a wishlist read, a login, a write, or a post-write check failed); 2 is a bad command
  line or environment value. A failed lookup for a single game is only a warning: the game counts
  as ambiguous for that run (removals pause) and is retried next run.
- Apprise gets a failure ping only when the set of failures differs from the last run's (numbers
  in the messages are ignored), so a persistent problem pings once, not hourly. A clean run
  resets it.
- No write (add, update or removal) starts more than 45 minutes into a run (the job is killed at
  50); the rest wait for the next run and the summary says how many.
- SIGTERM (the job's deadline, or the pod being stopped) ends the run at once: no further writes,
  the state is saved with what the run learned, and the exit code is 1. The next run reconciles
  any write that was cut short.

A run: load state, the feed and the overrides (all must be sound before any login) → read both
wishlists → refresh Disc to Digital verdicts → match every Xbox item on each list (override, then
the cached mapping, then search) → plan → apply adds, then updates, then removes → save state →
report.

## Configuration

| Variable | Purpose |
|----------|---------|
| `WSYNC_FEED` | Feed file (default `/feed/wishlist.json`) |
| `WSYNC_STATE` | State file (default `/state/state.json`). The Deku and TA session files (`deku-session`, `ta-session`, mode 600) live next to it. |
| `WSYNC_OVERRIDES` | Manual match overrides, JSON (default `/config/overrides.json`) |
| `WSYNC_FEED_MAX_AGE_HOURS` | Oldest feed accepted, by file mtime (default 6) |
| `WSYNC_ACCEPT_FEED_COUNT` | Shrink-guard recovery, see below (same as `--accept-feed-count`) |
| `DEKU_EMAIL`, `DEKU_PASSWORD`, `DEKU_WISHLIST_ID` | Deku Deals account |
| `TA_GAMERTAG`, `TA_PASSWORD`, `FLARESOLVERR_URL` | TrueAchievements account, and a FlareSolverr v3 `/v1` endpoint used only for Cloudflare clearances (see below) |
| `VIKUNJA_URL`, `VIKUNJA_TOKEN` | Task reporting (project "Wishlist Sync") |
| `APPRISE_URL` | Notifications |
| `DRY_RUN` | `true` (default) plans only; `false` applies changes |

Credentials come only from the environment. They are never logged, put in a notification or task,
or passed on a command line; every log line and report is also scrubbed of the configured secret
values.

TrueAchievements traffic: every request that carries the password or a session cookie (the login,
every page read, every wishlist write) goes straight to TrueAchievements through `curl_cffi` with a
Chrome TLS fingerprint. FlareSolverr logs the cookies and POST bodies it is sent, so it only ever
gets an anonymous, cookie-less GET of TrueAchievements' home page, and only when a direct request
meets a Cloudflare challenge; its `cf_clearance` cookie and user agent then go with the direct
requests. A request still challenged after one fresh clearance fails.

## DRY_RUN warning

`DRY_RUN` defaults to `true`: a dry run writes nothing to Deku Deals or TrueAchievements. It logs
the full plan, keeps one Vikunja task ("Review the wishlist sync plan") up to date with it, and
pings Apprise when the plan changes. Setting `DRY_RUN=false` makes the job add, remove and edit
entries on both sites so that they match the Xbox wishlist: anything on either list that isn't on
the Xbox wishlist is removed. Review a dry-run plan before switching it off.

Removals on a list are paused (and listed as such in the plan) while any Xbox item on that list is
ambiguous: candidate pages were found but none is a confident match, the TrueAchievements search
had more result pages than are read (4 × 25), a lookup failed or was deferred, or the item's title
is related to an entry that would be removed. A search result is a *candidate* when its first two
significant words agree with the item's (the first word, when either title has only one; leading
"the"/"a"/"an" skipped; numbers compared by value, so "40,000" = "40K" and "VII" = "7"). So
"Persona 4 Revival" vs "Persona 4 Golden" pauses removals, "Persona 6" vs "Persona 5 Royal" doesn't.
A publisher prefix defeats the rule ("Tom Clancy's The Division 2" vs "The Division 2"); use an
override for such games. Ambiguous items get a Vikunja task (resolve it with an override), except
failed or deferred lookups, which keep any existing task and open none. The plan names ambiguous
titles but only counts failed or deferred lookups ("2 lookups retried next run"), so which lookup
flaked this hour doesn't change the dry-run review task or send a "plan ready" ping. A Deku entry that is on
the wishlist but hidden from the share page gets a task and pauses Deku removals for the run.

Lookups are retried once after a failure (a Cloudflare challenge or a slow FlareSolverr clearance
can fail at random); Deku Deals GETs back off and retry on HTTP 429. TrueAchievements gives unreleased games a
negative placeholder id that can't be wishlisted yet: such games are listed with the "no page
yet" skips and retried every run.

## Overrides

A JSON object keyed by Xbox `productId` (shown in each Vikunja problem task), mounted from a
ConfigMap:

```json
{
  "9NB0ZZ452WF1": {"deku": "fable", "ta": "fable"},
  "9P8LNZ7X5GVP": {"deku": "skip", "ta": "skip", "note": "unannounced"},
  "9ABCDEF12345": {"ta": "elden-ring", "ta_dlc": 1234}
}
```

- `deku`: a Deku Deals item slug (`/items/<slug>`), or `"skip"`.
- `ta`: a TrueAchievements game slug (`/game/<slug>`); for DLC, the parent game's slug. Or
  `"skip"`.
- `ta_dlc`: a TrueAchievements DLC id (the number in the DLC's wishlist button); forces that pack.
- `note`: free text, ignored.

Overrides beat automatic matching. `"skip"` means the item isn't mirrored to that list at all: no
lookup, no problem task, and if an entry for it is already on that list it is **removed** like any
other entry that isn't on the Xbox wishlist. A missing file means no overrides; an invalid file
fails the run before any login.

## Shrink-guard recovery

The run refuses a feed with fewer than 70% of the previous run's item count (a truncated feed
must never read as "remove everything"). If the wishlist really did shrink that much:

1. Check the feed and confirm the new count, e.g. 30.
2. Run once with `WSYNC_ACCEPT_FEED_COUNT=30` (or `--accept-feed-count 30` on a manual job). A feed
   of exactly that many items is accepted and recorded as the new baseline.
3. Remove the variable again. (Left in place it only matters when the feed has exactly that
   count.)

## State

`/state/state.json` keeps the last feed count (recorded only after a run that read the lists), the
last failure's digest, the resolved productId → Deku slug / TA game (and
DLC) ids, TA game ids for DLC rows, the TrueAchievements Disc to Digital lists (24 h) and XCT.live
answers (7 days; at most 10 lookups a run, oldest attempt first). Deleting it is safe: the next run rebuilds the caches (slower, up to the 35-minute
lookup budget; lookups past it wait for the next run with removals paused).

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e .[test]
.venv/bin/pytest -q
```

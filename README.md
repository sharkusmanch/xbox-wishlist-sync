# xbox-wishlist-sync

An hourly Kubernetes CronJob that mirrors an Xbox wishlist, one way, onto a Deku Deals wishlist
and a TrueAchievements wishlist. The Xbox wishlist is never written. See `docs/design.md` for the
design, and `docs/ta-protocol.md` / `docs/deku-protocol.md` for how each site is driven.

A second step of the same run adds the Xbox games TrueAchievements lists as **Owned** to the Deku
Deals collection, so owned games stop showing up in Deku's deals (add-only; see
[Deku collection step](#deku-collection-step)).

The Xbox wishlist arrives as a JSON feed file (a list of records with `productId`, `title`,
`productKind`, `platforms` and so on). The run aborts without writing anything if the feed is
missing, stale, empty or has shrunk sharply since the last run.

## Running

```
python -m wsync [--only deku|ta|collection] [--dry-run | --apply] [--no-report] [--accept-feed-count N]
```

- `--dry-run` / `--apply` override `DRY_RUN`. With neither, `DRY_RUN` decides, and anything but
  `false`/`0`/`no`/`off` (including unset) means a dry run.
- `--only deku|ta` syncs one list (and skips the collection step). Problem tasks for the other
  list are left as they are. `--only collection` runs only the Deku collection step: no feed is
  read and no wishlist is touched (it needs both logins).
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
| `WSYNC_TA_READ_AFTER` | `HH:MM` (UTC): read the TA collection once a day, at the first run past this time (set it just after TA's daily purchase import); a read from before the last such time, e.g. after downtime, is redone at the next run. Unset: at most once every 24 h |
| `WSYNC_ACCEPT_FEED_COUNT` | Shrink-guard recovery, see below (same as `--accept-feed-count`) |
| `DEKU_EMAIL`, `DEKU_PASSWORD`, `DEKU_WISHLIST_ID` | Deku Deals account |
| `TA_GAMERTAG`, `TA_PASSWORD`, `FLARESOLVERR_URL` | TrueAchievements account, and a FlareSolverr v3 `/v1` endpoint used only for Cloudflare clearances (see below) |
| `VIKUNJA_URL`, `VIKUNJA_TOKEN` | Task reporting (project "Wishlist Sync") |
| `APPRISE_URL` | Notifications |
| `DRY_RUN` | `true` (default) plans only; `false` applies changes |
| `COLLECTION_DRY_RUN` | The collection step's own switch: `true` (default) plans only; `false` adds to the Deku collection. A dry run (`DRY_RUN` or `--dry-run`) is always dry here too |

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

The same file may carry a `"collection"` object for the collection step, keyed by
TrueAchievements game id (the number in the game's TA collection row, shown in the review task's
unmatched list):

```json
{
  "collection": {
    "12345": {"deku": "some-deku-slug"},
    "67890": {"deku": "skip", "note": "not on Deku"}
  }
}
```

`deku` is the Deku item slug to add or `"skip"`. The item must list an Xbox platform: TA's platform
is used when the item lists it, otherwise the other Xbox platform the item lists.
An override applies to the whole game (its Series and One rows). An invalid `collection` object
fails the collection step only; the wishlist step runs as usual.

## Deku collection step

Runs after the wishlist step has finished and saved its state; nothing in it can change or fail
the wishlist step's work. It doesn't use the feed, so it also runs when the wishlist step rejected
the feed or its overrides (an invalid `collection` overrides object stops only this step). Each
run:

1. reads the TrueAchievements Game Collection (once a day, at the first run past `WSYNC_TA_READ_AFTER`, or at most once every 24 h without it; cached in state) and the
   public Deku collection share page. A read that is not complete (rows ≠ the page's own total)
   means no adds that run and a failure; a failed TrueAchievements read is retried 6 h later (the
   runs in between repeat the same failure without a new read or ping). A fresh TrueAchievements
   read is not started with less than 10 minutes left before the write deadline (the step waits
   for the next run);
2. keeps the games TA marks **Owned** on Xbox Series X|S or Xbox One. Xbox 360, Windows, mobile
   and Switch rows are skipped without any Deku request, as are not-owned statuses (Game Pass, EA
   Play, "No longer have", ...). A game with Series and One rows is one game (the Series row);
3. skips games already in the Deku collection by title (on an Xbox entry or one with no platform);
4. looks up at most **25 games a run** (Series first, never-tried first, then the oldest try), with
   at least **4 s between Deku requests** (worst case about 180 Deku requests a run, i.e. about 12
   minutes: per game two searches, an item read and the add, a create and a details save each read
   back, plus the share-page reads): override slug, else a Deku search (the title, then the title
   without edition noise) and a confident match by the wishlist rules. The item page must list an
   Xbox platform. On the item page, an existing Xbox entry or one with no platform set means
   "already in the collection"; Switch / PlayStation entries are left alone and a separate Xbox
   entry is added beside them;
5. adds the entry (live) on TA's platform when the Deku item lists it, otherwise on the other
   Xbox platform the item lists (TA's platform is the console the game was played on: an Xbox
   One game played on a Series X shows Series X|S); format from TA's media (Disc → physical;
   Digital, Disc-to-Digital, Disc & Digital → digital; blank → unset), with full read-back;
6. caches each outcome per TA game id. A game with no confident match is retried after 7 days;
   unmatched games never open problem tasks. A `collection` override slug follows the same 7-day
   cooldown while it stays the same (a changed override is looked up on the next run). A game
   recorded as already there or added whose entry is not on the public share page (hidden from
   public view, or deleted by hand on Deku) is re-checked weekly; a hand-deleted entry is added
   again, since TrueAchievements is the source of truth (remove the game from the TA collection, or
   skip it with a `collection` override, to stop that).

Dry run (`COLLECTION_DRY_RUN` not `false`, or the whole run dry): no Deku collection writes. A
Vikunja task "Review the Deku collection plan" lists every game a live run would add (title, Deku
slug, format, platform), the unmatched games and the counts (already in the collection, skipped
platforms and overrides, same-game rows, unmatched, waiting for a lookup); it is updated in place
as each run looks up the next 25, and Apprise pings "Deku collection plan ready" only when its
would-add, unmatched or check-by-hand lists change (not when only the waiting count moves). Dry-run
matches are kept, so a later live run adds them without searching again. Live: that task is closed
and Apprise gets "+N Deku collection" when something was added.

Failures: a Deku 429 (after the client's own retries), a network error, or the 45-minute write
deadline (checked before each lookup and again just before each add) ends the step quietly for the
run (exit status unaffected; the rest waits for the next run). An incomplete read, a rejected login,
a failed lookup or add stops the step and fails the run (exit 1, with the usual deduplicated
failure ping). The step stops at the first such game every run, so **one game that keeps failing
blocks the step until the cause is fixed or the game is skipped with a `collection` override**
(`{"deku": "skip"}`).

Partial adds: before each add the step saves the Deku item (its final slug, after Deku's redirects)
in a list of unconfirmed adds, plus an `adding` record for the game. If the add fails after Deku
created the entry (or the run is killed in the middle), the item stays on that list and `own()` is
never called on it again, whichever TrueAchievements row, override, renamed slug or cached match
leads back to it. Each time a run reaches such an item (the game itself is re-checked first every
run) it re-reads the item page: once it shows an Xbox entry (or one with no platform), or the
public share page shows one for the item (checked at the start of every run, at no request cost),
the item leaves the list and the game is recorded as added (later runs then behave normally again,
including the weekly re-add of a hand-deleted entry); until then the run fails with "check <title>
on Deku Deals by hand" (pinged once) and the step stops there. The message says whether the item
page shows a new entry since the add (set it to Xbox by hand) or none (the create most likely left
nothing: add it by hand on Xbox if it is missing). A `collection` override can skip the game or
point it at another item, but the item itself stays on the list until its page (or the share page)
shows an Xbox entry; the dry-run review task lists every such item under "Check by hand".

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
answers (7 days; at most 10 lookups a run, oldest attempt first), and for the collection step the
last TrueAchievements collection read (until the next `WSYNC_TA_READ_AFTER` time, or 24 h) and each owned game's Deku outcome (`matched`,
`added`, `found` = dry-run match, `none` = retried after 7 days, `adding` / `partial` = an add
that may have left an entry) and the Deku items whose collection add is unconfirmed (never added
again until their page shows an Xbox entry). Deleting it is safe for the caches: the next run
rebuilds them (slower, up to the 35-minute lookup budget; lookups past it wait for the next run
with removals paused). It also forgets the unconfirmed adds, so check the "Check by hand" items
on Deku first.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e .[test]
.venv/bin/pytest -q
```

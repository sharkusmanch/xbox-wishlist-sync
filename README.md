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
python -m wsync [--only deku|ta|collection|library] [--dry-run | --apply] [--no-report] [--accept-feed-count N]
```

- `--dry-run` / `--apply` override `DRY_RUN`. With neither, `DRY_RUN` decides, and anything but
  `false`/`0`/`no`/`off` (including unset) means a dry run.
- `--only deku|ta` syncs one list (and skips the collection step). Problem tasks for the other
  list are left as they are. `--only collection` runs only the Deku collection step: no feed is
  read and no wishlist is touched (it needs both logins). `--only library` runs only the
  library step (below): no feed, no wishlist, no Deku (it needs the TrueAchievements login and the
  Microsoft token). `--only deku|ta|collection` skip the library step.
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
| `LIBRARY_DRY_RUN` | The library step's own switch: `true` (default) plans only; `false` writes to the TrueAchievements Game Collection. A dry run (`DRY_RUN` or `--dry-run`) is always dry here too |
| `WSYNC_LIBRARY_AFTER` | `HH:MM` (UTC, default `11:30`): the library step's daily pass starts at the first run past this time (before TA's own purchase import and the collection step's read) |
| `XBOX_REFRESH_TOKEN` | The Microsoft sign-in's **seed**: a refresh token used only when the token file has none that works, or when it is new (see "Microsoft sign-in") |
| `WSYNC_XBOX_TOKEN` | The rotating Microsoft refresh token's file (default `/state/xbox-token.json`, mode 600) |
| `WSYNC_LIBRARY_LOOKUPS` | The library step's TA page lookups per run, in games (default 10) |
| `WSYNC_LIBRARY_LOOKUPS_PER_DAY` | The same per UTC day, across the day's runs (default 40) |
| `WSYNC_LIBRARY_BUDGET` | The library step's TA writes per run, in games (default 25) |
| `COLLECTION_360_DRY_RUN` | Xbox 360 games in the collection step: `true` (default) lists them in their own review task; `false` adds them. A dry collection step is always dry here too |

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

The file may also carry a `"library"` object for the library step, keyed by Microsoft Store
product id (the `productId` shown in the step's Vikunja tasks):

```json
{
  "library": {
    "9AAAAAAAAAAA": {"ta": "Example-Game-Xbox-One", "note": "TA names it differently"},
    "9BBBBBBBBBBB": {"ta": "skip", "note": "not a game"}
  }
}
```

`ta` is required: the TrueAchievements game slug of the page to use, or `"skip"` to leave the game
out of the step. A slug is the game's page as it is: the title and generation rules are not applied
to it, and the lookup reads that page alone. A slug whose page does not exist on TA is a
check-by-hand item of its own kind, `override`, which always shows in the task (it is a mistake in
this file, never backfill noise). `note` is free text, ignored. An invalid `library` object stops
the library step only (an issue); the other steps run as usual.

## Library step: Microsoft to TrueAchievements

Keeps the TrueAchievements (TA) Game Collection in line with what Microsoft says the owner owns, so
that the TA collection (which the Deku collection step below mirrors onto Deku Deals) is complete:
Microsoft licences, orders and the Store catalog -> TA -> Deku. It adds owned games missing from
the collection and fixes entries with the wrong ownership; it never removes anything. Its own
failures never affect the wishlist or Deku collection steps.

**When.** One *pass* a day: the first run past `WSYNC_LIBRARY_AFTER` (UTC). A pass that has more to
do than one run allows (page lookups, writes) goes on over the following hourly runs until nothing
is left, and then it is complete for the day. A pass reads the TA collection **once**, in its first
run (it needs its own read; the Deku collection step's cache is not used), keeps the rows in state
for its later runs, and remembers which TA games it has already written so that re-planning never
repeats a write. Microsoft is read once a pass too: the first run signs in and reads the licences,
orders and catalog, and the later runs reuse the owned games it found (no sign-in). TA's Games with
Gold and EA Play lists are read at most weekly.

### Microsoft sign-in

The step signs in with a refresh token kept in a file on the state volume (`WSYNC_XBOX_TOKEN`,
mode 0600, written atomically), **rotated every run**: the new token is saved before any Xbox Live
call. `XBOX_REFRESH_TOKEN` is only a seed. It is tried when the file has no token or its token is
rejected, and when its SHA-256 differs from the one recorded with the file, so a re-seed in OpenBao
takes effect on the next run (after the secret reaches the pod) without deleting anything. Tokens,
the XUID and the gamertag are scrubbed from every log line and report.

When neither works, the step is **not** an error run. It posts the Vikunja task "Library sync:
Microsoft sign-in needed" (one Apprise ping per episode) and does not try again before the next
day's pass, unless `XBOX_REFRESH_TOKEN` has changed. Fix, in the games repo:

1. `scripts/xbox-library.py --token-file <tmp> login` (the temp file is created mode 0600), and
   approve the code at microsoft.com/link;
2. store the token from that file in OpenBao `secret/apps/tools/xbox-wishlist-sync` as
   `XBOX_REFRESH_TOKEN`;
3. delete the temp file (it holds a live refresh token plus the XUID and gamertag).

The task closes itself at the first working sign-in.

### Ownership rules

Each owned game (grouped by title, a Collections game never merged with an orders-only one) is
compared with the TA collection rows:

- **Adds.** A game with no TA entry gets its own TA page's entry: ownership **Owned** (**Games with
  Gold** for a free claim that is on TA's Games with Gold list), media **Digital**.
- **Fixes.** An entry that exists with a wrong ownership becomes **Owned** (media Digital when it
  has none): "No longer have" or "Renting" on any active licence. A subscription status (Game
  Pass, EA Play, Ubisoft+ ...) is only changed on **evidence**: a paid order or a claim. A licence
  alone, which a subscription also grants, is not enough, and the same holds for pre-2016 licences
  of EA Play games (they may be vault licences). Any other status (Loaned out, Looking to
  trade/sell, Ordered, Unable to play, Borrowing, Trial, or one the step doesn't know) is the
  owner's own: left alone, with no fix and no check-by-hand item.
- **Never.** Nothing is removed. An entry already Owned or Games with Gold is never changed, no
  other ownership is ever written on a fix, and an entry that has media (Disc, Disc to Digital ...)
  never has it changed.
- **Compare-and-set.** A write reads TA's current dropdown selection first: already the target ->
  nothing posted; neither what the plan saw nor the target -> nothing posted and the entry is left
  as it is (a warning, not an issue; the next pass plans from a fresh read).

Anything uncertain is a **check-by-hand** item, never a write: a near-miss title already in the
collection, an uncertain version or ownership, several non-Owned entries for one game, a game with
no TA page (`no-page`), an override whose page does not exist (`override`).

### Versions and generations

The TA page's own Platform field decides which TA game a purchase is; the Store catalog's console
generation only chooses among the pages that fit. A Collections licence, a ConsoleGen9 or Smart
Delivery purchase and a PC-only one never take an Xbox 360 / Xbox page or entry: ConsoleGen9 takes
the Xbox Series X|S page, ConsoleGen8 the Xbox One page (both for both; one page listing both is one
add), a PC-only licence the Windows page. Only an orders-only console game without ConsoleGen9 can
be a 360 or original Xbox game (backward compatible 360 products report ConsoleGen8 only); when
both a 360 and a modern page fit, it is a review. **Bundle twin rule:** an orders-only game whose
title key equals that of a Collections game (a bundled copy of a modern game) never writes an Xbox
360 / Xbox entry: a review instead. A console licence never fixes TA's separate "(Windows)" entry
(its console page is added instead); a PC-only licence fixes only a Windows entry.

### Page lookups

A missing game's TA page is found by guessing addresses from the title (platform phrases and
trailing tiers stripped; at most **6 candidates** per game; Windows too for a PC-only game), or the
`library` override's slug alone. At most **10 games a run** (`WSYNC_LIBRARY_LOOKUPS`) and **40 a
UTC day** across the day's runs (`WSYNC_LIBRARY_LOOKUPS_PER_DAY`) are looked up: games never looked
up first, then the expired re-lookups, the oldest first; **4 s** before every TA request, none past
the 45-minute write deadline. Results are cached per game, each lifetime with a stable offset per
product (from a hash of its first product id) so that games looked up together don't expire
together: found pages **30-44 days** for a product seen at go-live and **7-10 days** otherwise (every
product while the step is dry), no page **90-120 days**; a changed address set or override is looked
up again. **No page** means HTTP 404 or TA's page-not-found page (marker `PageNotFound404`).
What every address would meet stops the lookups quietly for the run, nothing cached, so a throttled
day never turns into months of "no page": network or Cloudflare trouble, TA unavailable or
throttling (HTTP 429, 502-504 or Cloudflare's 52x, a page with no game id such as a maintenance
page), a failed TA login. Anything else fails **that game's** lookup for the day's pass (another
HTTP error at one address, a game page that doesn't parse, an unexpected exception): an issue naming
the game and its product id (for a `library` override), and the next game goes on; the game is
looked up again in the next day's pass. After **3 failed lookups in a row** the pass looks nothing
more up until the next day's pass, so a TA-wide change is at most 3 issues a day.

### Writes

Live only (`LIBRARY_DRY_RUN` `false`). At most **25 games a run** (`WSYNC_LIBRARY_BUDGET`), 4 s
before each TA request, none started past the write deadline. Each write goes into state **before** its first request
(write-ahead, with what the plan saw), is checkpointed after each request, and removed after its
last; one left behind by a crash or stop is completed **at most once per pass** and never blocks
the reads, the plan or other writes. An add whose game TA already lists is never written
(`AlreadyListed`: a warning; TA's own import added it after the read). An entry changed on TA since
the read (`ChangedOnTA`) is left as it is (a warning). Any other failure of one game's write is an
issue naming that game and its product id (for a `skip` override), and the others go on; after
**3 consecutive write errors** the pass's writes stop until the next day's pass. Cloudflare or
network trouble and the write deadline stop the writes quietly. A failed write that reached nothing
on TA is dropped; a partial one stays for the next pass.

### Reporting

- **Dry run** (`LIBRARY_DRY_RUN` not `false`, or the whole run dry): no TA writes. The Vikunja task
  "Review the library sync plan" lists what a live run would add (title, TA slug, ownership and
  media), would fix, the games to check by hand, and the counts (already right, waiting for a
  lookup). It is updated every run; Apprise pings "Library sync plan ready" **once**, when the
  plan is complete (no lookup pending) and its lists changed.
- **Live:** that task closes. The task "Library sync: check by hand" holds the items needing a
  look, with the product id to key a `library` override. It holds **only new items**: at the first
  live run the step records a *seen baseline* (decision A) of every product in the plan's review
  items and in the games still waiting for a lookup, before any write, so the leftovers a dry run
  already showed never surface as new. `no-page` items never go in the task; an `override` item
  always does. Apprise pings when its item set changes (titles and a count, never product ids).
  "+N TA library" (the titles) is sent when a run added games.
- **Sign-in:** the task above, plus one "Library sync: Microsoft sign-in needed" ping per episode.

### Failures and back-offs

A failed Microsoft or TA read (a Microsoft or Store error, an incomplete TA read, data that cannot
be classified) is an issue (exit 1, with the usual deduplicated failure ping), and is **not
retried for 6 hours**: the runs in between make no new read and only log a warning (no issue, so
they don't fail the run or trip the stale-sync alert), as the Deku collection step's back-off does.
Nothing is planned or written from a partial read. A failed refresh of TA's Games with Gold / EA Play list
keeps the copy in state for up to a week (a warning). A sign-in that needs a new token is the task
above, not a failure. Reporting trouble (Vikunja) is an issue of its own and never breaks the step.

### TA traffic it adds

Every TA request is paced 4 s after the one before. A day's pass reads the Game Collection once
(about 17 pages) and, at most weekly, TA's Games with Gold (about 5 pages) and EA Play lists. Page
lookups: at most `WSYNC_LIBRARY_LOOKUPS` games a run (default 10) and `WSYNC_LIBRARY_LOOKUPS_PER_DAY`
a UTC day (default 40), at most 6 addresses each. Writes: at most `WSYNC_LIBRARY_BUDGET` games a run
(default 25), about **5 requests** each (an add: the game page, its postback, then per field an
EditOptions read and, when the field changes, an UpdateGame; a fix: the same per field). All three
take a whole number, 0 or more.

The first pass over the current library has about 200 games to look up (about 800 addresses): at 40
games a day it fills in over about 5 days, about 160 page requests a day (at most 240, and at most
60 in one run). The results then expire spread out by product rather than together: found pages
after 30-44 days (7-10 for products not seen at go-live, so every product while the step is dry), no
page after 90-120 days, all under the same daily cap. A steady day reads the collection once plus a
few re-lookups. TA withheld won/not-won data from this IP after heavy automated traffic
(2026-11-15), so keep these budgets.

## Deku collection step

Runs after the wishlist step has finished and saved its state; nothing in it can change or fail
the wishlist step's work. It doesn't use the feed, so it also runs when the wishlist step rejected
the feed or its overrides (an invalid `collection` overrides object stops only this step). Each
run:

1. reads the TrueAchievements Game Collection (once a day, at the first run past `WSYNC_TA_READ_AFTER`, or at most once every 24 h without it; cached in state) and the
   public Deku collection share page. A read that is not complete (rows ≠ the page's own total)
   means no adds that run and a failure; a failed TrueAchievements read is retried 6 h later (the
   runs in between make no new read and only log a warning: no issue, no ping). A fresh TrueAchievements
   read is not started with less than 10 minutes left before the write deadline (the step waits
   for the next run);
2. keeps the games TA marks **Owned** on Xbox Series X|S or Xbox One, and the Xbox 360 games it
   marks **Owned** or **Games with Gold** (see "Xbox 360 games" below). Windows, mobile and Switch
   rows are skipped without any Deku request, as are not-owned statuses (Game Pass, EA Play, "No
   longer have", ...). A game with Series and One rows is one game (the Series row), as are rows
   whose titles differ only by edition noise ("Foo" and "Foo Deluxe Edition"). An Xbox 360 row
   joins them only under the identical title (TA's trailing "(Xbox 360)" tag is ignored): a 360
   original and its Xbox One remaster ("Foo Definitive Edition") are two games;
3. skips games already in the Deku collection by title (on an Xbox entry or one with no platform;
   a 360 game by its identical title only);
4. looks up at most **25 games a run** (Series first, then One, then 360; never-tried first, then
   the oldest try), with
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

### Xbox 360 games

Deku Deals lists backward compatible Xbox 360 games as Xbox One items (with the original 360
release date), so an owned 360 game is matched like any other and then has to pass one more check:
the Deku item's Xbox release date (from its "Release date:" line, the earliest Xbox date when it
lists one per platform) must show that it is the 360 game and not a later remaster or port. Both
dates before the Xbox One launch (2013-11-22), or within 31 days of each other, pass; anything else
(or a missing date) leaves the game unmatched, retried after 7 days. The 360 date is TA's Released
column, read with the collection (no extra requests); until the first TrueAchievements read that
has it, 360 games wait. A `collection` override slug on a 360 game skips the date check. Matched
360 games are added as Xbox One, else Xbox Series X|S, with the format from TA's media as above.

`COLLECTION_360_DRY_RUN` (default `true`) keeps 360 games dry while the rest of the step is live:
they are looked up and listed in a Vikunja task "Review the Xbox 360 collection plan" (would add,
unmatched with the reason, waiting counts), updated in place, and Apprise pings "Xbox 360 collection
plan ready" when its lists change and are not empty. Setting it to `false` closes that task and adds
the matched games on the next runs (dry-run matches are re-checked, not searched again).

### Dry run, failures and partial adds

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

The **library step**'s state lives under `library` in the same file: the day of the last complete
pass, a failed read (retried after 6 h), a failed sign-in (day and the seed's SHA-256), the current
pass (day, the TA collection rows its first run read, the TA ids it has written, a write-error
count), TA's Games with Gold / EA Play lists (7 days), the cached page lookups, the write-ahead
entries of unfinished writes, the "seen" baseline product ids, and the report signatures that keep
pings to one per change. Deleting it is safe: the next pass starts from a fresh read (the seen
baseline is gone, so a live run reports every check-by-hand item again). The Microsoft refresh
token is a separate file, `WSYNC_XBOX_TOKEN` (`/state/xbox-token.json`, mode 600), beside it;
deleting it falls back to the `XBOX_REFRESH_TOKEN` seed.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e .[test]
.venv/bin/pytest -q
```

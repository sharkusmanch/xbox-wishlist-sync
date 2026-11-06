# Xbox wishlist sync: design

Date: 2026-11-01. Status: approved; implemented in this repository. Where the code and this
document disagree on protocol details, `docs/ta-protocol.md` and `docs/deku-protocol.md` (verified
live) win.

## Intent

The owner keeps three game wishlists: Xbox, Deku Deals and TrueAchievements (TA). The
**Xbox wishlist should be the only one maintained by hand**, with the other two following it
automatically.

- **Deku Deals** is there for **price alerts from third-party retailers** (Amazon, Best Buy and
  so on), which Xbox's own wishlist can't give. For a game whose **disc converts to a digital
  license (Disc to Digital, "D2D")**, a cheap disc is a cheap way to get the digital license,
  so Deku should watch **physical and digital** prices. For every other game it should watch
  **digital only**.
- **TrueAchievements** gets every Xbox wishlist game on its wishlist (per game, no format).

## Goals

1. **Strict one-way mirror.** Xbox → Deku Deals and Xbox → TA. Each run:
   - adds what's on Xbox but missing from the other list;
   - removes anything on Deku or TA that isn't on the Xbox wishlist, **whatever added it**.

   Removing a game from the Xbox wishlist, because it was bought or dropped, takes it off both
   lists on the next run.
2. **Deku format and platform per game.**
   - D2D game: desired format **"Physical or digital"** (form value `either`).
   - Any other game: **"Digital"** (form value `digital`).
   - Desired platform: the Xbox platform(s) the Deku entry offers (`platform_xbox_series`,
     `platform_xbox_one`).
   - DLC and add-ons on the Xbox wishlist are mirrored too (see "DLC"). They are digital-only,
     so their Deku format is `digital`, and D2D doesn't apply.
   - Existing entries whose format or platform is wrong are corrected (entries with no format
     set, or set to Physical only, are common).
3. **Runs unattended** as a Kubernetes CronJob.
4. **Starts in dry-run mode.** It sends the owner the full plan of adds, removes and format
   changes, and only goes live when the owner flips the switch.
5. **Tells the owner when something needs attention** (see Reporting) and stays quiet otherwise.

## Non-goals

- Nothing flows back into the Xbox wishlist. It is never written.
- Price targets ("desired price"), notes, privacy and hide settings on Deku entries are not
  managed. New entries take the account defaults; existing ones keep whatever the owner set.
- The Steam wishlist, the Deku/TA *collections* (owned games) and the Switch/PS platforms are
  out of scope. Only the Xbox Series X|S and Xbox One platform boxes of a Deku entry are managed.
- No UI. Reporting is through Vikunja and Apprise only.

## Anti-goals (failure even if it "works")

- **Wiping wanted games through a bad match.** A wrong or ambiguous match must never cause a
  removal or an add. Unmatched games are skipped and reported. Removals only apply to entries
  on Deku or TA that are definitely not on the Xbox wishlist.
- **Spamming.** No notification for a run that changed nothing. No new Vikunja task every run
  for the same problem.
- **Bulk-downloading XCT.live.** Its terms allow per-game lookups only. Query it only for games
  on the Xbox wishlist, one at a time, cached.
- **Leaking credentials.** Never put the TA password, Deku password, session cookies or the
  Vikunja token in logs, task text, notifications or process arguments.

## Before go-live

Add to the Xbox wishlist anything currently on Deku or TA that is still wanted, as a one-time
fix, because the strict mirror will remove it otherwise. The dry-run plan's "removes" section
doubles as that checklist.

## How it gets made (decided)

### Code, image, deployment

- **Source:** this public repository. No secrets, account ids or personal data go in it; the
  login and Cloudflare-clearing automation is public by design.
- **Image and deployment:** a container image built from a pinned commit, run as a Kubernetes
  CronJob (`backoffLimit: 0`, `concurrencyPolicy: Forbid`, a non-root, read-only security
  context, an alert when the job stops succeeding), with `DRY_RUN` in its environment.
- **Secrets:** TA gamertag and password, Deku Deals email, password and wishlist id, the Vikunja
  token and the Apprise URL come from a Kubernetes Secret (synced from a secret store) as
  environment variables. The Vikunja token belongs to a dedicated user or token used only by
  this job.

### Inputs

- **Xbox wishlist:** read a JSON feed that a separate job exports from the Xbox wishlist every
  few minutes, mounted read-only; this job makes no Xbox network calls. Each record has
  `productId`, `title`, `productKind` (`game`, etc.), `kind` (`concrete`, `unannounced`, ...),
  `platforms`, `editions`, `preorder` and `added`.
- **Feed staleness:** a stale or unreadable feed must abort the run without changing
  anything. A truncated or empty feed must never be read as "the wishlist is empty, remove
  everything". Refuse to remove when the feed has implausibly fewer items than the last good
  run (below 70% of it, see the README).

### Deku Deals (verified 2026-11-01, read-only)

- No Cloudflare. A plain HTTPS client works.
- **Login:** `POST https://www.dekudeals.com/login`, form fields `return_to`, `email`,
  `password`. Session is cookie-based.
- **Read the wishlist:** the public share page `https://www.dekudeals.com/wishlist/<id>`. The
  id is read from the environment, not hard-coded. It pages at 120 items and shows each entry's
  "Desired format". The logged-in item page shows the full entry settings.
- **Add / remove:** `POST /items/<item-uuid>/watch` with `to=true` / `to=false` and header
  `render: true`. The item UUID is in the item page's forms
  (`action='/items/<uuid>/watch'`).
- **Entry settings:** the same `POST /items/<uuid>/watch`, using the item page's
  `watch-details-form` fields:
  - `desired_price` (`""` = account default, `drop`, `specific` with `specific_price`,
    `lowest`);
  - `desired_format` (`""`, `either`, `physical`, `digital`);
  - one `platform_<key>` checkbox per platform the item has (hidden `false` plus checked
    `true`);
  - `note`, `note_private`, `hide_from_public_view`.

  When changing format or platform, re-send the existing values of every other field so
  nothing else changes.
- **Items:** one Deku item per game across all platforms, e.g.
  `items/clair-obscur-expedition-33` covers PS5, Xbox Series X|S and Steam. A match must offer
  an Xbox platform. DLC has its own items, e.g. "Kingdom Come: Deliverance II Expansion Pass".
- **Add flow** (verified on a test item, see `docs/deku-protocol.md`): `to=true` creates the
  entry, then a second details POST sets format and platform.

### TrueAchievements (verified 2026-11-01)

- **Cloudflare blocks plain clients.** Every TA request goes out through **`curl_cffi`**
  (`impersonate="chrome"`): the login, every page read and the JSON AJAX writes. When a request
  meets a Cloudflare challenge, a **FlareSolverr** v3 instance supplies a `cf_clearance` cookie
  and its user agent, which then go with the direct requests (one refresh per request, then the
  request fails).
  - **FlareSolverr only ever gets an anonymous, cookie-less GET of a public TA page.** It logs
    the cookies and POST bodies it is sent, so the password and the session cookies never go
    through it. (The first version logged in and fetched pages through FlareSolverr; that was
    changed before publication.)
  - The login is a WebForms post to `/login` with the `#frm` hidden fields plus
    `txtGamerTag`, `txtPassword`, `Command=btnLogin_click` and `Argument=""`.
  - Success sets cookies including `GamerToken`, `HashKey` and `GamerID`.
- **Wishlist add / remove:** `POST /ajaxfunctions.aspx/AddToWishlist` and
  `/ajaxfunctions.aspx/RemoveFromWishlist`, JSON
  `{"Wishlist":{"GameID":<ta id>,"DLCID":<0|id>,"HashKey":<HashKey cookie>}}`. Success is when
  `d.success` is true or `d.errors` is empty.
- **TA game ID:** a game page (`/game/<slug>`) carries it as `data-game="<id>"`.
- **Reading the TA wishlist:** `https://www.trueachievements.com/gamer/<tag>/wishlist`, logged
  in, 25 rows a page since TA's 2026-11-06 redesign; every page is read and checked against the
  list's Games + DLC counts (see `docs/ta-protocol.md`). If the list can't be read completely in a run, that run makes **no TA writes** and sends an Apprise failure notice.
- Page fetches are slow when a clearance is needed. Keep TA page fetches to what changed: cache
  the productId → TA game ID mapping.

### Deciding D2D per game

- **Source 1:** TrueAchievements' lists `/disc-to-digital/games` (converts) and
  `/disc-to-digital-unsupported/games`. These are member votes, per game. Fetch them at most once
  a day and cache them.
- **Source 2:** XCT.live (`https://xct.live/api/v1/d2d`), **per game, only for wishlist
  games**, cached. Its rows are per edition/print, and some are account-verified.
- **Verdict:** D2D = yes if either source says it converts and neither contradicts it. When
  they disagree, or XCT says "varies by print", "unconfirmed" or "retest", treat the game as
  **disputed**. Disputed means a Vikunja task, plus format `either` so the disc deals stay
  visible. A digital-only release, or no data at all, means `digital`.
- **Re-checks:** a game's verdict can change later. TA votes accumulate, and publishers join
  D2D. Re-evaluate on a slow cadence, e.g. weekly per game, and correct the Deku format when it
  changes.

### Matching an Xbox wishlist item to Deku and TA

- Start from the Store title. Strip edition noise (Premium / Deluxe / Digital Deluxe / Gold /
  Ultimate / Complete Edition, ™/®) to get the base game, e.g. "<Game> Premium Edition" or
  "<Game> Digital Deluxe Edition".
- A match must be **confident**: exact normalized title, or exact after removing the noise,
  and on Deku the item must offer an Xbox platform. Anything else is unmatched. There is no
  fuzzy guessing.
- Bundles and collections match their own Deku/TA entry when one exists. Otherwise they're
  unmatched.
- **TA has several pages for one game** (Xbox Series X|S, Xbox One, "(Windows)" for Play
  Anywhere titles). Pick **one** per Xbox wishlist item:
  - the Xbox Series X|S page;
  - else the Xbox One page;
  - never a Windows or Xbox 360 page.

  Under the strict mirror, other-version entries for the same game come off TA's wishlist.
- **Removals vs. unmatched games** (implementing the anti-goal). An entry on Deku or
  TA is removed only when **both** of these hold:
  1. it is not the confident match, or override, of any Xbox wishlist item;
  2. **no** Xbox item is "ambiguous" on that list.

  An Xbox item is **ambiguous** when the search found candidate pages but none was a confident
  match. That case gets a Vikunja task and **pauses removals on that list** until it's
  resolved, though adds continue. An Xbox item with **no candidate page at all** (typical for
  unannounced games) doesn't block removals: it can't have an entry on the list. The dry-run
  plan shows which removals are paused and why.
- **DLC** is included. Feed items whose `productKind` isn't a game
  (add-ons, expansion passes, season passes) are mirrored like games.
  - On **Deku**, DLC has its own items, e.g. "Kingdom Come: Deliverance II Expansion Pass",
    matched by title like a game, with format `digital`.
  - On **TA**, a DLC wishlist entry is the parent game's `GameID` plus a `DLCID`. Find the
    parent game page first; the DLC ids are on its DLC listing (`/game/<slug>/dlc`). Then
    match the DLC by title there.
  - The confident-match and ambiguity rules apply unchanged.
- **Manual overrides:** a small mapping file mounted from a ConfigMap. It maps `productId` to a
  Deku slug, a TA slug/ID or `skip`, and is how the owner resolves an unmatched task. Overrides
  beat automatic matching.
- **Persistence:** keep the resolved mapping productId → {deku uuid, ta game id} between runs
  so pages aren't re-fetched every time. The state file lives on a small volume.

### Reporting

- **Apprise:** an in-cluster Apprise API endpoint (`APPRISE_URL`). Send:
  - a run that **fails**: login rejected, a layout change breaks parsing, FlareSolverr
    unreachable, or the feed stale or implausible;
  - a run that **changed something**: one short summary, e.g. "+2 Deku, −1 TA, 1 format
    change";
  - the **dry-run plan** being ready.
- **Vikunja**, project **"Wishlist Sync"** (`VIKUNJA_URL`, `VIKUNJA_TOKEN`).
  - One task per problem game: **unmatched** on Deku or TA, or a **disputed** D2D verdict. Name
    the game, the productId and what's unclear, plus how to fix it (the override file).
  - **Deduplicated:** one open task per game and problem, found by a stable marker in the
    task. It is **closed automatically** when the problem goes away: matched, overridden, or
    the game removed from the Xbox wishlist.
  - Dry-run: one task, "Review the wishlist sync plan", with the full plan (adds, removes and
    format changes per list), updated in place on each dry run.
  - **Unannounced games** (`kind: unannounced`) with no Deku/TA page yet don't get tasks.
    They're listed in the run log and the plan and retried each run.
- Silent when nothing changed and nothing is newly wrong.

### Network

- DNS.
- FQDN egress: `www.dekudeals.com`, `www.trueachievements.com` and `xct.live`, TCP 443.
- FlareSolverr, Apprise and Vikunja in the cluster: egress from this pod, plus matching ingress
  allowances where those namespaces default-deny.

### Schedule and modes

- **Hourly.** A run fetches only what changed and caches the rest (mapping, D2D verdicts, TA
  lists) to fit well inside the hour; `concurrencyPolicy: Forbid`. No write starts after 45
  minutes, and SIGTERM stops a run cleanly.
- `DRY_RUN=true` at first deploy. A dry run computes the full plan, writes it to the Vikunja
  review task and pings Apprise, and writes nothing to Deku or TA. The owner flips it to `false`
  after approving.
- Also useful: a manual one-off run of the CronJob, and a CLI flag to limit a run to one list
  for testing.

## Decisions

- DLC is included.
- Removals pause on a list while any Xbox item is ambiguous on it.
- TA picks the Xbox Series X|S page, else the Xbox One page, never Windows or Xbox 360.
- TA wishlist read: `/gamer/<tag>/wishlist`. An incomplete read means no TA writes that run.
- Implementation: Python with `curl_cffi`; state in a JSON file; the TA and Deku sessions are
  reused between runs and renewed when they expire.

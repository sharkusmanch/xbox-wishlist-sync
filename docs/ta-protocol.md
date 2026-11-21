# TrueAchievements protocol (verified live 2026-11-01)

Everything here was observed on the real site with the owner's account. The write tests used
three temporary games nobody on the account wanted (LEGO Batman (JP) `1873`, LEGO Dimensions
(Xbox 360) `5548`, LEGO The Hobbit (Xbox 360) `5074`) and one temporary DLC entry
(Kingdom Come: Deliverance II, Horse Races: game `13982`, DLC `11986`). Every one was removed again
and the wishlist was read back identical to how it started.

## Transport

**Changed 2026-11-01 (before publication): FlareSolverr never sees credentials or session
cookies.** FlareSolverr logs every request it receives at its default `info` level, including the
`postData` body and the `cookies` list, so a login POST (with `txtPassword`) or a logged-in page
fetch (with `GamerToken`, `HashKey`, ...) sent through it would put those values in its log. So:

- **Every TA request goes out directly** through `curl_cffi` with Chrome's TLS fingerprint
  (`impersonate="chrome"`): the login `GET`/`POST`, every page (wishlist, search, game, DLC and the
  Disc to Digital lists) and the AJAX writes. The client sends its own `Cookie` header (the session
  cookies plus `cf_clearance` when it has one), follows redirects itself (only to
  `https://www.trueachievements.com/`, at most 5, keeping each hop's `Set-Cookie`), and never
  follows a redirect from an AJAX write.
- **FlareSolverr is only a clearance source.** It is sent nothing but an anonymous, cookie-less
  `{"cmd": "request.get", "url": "https://www.trueachievements.com/", "maxTimeout": 90000}`. The
  client keeps the answer's `cf_clearance` cookie and `userAgent` (and none of its other cookies)
  and sends both with its direct requests; the clearance only works with that user agent.
- **When to ask for a clearance:** only when a direct request meets Cloudflare: HTTP `403`, a
  `cf-mitigated: challenge` header, or a `429`/`503` "Just a moment" / `challenge-platform` page.
  The request is then repeated once with a fresh clearance; a second challenge fails it as a
  `TAError` (a challenge never reached TA, so repeating a write is safe). Each later request may
  refresh once more. On 2026-11-01 TA answered `curl_cffi` without any challenge, with or without
  a clearance, so FlareSolverr was not needed at all; the client copes either way.
- **Form POSTs are challenged (2026-11-20).** A form POST to a page (`/login`, a game page's
  postback) now meets HTTP `403` even with a fresh clearance, while page `GET`s and the AJAX
  calls (`/ajaxfunctions.aspx/...`) pass. So the collection add uses the site's AJAX add (below),
  and a fresh login fails: the client lives on its saved session until that expires.
- A `cf_clearance` that TA itself renews in a `Set-Cookie` replaces the stored one (same user agent).
  The clearance is kept in memory only, never in the session file.
- FlareSolverr's own API, for reference: `POST <FLARESOLVERR_URL>` with JSON; the answer's
  `solution` has `response` (HTML), `cookies`, `userAgent`, `status` and the final `url`. A failure
  is `status: "error"` with a `message`, sent with HTTP 500. The client has no way to send it
  cookies or a POST body.
- Static assets (`/js-responsive/...`) need no clearance with `curl_cffi`.

## Session

- `GET /login`, then `POST /login` (`Content-Type: application/x-www-form-urlencoded`, standard
  form encoding, `Origin`/`Referer` on TA) with every `<input>` inside `<form id="frm">` except
  checkbox, radio and password types (`__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `txtSearch`, ...),
  plus `txtGamerTag`, `txtPassword`, `Command=btnLogin_click`, `Argument=` (empty). Send the
  cookies of the `GET` (`ASP.NET_SessionId`). Both go out through `curl_cffi`.
- A successful `POST` answers `302` to `/` and sets the session cookies on that redirect.
- Success is a `GamerToken` cookie. A successful login also sets `HashKey`, `GamerID`,
  `TrueGamingIdentity`, `UserHash` and others. A wrong password is not tested live (lockout risk);
  the client treats "no `GamerToken`" as a failed login.
- **Logged-in marker.** As served (what `curl_cffi` gets), a logged-in page's header has the
  account link `<a href="/login" title="Log in" rel="nofollow" class="login hd">` and no
  `<div class="login-full hd">` log-in menu; a logged-out page has the `login-full hd` menu.
  In a browser, TA's JavaScript also appends `g-li` to `<body class>` when a `TrueGamingIdentity`
  cookie exists (`ta g-li d`); pages fetched through FlareSolverr showed that form, served HTML
  never does (body class `ta`, `ta na` on the login page). The client accepts either. A
  logged-out wishlist does **not** say "Please log in" (see below). Pages that do say
  "Please log in" (e.g. the game collection) are also treated as gated.
- The gamertag in URLs joins words with `+`: `/gamer/First+Second/...`.
- Cookies (without `cf_clearance`) are kept in a session file outside the repo
  (`ta-session` next to the state file, e.g. `/state/ta-session`, mode 600; a missing directory is
  created 700). A `Set-Cookie` that expires a cookie removes it.

## Wishlist (read)

- `GET /gamer/<tag>/wishlist`, logged in. Also reached from `/wishlist.aspx` (redirects there).
- The owner's view has `<h1 id="h1PageTitle" class="pagetitle">My Wish List</h1>`, then (since
  TA's 2026-11-06 redesign, "Site Feature: New Release Calendar & Personal Wishlist Updates") a
  filter bar, a top pagination block, one `<div class="ach-panel wl-item nc">` per entry, a bottom
  pagination block, then `<div id="divButtons" class="buttons">` (Clear wishlist, Remove played).
  Synthetic copies of the layout: `tests/fixtures/ta_wishlist_v2_p1.html` / `_p2.html`.
- A **game row**: inside `<div class="wl-head">`,
  `<a href="https://www.trueachievements.com/game/<slug>/achievements">Title</a>` and
  `<span class="remove"><a ... href="javascript:Postback('btnRemoveFromWishList', <game id>)">`.
  Below the head, `<details class="wl-listings">` lists the store products (`/products/<id>/...`)
  with prices; the parser ignores them (it takes the row's first `/game/` link).
- A **DLC row**: `<a href="/game/<game slug>/dlc/<dlc slug>">Game - DLC title</a>` and
  `Postback('btnRemoveDLCFromWishList', <dlc id>)`. The row has **no game id**, only the game's
  slug, so the client reads `data-game` from `/game/<game slug>` (cached per slug in `game_ids`).
  Seen on the old layout only: the live list had no DLC when the new layout was checked. A DLC row
  in some other markup fails the row / remove-button checks (`IncompleteRead`), never a short read.
- **Filter bar:** `<div class="gr-wrap wl-wrap" data-show="" data-region="" data-sale="" data-gp="">`
  plus a search box `<input type="search" name="q" value="">`. Filters are URL parameters
  (`?show=games|dlc`, `?sale=1`, `?gp=<pass>`, `?q=`), not stored per gamer; `region` (a cookie or
  `?region=`) changes prices only. The client raises `IncompleteRead` if `data-show`, `data-sale`,
  `data-gp` or the search value is non-empty. The **Show** pills carry the whole list's counts:
  `<a class="gr-pill" ...>Games <span class="n">12</span></a>` and `...>DLC <span class="n">0</span></a>`.
- **Paging (since 2026-11-06):** 25 rows a page. Page 1 is `/gamer/<tag>/wishlist`, page N is
  `?page=N`. Each block is `<ul class="pagination">` with `<li class="current"><a ...>N</a></li>`
  and links to the other pages (`?page=N+1` when there is a next one). The client reads page 1,
  then each next page while the current page links one (at most 40 pages). It raises
  `IncompleteRead` if a page's current number isn't the one asked for, the Games + DLC total
  differs between pages, the rows read don't add up to that total, or a paged page has no total.
  Before the redesign the list was one page with no counts (a short own list and 90 rows of
  another gamer's public list both came unpaged); that layout still parses as one page, but
  paging without counts is `IncompleteRead`.
- **Per-page checks:** the heading is present and is "My Wish List", the page reaches `</main>`,
  `divButtons` follows the rows, every `ach-panel` yields a link and a remove id, and the number of
  remove buttons on the whole page (`btnRemove(DLC)FromWishList` postbacks) equals the rows read.
  Any miss is `IncompleteRead`.
- Logged out, the same URL renders a public view: heading `<Tag>'s Wishlist`, body without `g-li`,
  and no remove buttons (so no ids). The client logs in once and re-reads.
- An empty list was not observed (it would have meant clearing the real list). The client accepts a
  page with the heading, `</main>`, zero `ach-panel` rows and zero remove buttons as empty.
- The gamer page's sidebar (`/gamer/<tag>`) also lists the wishlist, with DLC links as
  `/dlc.aspx?dlcid=<id>`; it is not used.

## Game Collection (read)

Verified live 2026-11-02; code in `wsync/ta_coll.py` and `TAClient.collection()`. Ported from the
`xbox` plugin's `ta_collection.py`, with the transport replaced (curl_cffi session, never
FlareSolverr).

- `GET /gamer/<tag>/gamecollection`, logged in (login-only for every gamer). The page's list
  filters are form controls inside `.ajaxlistfilter` containers (a search/filter `<div>`, the
  "View and Filter" `<ul>` and a `<span>` of hidden `oGameCollection_*` / `txtGamerID` inputs),
  read the way the page's `AJAXList.GetListFilters` does: a text/hidden input with a value, a
  *checked* radio/checkbox, a select's selected option (else its first option).
- **`multiple` selects must be left out.** Ownership (`sddOwnerShipStatusIDs`), play status
  (`sddPlayStatusIDs`) and platform (`ddlPlatformIDs`) are `multiple` selects with nothing chosen
  (= any), and the ownership and play-status ones have no blank first option, so "first option"
  sends `1` (Owned) and `1` (Playing now): the list then shrinks to a fraction of the collection
  with a matching footer total, i.e. a plausible-looking partial read. Sending none of the three
  gives the whole collection, every ownership status included.
- The list is an AjaxList fragment: **GET**
  `/gamer/<tag>/gamecollection?executeformfunction&function=AjaxList&params=<params>`. A POST to the
  same URL answers "no matching titles". `params` is `oGameCollection|` followed by `&name=value`
  for every filter, URL-quoted, after two escapes of the page script: the first `&` of each value
  becomes `*amp*` (`AJAXList.ReplaceCharacters`) and every `|` becomes `/\pipe\/` (`ff.SafeParam`).
- **Pinned filters.** The page restores the gamer's saved view, and a narrowed one (a search term, "Started", a media or Mythic
  filter, apps only, ...) gives a short list whose footer total still agrees, so the client pins every
  narrowing filter instead of trusting the page: drop `txtSearchString`; `ddlStartedStatus=0`,
  `ddlDifficulty=0`; `ddlCompletionStatus`, `sddGameMediaID`, `ddlGameNotes`, `ddlMythicStatus`,
  `asdGamePropertyID` = ` ` (each control's `data-default`, checked live 2026-11-02);
  `ddlTitleType=Game` (games only; its `data-default` is blank = apps too); `ddlSortBy` and
  `oGameCollection_Order` = `TitleName` (stable paging; the live sort accepts it, rows came back in title order).
  Any other filter name the page sends that is not a view, column, `oGameCollection_*`, id or
  DLC-inclusion control makes `collection()` raise `IncompleteRead` naming it, and the page must carry
  `txtGamerID` and `oGameCollection_Order`.
- Overrides: drop `chkExcludeNotOwned`; `oGameCollection_ItemsPerPage=100` (TA caps a page at 100),
  `oGameCollection_Page=<n>`, `oGameCollection_ShowAll=False`; `chkColPlatform`,
  `chkColOwnershipstatus`, `chkColMedia` and `chkColReleasedate` switch the four columns the sync
  needs on, on top of the gamer's saved columns. The release date column is headed `Released`, in
  `dd Mon yy` form (`07 Feb 12`); a blank or unparseable date is read as none (added 2026-11-17 for
  Xbox 360 games in the Deku collection step).
- The fragment is a pagination block, then `<table class="maintable">`: a header row of `<th>`
  (`Title`, `Platform`, `Ownership`, `Media`, ...; the client reads cells by header name), one
  `<tr class="odd|even">` per title, and a footer `<tr class="total">` whose first cell reads
  ` N Titles` (thousands separated by commas). A page past the last returns a near-empty fragment
  with no total.
- A row: title in `<td class="smallgame"><a href="/game/<slug>/achievements">` (entities are mostly
  escaped, but a bare `&` occurs: "Fast & Furious"); platform as an icon only,
  `<img ... title="xbox-series-x-s">` (slugs seen: `xbox-series-x-s`, `xbox-one`, `xbox-360`,
  `windows`, `ios`, `android`, `nintendo-switch`); ownership and media as text; the TA game id in the
  edit button, `GameCollection.EditDetails(<id>, false, true)`, and `chkMultiEdit_<id>`.
- Ownership values seen: `Owned`, `Xbox Game Pass (deprecated)`, `PC Game Pass`, `EA Play`,
  `No longer have`, `Ubisoft+`, `Games with Gold`, `Xbox Game Pass Premium`. Media values seen:
  `Digital`, `Disc`, `Disc & Digital`, `Disc-to-Digital`, and blank.
- **The AjaxList fragment carries no logged-in marker** (no `g-li`, no header), so `collection()`
  reads it with `need_login=False` and relies on the collection page having just been read logged
  in; a logged-out answer is the "Please log in" page, which the shared `_get` retries after a login.
- Completeness: `collection()` raises `IncompleteRead` unless the rows read equal the footer total
  of every page, the total is present and unchanged between pages, no game id repeats, and the
  total is plausible (at most 20,000). Pages are fetched until the rows reach the total.

## Writes (AJAX)

- `POST https://www.trueachievements.com/ajaxfunctions.aspx/AddToWishlist` or `.../RemoveFromWishlist`
  with `Content-Type: application/json; charset=utf-8`, `X-Requested-With: XMLHttpRequest`,
  `Origin`/`Referer` on TA and the session cookies, sent through `curl_cffi` (plus `cf_clearance`
  and FlareSolverr's user agent once a challenge has made the client fetch a clearance).
- Body (the site's own `WishListService` in `logged-in.min.js` sends the same):
  `{"Wishlist": {"GameID": <int>, "DLCID": <int, 0 for the base game>, "HashKey": "<HashKey cookie>"}}`.
- Answer: `{"d": {"__type": "cbAjaxFunctions+GenericAjaxResponse", "success": bool, "errors": [..] | null, "information": "", "html": ""}}`.
  The site's success test is `d.success || d.errors === null || d.errors.length === 0`.
- Observed answers:
  - add / remove of a valid entry: `success: true, errors: null`;
  - add of an entry already listed: `success: false, errors: ["This Game is already on your wishlist"]`
    (the client raises `AlreadyListed`: the run's read of the list must have been wrong or stale);
  - remove of an entry not listed: `success: false, errors: ["No wishlist items to remove"]`
    (the client raises: it can't tell this from a wrong id);
  - no session cookies: `success: false, errors: ["The Gamer must be specified"]` (the client
    logs in once and retries, then raises `AuthError`).
- `HashKey` is **not** checked by these two endpoints: an add with `HashKey: "0"` and a valid
  session succeeded (and was removed again). The session cookies are what identify the gamer.
- A DLC entry is `GameID` = the parent game, `DLCID` = the DLC id; it showed up as a DLC row and was
  removed with the same pair.

## Game Collection (writes)

Used by the library step (`TAClient.collection_page`, `collection_add`, `collection_set`).

- **Add.** The logged-in game page carries `Postback('btnGC_Add','<id>')` or `btnGC_Remove` (in the
  collection already): it gives the game id and says whether the game is listed. The add itself is
  the AJAX call TA's own game lists use (`SiteList_AddGame` in
  `/js-responsive/shared/game-collection-rd.min.js`, read 2026-11-20): `GameCollection_UpdateGame`
  with `GameCollection_Data` = `GameID`, `GamerID`, `HashKey` and `Field: "AddGame"`. Its answer
  is not relied on (the site's script ignores it); an answer with `errors` fails the add (a
  logged-out one logs in again once). The page is then read again from its final address (after
  TA's redirects, e.g. a renamed slug) and must show `Postback('btnGC_Remove','<id>')`. The page's
  own form postback (`Command=btnGC_Add`) is not used: Cloudflare challenges it (see Transport).
  Both the button's id and, when given, the expected id are checked before the add. A game
  already in the collection is not added again; a *new* add of one raises `AlreadyListed`.
- **EditOptions.** `GameCollection_EditOptions` (the same AJAX endpoint family as the wishlist
  writes, payload `GameCollection_Data` with `GameID`, `HashKey`) returns an HTML fragment with a
  `<select>` per field (`id="<prefix>_<game id>"`; ownership and media). The current choice carries
  the `selected` marker (`<option value="1" selected="selected">Owned</option>`, verified
  2026-11-03). Options are parsed attribute by attribute, **in any order**, quoted or bare; a
  dropdown with several selected options is a parse failure, not a guess; none selected, or value
  `""`/`"0"`, is blank.
- **UpdateGame.** `GameCollection_UpdateGame` with `Field=<prefix>_<game id>` and `Value=<option
  value>` (plus `GamerID`). The answer's `d.UpdateValue` is the label now set; the client checks it
  equals the wanted label and raises otherwise. A logged-out answer logs in again once.
- **Compare-and-set.** The one EditOptions read decides first: already the target label -> no
  post; with the caller's expected labels, a current one that is neither -> `ChangedOnTA`, no post.

## Page not found

An unknown `/game/<slug>` is answered with HTTP 200 and TA's page-not-found page: `<h1
class="pagetitle">Page not found</h1>` and the marker `PageNotFound404`, with no game id and no
canonical link (verified 2026-11-17). A real 404 is possible as well. "No page" is therefore HTTP
404 *or* the marker. Anything else is *not* "no page": `game_page_or_none` raises `Unreachable` for
what every address would meet (a network error, an unpassed challenge; and, as its subclass
`PageUnavailable` because it is that address's answer, a page with no game id at all: throttling,
maintenance; HTTP 429, 502-504 or Cloudflare's 52x), and a plain `TAError` for an error of that
address alone (any other HTTP error; a page with game ids that isn't one game's page: several ids, no
title). The library step counts `PageUnavailable` against the game, never the plain `Unreachable`.

## Property lists

Public lists such as Games with Gold and EA Play: `GET /<slug>/games?page=N` (no login).

- Each page must carry `<link rel="canonical">` to `/<slug>/games`; a wrong slug serves the home
  page, so a page without it is an error.
- Rows are `<tr id="tr<game id>">`. **0 rows** on a page is TA's throttle answer: an error, never
  an empty list.
- The last page is the highest page number in the page's own pagination links (not "until a page
  has fewer than 100 rows", which fails for a list that is an exact multiple of 100); the list is
  returned only when read to that page, and a list longer than the page cap (20) is an error.

## Search

**Layout change seen 2026-11-01.** `/searchresults.aspx` now
redirects to `/search-results?search=<query, form-encoded>`, an all-categories overview whose
Games section shows at most 8 rows ("8 of 45"). The client asks for the games-only view instead:

- `GET /search-results?search=<query>&cat=games[&page=N]`: 25 rows a page, in
  `<a class="sst-game-row" href="/game/<slug>/achievements">` rows with
  `<span class="sst-game-name">Title</span>` (search terms wrapped in `<span class="hl">`) and
  `sst-platform-tag` spans; heading `<h2 class="results-heading">... <span class="count">45
  results</span>`; paging links `...&cat=games&page=2` in `ul.pagination`. The client follows
  them up to `SEARCH_PAGES` (4). No results: the heading with `0 results` and no rows.
- A row found through an alias adds `<span class="sst-matched-name">Matched: &ldquo;...&rdquo;</span>`
  after the name (seen 2026-11-01); it is not part of the title.
- On the overview page, series rows share the `sst-game-row` markup (`href="/series/..."`); only
  `/game/` rows in the `#sr-games` section count.
- In the new layout one game page often lists several platforms ("Xbox Series X|S, Windows").
- The parser still reads the old layout below, in case TA serves both for a while.

Old layout:

- `GET /searchresults.aspx?search=<query, percent-encoded>`.
- Several hits: a table `#oSearchResults`, rows
  `<td class="gamerwide"><a href="/game/<slug>/achievements">Title</a></td><td>Game</td>`. Other
  types in the same table: `Series`, `Franchise` (dropped). A pre-release game's link may lack
  `/achievements` (`/game/LEGO-Skylines`). 54 rows came back for "lego" with no paging.
- **Exactly one hit redirects to the game's achievements page** (`/game/<slug>/achievements`);
  the client uses the final URL (after the redirects it follows) and the page title for that case.
- No hits: `<div class="informationpanel">There are no matching search results...`.
- Platform-specific pages are separate games with a suffix: `Kingdom Come: Deliverance (Windows)`,
  `LEGO Harry Potter Collection (Xbox One)`, `... (Xbox 360)`.

## Game page

- `GET /game/<slug>` (the "News" tab). `<title>` is `<Title> News and Videos | TrueAchievements`
  (the suffix is sometimes missing); the achievements tab is `<Title> Achievements | ...`.
- The id is `data-game="<id>"` (twice, one value) on the follow/wishlist buttons, e.g.
  `id="related_<id>_wishlist" data-game="<id>"`.
- **Unreleased games have a negative placeholder id** (Persona 6: `data-game="-7827"`; 2026-11-01,
  most announced-but-unreleased games). Whether `AddToWishlist` accepts a negative `GameID`, and
  how such a row looks on the wishlist page, is **not verified**: the client refuses to write
  ids < 1, the run treats a placeholder-only result as "no page yet", and a wishlist row whose
  remove button carries a negative id fails the read (IncompleteRead), which blocks TA writes.
- Details are `<dt>Name</dt><dd>value</dd>` pairs: `Publisher`, `Developer`, `Release`, `Platform`,
  `Genres`, `Features`, `Hardware`, `Notes`, `Medium`, `Size`, `Completion est`.
- `Platform` lists the page's own platform **first**, then others it plays on:
  - `Xbox Series X|S, Windows (Windows 10)` (Kingdom Come: Deliverance II, Play Anywhere);
  - `Xbox One, Xbox Series X|S` (The Witcher 3: Wild Hunt; LEGO Harry Potter Collection (Xbox One));
  - `Xbox 360` (Mass Effect); `Windows (Windows 10)` (Kingdom Come: Deliverance (Windows)).
  `TAGame.platform` is the first entry, without the parenthesised note. Page choice also rejects
  any page whose title ends in `(Windows)`/`(Xbox 360)` or whose slug ends in `-Windows`/`-Xbox-360`,
  in case the field's order ever differs.
- `/game.aspx?gameid=<id>` redirects to `/game/<slug>/achievements`.

## DLC listing

- `GET /game/<slug>/dlc` (the game's "DLC" tab; the tab shows the pack count). Heading
  `<h1 class="gh pagetitle"><Title> DLC Achievements</h1>`.
- One `<div class="pnl-hd no-pills no-pr game">` for the overall stats, then one
  `<div class="pnl-hd no-pills no-pr game dlc">` per pack with
  `<h2><a href="/game/<slug>/dlc/<dlc slug>">DLC title</a></h2>` and a menu
  `id="btnDLC_<dlc id>"` whose items include `Postback('btnAddDLCToWishList','<dlc id>')`.
- Packs include title updates (`<span>Update</span>`, e.g. "Hardcore Mode") as well as add-ons.
- A game without DLC answers the same page with no `game dlc` panels.
- `/dlc.aspx?dlcid=<id>` redirects to `/game/<slug>/dlc/<dlc slug>`.

## Request budget

TrueAchievements limits clients that send too many requests, and the job shares the home IP with the
owner's own browsing, so every TA request of a run goes through one meter (`wsync/meter.py`). It is
the only place that counts and paces; a second client of the same run can't restart either.

- **What counts.** Each request that reaches TA: every redirect hop, login page, page read, AJAX
  call and every FlareSolverr clearance. Counts are kept per UTC day in `state.ta_requests`
  (`day`, `total`, `by` step) and start again at the first run of a new day. The requests are counted
  under the step running at the time: `wishlist`, `d2d`, `library`, `collection`, or `other` outside
  any step. A clearance is counted as `clearance`: it adds to the ceiling and never to a share, and
  it is never refused (the request that needed it is the one the budget governs).
- **Pacing.** Requests start 4 s plus 0-3 s at random after the one before (`WSYNC_TA_GAP`,
  `WSYNC_TA_JITTER`), counted start to start, so a slow answer doesn't add to the wait and the
  rhythm isn't regular.
- **Shares and ceiling.** The day has a ceiling (`WSYNC_TA_DAILY_BUDGET`, 300) and a share per step
  (`WSYNC_TA_BUDGET_*`: wishlist 80, library 160, collection 60, other 20). The Disc to Digital lists
  count in the wishlist share. A request is refused when it would pass its step's share or the
  ceiling. A share or ceiling of 0 turns those requests off: each is refused, quietly, and a 0 limit
  is never reported as spent (no warning, no task).
- **Admission.** A unit of several requests asks first (`ta_admit`, which counts nothing) and is
  skipped whole when it doesn't fit, so the budget isn't spent on half a unit: a wishlist item's
  lookup (4 requests, and only while 10 more of the wishlist share stay free for the TA adds and
  removes and the re-read after them, so lookups never spend what the writes need), the collection
  read (its pages plus 2, from the last read's size), a library page lookup (one per address tried)
  and a library write (7: an add takes 6 requests, a Games with Gold add 7, a fix at most 4).
- **A spent budget is a quiet stop.** A refused request raises `BudgetSpent`, a `TAPaused`: nothing
  was sent, and the step stops its TA work without an issue, a back-off stamp or a failure counted
  against a game; it goes on at a later run with room. The run logs a warning per spent share. While
  the wishlist share or the ceiling is spent, one Vikunja task a UTC day ("TrueAchievements request
  budget spent") and one ping say so, and the first run of a later day with room closes it. The
  other shares being spent only log. The log's `ta requests:` line gives the run's and the day's
  counts per step.
- **Login back-off.** A failed login (a wrong password, a server error, a Cloudflare block) is not
  tried again for 24 h while the TA password and the session file are unchanged
  (`state.ta_login`: the time, a hash of the two, and whether the issue was raised). The guard
  refuses with `LoginBackoff`, another `TAPaused`, so every TA step stops quietly, but only at the
  login: each step still sends the first page request of its work every run, and that request is
  what finds the session not accepted (about 24 a day for the wishlist and, with both daily read
  times at 12:30, 11 each for the library and collection steps). The guard raises its issue once
  per episode, at the first failure. The login retried after 24 h fails the TA wishlist read while
  TA still refuses it, an issue of its own, so a failing episode pings once a day (the runs in
  between exit 0). A new password, a re-seeded session file (the cookies of a working browser login
  in `/state/ta-session`; see "Session") or a login that works ends it at once. A request the meter
  refused during a login is not a failed login.
- **Stored wishlist misses.** A lookup that found nothing to add (no page, only a placeholder, an
  ambiguous result, a search cut off by the page cap) is kept under `mapping[productId]["ta_miss"]`
  with its candidates, the check time and a key: the base title, the feed's kind, the product kind and
  the TA override. It is reused without a request until due; a changed key looks up at once, a match
  clears it. An override page that is still a placeholder is kept the same way as `ta_override_miss`.
  Due is the start of the product's first slot at least a day after the hour it was checked in: a
  released game's slot is its own hour of the day (daily), a game not out yet has its own hour of
  the week (weekly), both from a hash of the product id, so lookups made together don't come back
  together. A lookup that failed is retried at once only for a FlareSolverr failure; otherwise the
  item backs off 1, 2, 4 ... hours (at most 24, ending on the hour) under `ta_backoff`, kept with the
  same key: a changed key looks up at once (its failures count from 1 again), and a lookup that got
  an answer clears it. A lookup without budget is deferred, never kept or backed off.
- **Disc to Digital lists.** The two TA lists are read once a week (`state.ta_lists`), not daily.
  A failed refresh with a copy serves the copy and waits another week; with no copy it waits 24 h
  and that run raises an issue. The runs inside the wait have no lists either (`D2DWaiting`, nothing
  sent): they keep the cached verdicts, make no Deku format changes and keep the `d2d-disputed`
  tasks, with a warning only, so nothing is judged on XCT.live alone. A refused read (`TAPaused`)
  stamps nothing: it serves the copy when there is one, and without one the run goes on as in the
  wait.
- **Games with Gold and EA Play lists.** The library step reads Games with Gold only when it has no
  copy (the programme ended, so the list is frozen) and EA Play when its copy is 30 days old. A failed
  refresh keeps the copy and waits 24 h; a refused one (`TAPaused`) uses the copy and stamps nothing.
- **The collection hand-off.** The library step's collection read is the pass's first run's, and the
  Deku collection step takes it as its own when it is past `WSYNC_TA_READ_AFTER` and the library
  started no TA write in that run, so one read serves both. It is per run: a library write in the
  run that read the collection makes the Deku step read again, and writes in the pass's later runs
  reach Deku with the next day's read. `WSYNC_LIBRARY_AFTER` defaults to 12:30 UTC, the time TA
  imports purchases itself, so that read and the Deku step's daily one fall in the same run.

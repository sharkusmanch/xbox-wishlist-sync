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
  `chkColOwnershipstatus` and `chkColMedia` switch the three columns the sync needs on, on top of
  the gamer's saved columns.
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

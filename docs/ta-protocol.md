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
- The owner's view has `<h1 id="h1PageTitle" class="pagetitle">My Wish List</h1>`, then one
  `<div class="ach-panel nc">` per entry, then `<div id="divButtons" class="buttons">` (Clear
  wishlist, Remove played, Subscribe to all).
- A **game row**: `<a href="https://www.trueachievements.com/game/<slug>/achievements">Title</a>`
  and `<span class="remove"><a ... href="javascript:Postback('btnRemoveFromWishList', <game id>)">`.
- A **DLC row**: `<a href="/game/<game slug>/dlc/<dlc slug>">Game - DLC title</a>` and
  `Postback('btnRemoveDLCFromWishList', <dlc id>)`. The row has **no game id**, only the game's
  slug, so the client reads `data-game` from `/game/<game slug>` (cached per slug in `game_ids`).
- **Paging:** none. A short own list and 90 rows (another gamer's public list) both came on one
  page with no pagination links; rows are sorted by title. The client raises `IncompleteRead` if a
  pagination block or `?page=N` link ever appears, rather than reading page 1 only.
- **Count:** the page shows no total. Completeness checks instead: the heading is present and is
  "My Wish List", the page reaches `</main>`, `divButtons` follows the rows, and every
  `ach-panel` yields a link and a remove id, and the number of remove buttons on the whole page
  (`btnRemove(DLC)FromWishList` postbacks) equals the rows read. Any miss is `IncompleteRead`.
- Logged out, the same URL renders a public view: heading `<Tag>'s Wishlist`, body without `g-li`,
  and no remove buttons (so no ids). The client logs in once and re-reads.
- An empty list was not observed (it would have meant clearing the real list). The client accepts a
  page with the heading, `</main>`, zero `ach-panel` rows and zero remove buttons as empty.
- The gamer page's sidebar (`/gamer/<tag>`) also lists the wishlist, with DLC links as
  `/dlc.aspx?dlcid=<id>`; it is not used.

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

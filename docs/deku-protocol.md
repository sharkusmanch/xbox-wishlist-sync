# Deku Deals protocol (verified live 2026-11-01)

Everything here was observed on the real site with one test item (a cheap Xbox One game that was
on neither the wishlist nor the collection). The sequence was run end to end and reverted; the
public wishlist was the same set of slugs before and after.

## Session

- Plain HTTPS works; there is no Cloudflare challenge. Send a desktop Chrome `User-Agent`.
- `POST /login` with form fields `return_to=/`, `email`, `password` (no CSRF token). It answers
  `303`. Logged in means a page contains `/logout` (and a `user_data` script). A wrong password
  answers a page without `/logout`.
- Cookies keep the session; they are stored outside the repo (`deku-session` next to the state file, e.g. `/state/deku-session`, mode 600).
- No client follows redirects: an expired session shows up as `303` to
  `https://www.dekudeals.com/login?return_to=...` on a write, or as a page without `/logout` on
  a read. Both mean "log in again".

## Reads

- Wishlist: public share page `GET /wishlist/<id>?page=N` (120 per page; the page's own total is
  `<n> items` or `<a>-<b> of <n>` inside `div.pagination_controls`). Each card has
  `<a class='main-link' href='/items/<slug>'><h6>Title</h6></a>` and, when the entry sets one, a
  `Desired format` detail: `Digital`, `Physical` or `Physical or digital`. An entry on the
  account default shows no detail at all. The page does **not** show which platforms are ticked,
  so platforms come from the logged-in item page.
- Search: `GET /search?q=<title>`; hits are the same `main-link` anchors.
- Item page (logged in): `GET /items/<slug>`. If Deku Deals renames an item, the old slug may
  redirect to the new one (not observed live); the client follows redirects and takes the slug from
  the final URL, which is the one the share page lists.
  - `Platforms:` list item names the platforms the game exists on (`Xbox Series X|S`, `Xbox One`,
    ...). This is the only platform information for a game that is not on the wishlist.
  - The item UUID is in the form actions `/items/<uuid>/watch`.
  - A game on the wishlist has `form.watch-details-form` with `action='/items/<uuid>/watch'`.
    A game that is not has none (only the `to=true` button form). A game in the collection has a
    `watch-details-form` whose action is `/owned_items/...`; that is not the wishlist form.
  - Fields of the details form: `desired_price` radio (`""`, `drop`, `specific`, `lowest`),
    `specific_price` hidden (cents; present even when `desired_price` is not `specific`),
    `desired_format` radio (`""`, `either`, `physical`, `digital`), one `platform_<key>` per
    platform the game has on an account-enabled platform (hidden `false`, then checkbox `true`
    when ticked), `note` textarea, `note_private` and `hide_from_public_view` (hidden `false` plus
    checkbox `true`).

## Writes

All writes are `POST` with `application/x-www-form-urlencoded` and the header `render: true`
(the site's JavaScript sends it). Every successful write answers `303` with an empty body and
`Location: /items/<slug>`.

### Add

1. `POST /items/<uuid>/watch` with `to=true`.
2. The entry now exists. Defaults it was given: `desired_price=""` (account default),
   `desired_format=""` (account default), every platform checkbox the game has **ticked**,
   `note` empty, `note_private=false`, `hide_from_public_view=false`. The share page shows no
   `Desired format` for it.
3. `POST /items/<uuid>/watch` with the full details form (below) to set the format and the Xbox
   platforms. Read back the item page (and, for the format, the share page) to confirm.

`.add()` sends step 1, re-reads the item page for the entry's real settings, then sends step 3.
The two-step shape is required: `to=true` alone cannot carry a format.

### Update

`POST /items/<uuid>/watch` with the **whole** details form, current values re-sent:
`desired_price`, `specific_price`, `desired_format`, every `platform_<key>` (a hidden `false`,
then a `true` when ticked; the browser sends both), `note`, `note_private`,
`hide_from_public_view`. Only `desired_format` and the Xbox `platform_*` values change.
Unticking the last platform works and is read back as no `platform_*` ticked; ticking it again
restores it.

Format values read back on the share page: `digital` -> `Digital`, `physical` -> `Physical`,
`either` -> `Physical or digital`, `""` -> no detail.

### Remove

`POST /items/<uuid>/watch` with `to=false`. The entry and its settings disappear: the item page
goes back to having no `watch-details-form`, and the share page no longer lists the slug.

## Verified sequence (test item)

| Step | Request | Result |
|------|---------|--------|
| 1 | share page, item page | not on the wishlist; no details form |
| 2 | `to=true` | 303; entry exists with the defaults above; share page one entry longer |
| 3 | details: `digital` + Xbox box | 303; read back `digital`, box ticked; share page `Digital` |
| 4 | details: `either` + Xbox box | 303; share page `Physical or digital` |
| 5 | details: `digital`, box unticked | 303; no platform ticked |
| 6 | details: `digital` + Xbox box | 303; ticked again |
| 7 | `to=false` | 303; no details form; share page back to the original entries, same slugs |

An unauthenticated `POST` to the same URL answered `303` to the login page and changed nothing.

## Settings survive an update (verified live 2026-11-01, round 2)

Same kind of test item (not on the wishlist), driven through the finished client:

1. `add(digital, Xbox box)`, then a details POST setting `desired_price=specific`,
   `specific_price=777` and a note.
2. `update(either, Xbox box)` through the client. Read back: `desired_price`, `specific_price`
   and `note` unchanged, `desired_format=either`, box ticked. The client's read-back compares the
   whole settings dict, so any other drift raises `DekuError` naming the fields.
3. `remove`. The share page was back to the original slugs with the same formats.

Other checks: every item page on a real wishlist parses with a single consistent item uuid (watch,
hide, rating and owned forms and `data-item-id` all agree). New entries start with every platform
box ticked, so `.add()` unticks the non-Xbox boxes; `.update()` leaves non-Xbox boxes as they are.

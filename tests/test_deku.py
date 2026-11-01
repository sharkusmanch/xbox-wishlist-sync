import re
from pathlib import Path
import pytest

from wsync import deku
from wsync.deku import AuthError, DekuClient, DekuError, Response
from wsync.plan import Entry

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.dekudeals.com"
ITEM_UUID = "11111111-1111-4111-8111-111111111111"
NEW_UUID = "22222222-2222-4222-8222-222222222222"
WID = "testlist1"
SECRET = "hunter2-do-not-leak"


def fixture(name):
    return (FIX / name).read_text()


LOGGED_IN = fixture("deku_item_logged_in.html")
NOT_LISTED = fixture("deku_item_not_on_wishlist.html")
WISHLIST = fixture("deku_wishlist_page.html")
SEARCH = fixture("deku_search.html")


def redirect(to):
    return Response(303, BASE + "/x", "", to)


def with_settings(page, fmt=None, plats=None):
    """The item page as the site would render it after a save."""
    if fmt is not None:
        page = re.sub(r"<input checked (class='form-check-input' id='[^']*desired_format_[^']*' name='desired_format')",
                      r"<input \1", page)
        page = re.sub(rf"<input (class='form-check-input' id='[^']*' name='desired_format' type='radio' value='{fmt}')",
                      r"<input checked \1", page)
    if plats is not None:
        for key in ("xbox_series", "xbox_one"):
            page = re.sub(rf"(name='platform_{key}' id='[^']*' value='true')\s+checked\s*>", r"\1 >", page)
            if key in plats:
                page = re.sub(rf"(name='platform_{key}' id='[^']*' value='true')\s*>", r"\1 checked >", page)
    return page


class Fake:
    """Scripted transport: routes are (method, path-prefix) -> list of responses (last one repeats)."""

    def __init__(self, routes=None, logged_in=True):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in (routes or {}).items()}
        self.requests = []
        self.logged_in = logged_in

    def request(self, method, url, data=None, headers=None):
        self.requests.append((method, url, list(data) if data else None, dict(headers or {})))
        path = url[len(BASE):]
        for (m, prefix), queue in self.routes.items():
            if m == method and path.startswith(prefix):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"unexpected request {method} {path}")

    def posts(self):
        return [(u[len(BASE):], dict(d) if d else {}, d, h)
                for m, u, d, h in self.requests if m == "POST"]


def page(text):
    return Response(200, BASE + "/x", text, None)


def client(fake, **kw):
    return DekuClient("me@example.invalid", SECRET, WID, None, transport=fake, sleep=lambda s: None, **kw)


def login_ok():
    return {("POST", "/login"): redirect("/"), ("GET", "/"): page("<a href='/logout'>x</a>")}


# ---------------------------------------------------------------- reading


def test_wishlist_parse_maps_formats_and_keys_by_slug():
    fake = Fake({("GET", f"/wishlist/{WID}"): page(WISHLIST)})
    got = client(fake).wishlist()
    assert list(got) == ["test-game-digital", "test-game-physical", "test-game-either",
                         "test-game-default", "test-game-digital-2"]
    assert got["test-game-digital"] == Entry("test-game-digital", "Test Game: Digital", "digital", frozenset())
    assert got["test-game-physical"].format == "physical"
    assert got["test-game-either"].format == "either"
    assert got["test-game-default"].format == ""
    # entities decoded, accents kept
    assert got["test-game-physical"].title == "Test Game & Physical"
    assert got["test-game-either"].title == "Ōkami Test's Either"
    assert all(e.target == slug for slug, e in got.items())


def test_wishlist_pages_until_total_reached():
    cards = WISHLIST.split("<div class='d-block col'>")
    head, body = cards[0], cards[1:]

    def pg(chunk, counter):
        return page(head.replace("5 items", counter) + "".join("<div class='d-block col'>" + c for c in chunk))

    fake = Fake({
        ("GET", f"/wishlist/{WID}?page=1"): pg(body[:3], "1-3 of 5"),
        ("GET", f"/wishlist/{WID}?page=2"): pg(body[3:], "4-5 of 5"),
    })
    got = client(fake).wishlist()
    assert len(got) == 5
    assert [r[1] for r in fake.requests] == [f"{BASE}/wishlist/{WID}?page=1", f"{BASE}/wishlist/{WID}?page=2"]


def test_wishlist_short_read_raises_instead_of_returning_partial():
    cards = WISHLIST.split("<div class='d-block col'>")
    short = page(cards[0].replace("5 items", "1-3 of 5") + "<div class='d-block col'>" + cards[1])
    empty = page(cards[0].replace("5 items", "4-5 of 5"))
    fake = Fake({("GET", f"/wishlist/{WID}?page=1"): short, ("GET", f"/wishlist/{WID}?page=2"): empty})
    with pytest.raises(DekuError):
        client(fake).wishlist()


def test_wishlist_without_a_count_raises():
    fake = Fake({("GET", f"/wishlist/{WID}"): page("<html>Just a moment...</html>")})
    with pytest.raises(DekuError):
        client(fake).wishlist()


def test_wishlist_with_platforms_reads_each_item_page():
    fake = Fake({
        ("GET", f"/wishlist/{WID}"): page(WISHLIST.split("<div class='d-block col'>")[0].replace("5 items", "1 items")
                                           + "<div class='d-block col'>" + WISHLIST.split("<div class='d-block col'>")[2]),
        ("GET", "/items/test-game-physical"): page(LOGGED_IN),
    })
    c = client(fake)
    got = c.wishlist(with_platforms=True)
    assert got["test-game-physical"].platforms == frozenset({"xbox_series"})
    assert got["test-game-physical"].format == "physical"
    # The item pages it read are kept for reuse (offered platforms, uuid).
    assert set(c.last_items) == {"test-game-physical"}
    assert c.last_items["test-game-physical"].on_wishlist


def test_search_returns_title_slug_pairs():
    fake = Fake({("GET", "/search?q="): page(SEARCH)})
    got = client(fake).search("Hades & co")
    assert got == [("Hades", "hades"), ("Hades II", "hades-ii"),
                   ("Disney Universe Hades Costume", "disney-universe-hades-costume")]
    assert fake.requests[0][1] == f"{BASE}/search?q=Hades+%26+co"


def test_item_parse_wishlisted():
    fake = Fake({("GET", "/items/banishers"): page(LOGGED_IN)})
    it = client(fake).item("banishers")
    assert it.uuid == ITEM_UUID
    assert it.title == "Banishers: Ghosts of New Eden"
    assert it.on_wishlist is True
    assert it.platforms == frozenset({"xbox_series"})
    assert it.offered == frozenset({"xbox_series"})
    s = it.settings
    assert s["desired_price"] == "specific" and s["specific_price"] == "4499"
    assert s["desired_format"] == "physical"
    assert s["platform_xbox_series"] == "true"
    assert s["note"] == "Wait for a sale & Game Pass"
    assert s["note_private"] == "false" and s["hide_from_public_view"] == "false"


def test_item_takes_its_slug_from_the_final_url_after_a_redirect():
    # A renamed item: the old slug redirects to the new one, which is what the share page lists.
    fake = Fake({
        ("GET", "/items/old-banishers"): Response(301, BASE + "/items/old-banishers", "", "/items/banishers"),
        ("GET", "/items/banishers"): Response(200, BASE + "/items/banishers", LOGGED_IN, None),
    })
    it = client(fake).item("old-banishers")
    assert it.slug == "banishers" and it.uuid == ITEM_UUID


def test_item_without_a_redirect_keeps_the_requested_slug():
    fake = Fake({("GET", "/items/banishers"): Response(200, BASE + "/items/banishers", LOGGED_IN, None)})
    assert client(fake).item("banishers").slug == "banishers"


def test_item_parse_not_on_wishlist():
    fake = Fake({("GET", "/items/celeste"): page(NOT_LISTED)})
    it = client(fake).item("celeste")
    assert it.uuid == NEW_UUID
    assert it.on_wishlist is False
    assert it.settings == {}
    assert it.platforms == frozenset()
    assert it.offered == frozenset({"xbox_one"})


def test_item_in_collection_only_is_not_on_wishlist():
    owned = NOT_LISTED.replace(
        "<div class='dropdown add-dropdown",
        "<form action='/owned_items/33333333-3333-4333-8333-333333333333' class='watch-details-form' method='POST'>"
        "<input name='note' value='x'></form><div class='dropdown add-dropdown", 1)
    fake = Fake({("GET", "/items/celeste"): page(owned)})
    assert client(fake).item("celeste").on_wishlist is False


# ---------------------------------------------------------------- writes


def sent_details(fake):
    return [p for p in fake.posts() if "to" not in p[1] and p[0] != "/login"]


def test_update_preserves_custom_price_and_note():
    after = with_settings(LOGGED_IN, fmt="either", plats={"xbox_series"})
    fake = Fake({("GET", "/items/banishers"): [page(LOGGED_IN), page(after)],
                 ("POST", f"/items/{ITEM_UUID}/watch"): redirect("/items/banishers")})
    c = client(fake)
    item = c.item("banishers")
    c.update(item, "either", frozenset({"xbox_series"}))
    (path, fields, pairs, headers), = fake.posts()
    assert path == f"/items/{ITEM_UUID}/watch"
    assert fields["specific_price"] == "4499"
    assert fields["desired_price"] == "specific"
    assert fields["note"] == "Wait for a sale & Game Pass"
    assert fields["desired_format"] == "either"
    assert fields["note_private"] == "false" and fields["hide_from_public_view"] == "false"
    assert ("platform_xbox_series", "false") in pairs and ("platform_xbox_series", "true") in pairs
    assert headers["render"] == "true"
    assert "password" not in str(pairs)


def test_update_unticks_xbox_platforms_not_wanted_and_keeps_others():
    page_two = LOGGED_IN.replace(
        "<div class='form-group'>\n<label for=",
        "<div class='form-group'>\n <input type='hidden' name='platform_switch' value='false'>\n"
        " <input class='form-check-input' type='checkbox' name='platform_switch' id='z' value='true'  checked >\n"
        "</div>\n<div class='form-group'>\n<label for=", 1)
    assert "platform_switch" in page_two
    after = with_settings(page_two, fmt="digital", plats=set())
    fake = Fake({("GET", "/items/banishers"): [page(page_two), page(after)],
                 ("POST", f"/items/{ITEM_UUID}/watch"): redirect("/items/banishers")})
    c = client(fake)
    c.update(c.item("banishers"), "digital", frozenset())
    (_, _, pairs, _), = fake.posts()
    assert [v for k, v in pairs if k == "platform_xbox_series"] == ["false"]
    assert [v for k, v in pairs if k == "platform_switch"] == ["false", "true"]


def test_update_rejects_an_xbox_platform_the_item_lacks():
    fake = Fake({("GET", "/items/banishers"): page(LOGGED_IN)})
    c = client(fake)
    with pytest.raises(DekuError):
        c.update(c.item("banishers"), "digital", frozenset({"xbox_one"}))
    assert not fake.posts()


def test_update_rejects_unknown_format():
    fake = Fake({("GET", "/items/banishers"): page(LOGGED_IN)})
    c = client(fake)
    with pytest.raises(ValueError):
        c.update(c.item("banishers"), "dgital", frozenset())


def test_update_raises_when_read_back_differs():
    fake = Fake({("GET", "/items/banishers"): [page(LOGGED_IN), page(LOGGED_IN)],
                 ("POST", f"/items/{ITEM_UUID}/watch"): redirect("/items/banishers")})
    c = client(fake)
    with pytest.raises(DekuError, match="read back"):
        c.update(c.item("banishers"), "either", frozenset({"xbox_series"}))


def test_add_sequence_matches_protocol_doc():
    # docs/deku-protocol.md: POST to=true, re-read the item page, POST the full details form, read back.
    listed = with_settings(LOGGED_IN.replace(ITEM_UUID, NEW_UUID), fmt="", plats={"xbox_series"})
    final = with_settings(listed, fmt="digital", plats={"xbox_series"})
    fake = Fake({
        ("GET", "/items/celeste"): [page(NOT_LISTED), page(listed), page(final)],
        ("POST", f"/items/{NEW_UUID}/watch"): redirect("/items/celeste"),
    })
    c = client(fake)
    item = c.item("celeste")
    c.add(item, "digital", frozenset({"xbox_series"}))
    steps = [(m, u[len(BASE):]) for m, u, _, _ in fake.requests]
    assert steps == [
        ("GET", "/items/celeste"),
        ("POST", f"/items/{NEW_UUID}/watch"),
        ("GET", "/items/celeste"),
        ("POST", f"/items/{NEW_UUID}/watch"),
        ("GET", "/items/celeste"),
    ]
    first, second = fake.posts()
    assert first[2] == [("to", "true")] and first[3]["render"] == "true"
    assert second[1]["desired_format"] == "digital" and "to" not in second[1]
    assert ("platform_xbox_series", "true") in second[2]
    assert second[3]["render"] == "true"


def test_add_refuses_an_item_already_on_the_wishlist():
    fake = Fake({("GET", "/items/banishers"): page(LOGGED_IN)})
    c = client(fake)
    with pytest.raises(DekuError):
        c.add(c.item("banishers"), "digital", frozenset({"xbox_series"}))
    assert not fake.posts()


def test_add_fails_when_entry_does_not_appear():
    fake = Fake({("GET", "/items/celeste"): [page(NOT_LISTED), page(NOT_LISTED)],
                 ("POST", f"/items/{NEW_UUID}/watch"): redirect("/items/celeste")})
    c = client(fake)
    with pytest.raises(DekuError):
        c.add(c.item("celeste"), "digital", frozenset({"xbox_one"}))


def test_remove_posts_to_false_and_verifies():
    fake = Fake({("GET", "/items/banishers"): [page(LOGGED_IN), page(NOT_LISTED.replace(NEW_UUID, ITEM_UUID))],
                 ("POST", f"/items/{ITEM_UUID}/watch"): redirect("/items/banishers")})
    c = client(fake)
    c.remove(c.item("banishers"))
    (path, fields, pairs, headers), = fake.posts()
    assert pairs == [("to", "false")] and headers["render"] == "true"


def test_remove_raises_if_entry_still_there():
    fake = Fake({("GET", "/items/banishers"): [page(LOGGED_IN), page(LOGGED_IN)],
                 ("POST", f"/items/{ITEM_UUID}/watch"): redirect("/items/banishers")})
    c = client(fake)
    with pytest.raises(DekuError):
        c.remove(c.item("banishers"))


# ---------------------------------------------------------------- session


def test_login_posts_form_and_checks_for_logout():
    fake = Fake(login_ok(), logged_in=False)
    client(fake).login()
    (path, fields, pairs, _), = fake.posts()
    assert path == "/login"
    assert fields == {"return_to": "/", "email": "me@example.invalid", "password": SECRET}


def test_login_failure_raises_without_leaking_password():
    fake = Fake({("POST", "/login"): page("<form>bad password</form>"), ("GET", "/"): page("<html>no</html>")})
    with pytest.raises(AuthError) as e:
        client(fake).login()
    assert SECRET not in str(e.value) and "me@example.invalid" not in str(e.value)


def test_client_repr_hides_password():
    assert SECRET not in repr(client(Fake()))


def test_item_page_without_session_logs_in_once_and_retries():
    fake = Fake({
        ("GET", "/items/banishers"): [page("<html>public page</html>"), page(LOGGED_IN)],
        **login_ok(),
    })
    it = client(fake).item("banishers")
    assert it.uuid == ITEM_UUID
    assert [p[0] for p in fake.posts()] == ["/login"]


def test_reauth_once_on_write_then_succeeds():
    after = with_settings(LOGGED_IN, fmt="either", plats={"xbox_series"})
    fake = Fake({
        ("GET", "/items/banishers"): [page(LOGGED_IN), page(after)],
        ("POST", f"/items/{ITEM_UUID}/watch"): [redirect(BASE + "/login?return_to=%2F"), redirect("/items/banishers")],
        **login_ok(),
    })
    c = client(fake)
    c.update(c.item("banishers"), "either", frozenset({"xbox_series"}))
    assert [p[0] for p in fake.posts()] == [f"/items/{ITEM_UUID}/watch", "/login", f"/items/{ITEM_UUID}/watch"]


def test_reauth_once_then_raise():
    fake = Fake({
        ("GET", "/items/banishers"): page(LOGGED_IN),
        ("POST", f"/items/{ITEM_UUID}/watch"): redirect(BASE + "/login?return_to=%2F"),
        **login_ok(),
    })
    c = client(fake)
    item = c.item("banishers")
    with pytest.raises(AuthError):
        c.remove(item)
    # one write, one login, one retry; no third attempt
    assert [p[0] for p in fake.posts()] == [f"/items/{ITEM_UUID}/watch", "/login", f"/items/{ITEM_UUID}/watch"]


def test_http_401_counts_as_auth_failure():
    fake = Fake({
        ("GET", "/items/banishers"): page(LOGGED_IN),
        ("POST", f"/items/{ITEM_UUID}/watch"): Response(401, BASE, "", None),
        **login_ok(),
    })
    c = client(fake)
    with pytest.raises(AuthError):
        c.remove(c.item("banishers"))


def test_server_error_on_write_is_a_deku_error_not_auth():
    fake = Fake({
        ("GET", "/items/banishers"): page(LOGGED_IN),
        ("POST", f"/items/{ITEM_UUID}/watch"): Response(500, BASE, "oops", None),
    })
    c = client(fake)
    with pytest.raises(DekuError) as e:
        c.remove(c.item("banishers"))
    assert not isinstance(e.value, AuthError)


def test_user_agent_is_desktop_chrome():
    assert "Chrome/" in deku.USER_AGENT and "Mozilla/5.0" in deku.USER_AGENT


def test_cookie_file_is_private(tmp_path):
    import http.cookiejar
    path = tmp_path / "dekudeals" / "cookies.txt"
    t = deku.UrllibTransport(path)
    t._jar.set_cookie(http.cookiejar.Cookie(
        0, "s", "v", None, False, "www.dekudeals.com", True, False, "/", True, True, None, True, None, None, {}))
    t._save()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"


# ---------------------------------------------------------------- review round 1

WITH_SWITCH = LOGGED_IN.replace(
    "<div class='form-group'>\n<label for=",
    "<div class='form-group'>\n <input type='hidden' name='platform_switch' value='false'>\n"
    " <input class='form-check-input' type='checkbox' name='platform_switch' id='z' value='true'  checked >\n"
    "</div>\n<div class='form-group'>\n<label for=", 1)


def test_update_read_back_catches_a_changed_price_or_note_by_field_name_only():
    changed = with_settings(LOGGED_IN, fmt="either", plats={"xbox_series"})
    changed = changed.replace("value='4499'", "value='1234'").replace("Wait for a sale &amp; Game Pass", "lost")
    fake = Fake({("GET", "/items/banishers"): [page(LOGGED_IN), page(changed)],
                 ("POST", f"/items/{ITEM_UUID}/watch"): redirect("/items/banishers")})
    c = client(fake)
    with pytest.raises(DekuError) as e:
        c.update(c.item("banishers"), "either", frozenset({"xbox_series"}))
    msg = str(e.value)
    assert "specific_price" in msg and "note" in msg
    assert "4499" not in msg and "1234" not in msg and "lost" not in msg and "Game Pass" not in msg


def test_update_read_back_passes_when_everything_is_preserved():
    after = with_settings(LOGGED_IN, fmt="either", plats={"xbox_series"})
    fake = Fake({("GET", "/items/banishers"): [page(LOGGED_IN), page(after)],
                 ("POST", f"/items/{ITEM_UUID}/watch"): redirect("/items/banishers")})
    c = client(fake)
    c.update(c.item("banishers"), "either", frozenset({"xbox_series"}))


OTHER_UUID = "99999999-9999-4999-8999-999999999999"
OTHER_FORM = (f"<form action='/items/{OTHER_UUID}/watch' class='watch-form btn-group' method='POST'>"
              "<input name='to' type='hidden' value='true'></form>\n")


def test_item_page_naming_two_uuids_raises():
    mixed = LOGGED_IN.replace("<div class='watch-buttons'", OTHER_FORM + "<div class='watch-buttons'", 1)
    assert OTHER_FORM in mixed
    fake = Fake({("GET", "/items/banishers"): page(mixed)})
    with pytest.raises(DekuError, match="more than one"):
        client(fake).item("banishers")


@pytest.mark.parametrize("extra", [
    f"<button class='remove-watch-button' data-item-id='{OTHER_UUID}'></button>",
    f"<form action='/owned_items' method='POST'><input name='item_id' type='hidden' value='{OTHER_UUID}'></form>",
])
def test_other_uuid_sources_are_cross_checked(extra):
    fake = Fake({("GET", "/items/banishers"): page(LOGGED_IN.replace("</body>", extra + "</body>"))})
    with pytest.raises(DekuError, match="more than one"):
        client(fake).item("banishers")


def test_consistent_uuid_sources_are_fine():
    extra = (f"<button class='remove-watch-button' data-item-id='{ITEM_UUID}'></button>"
             f"<form action='/owned_items' method='POST'><input name='item_id' type='hidden' value='{ITEM_UUID}'></form>")
    fake = Fake({("GET", "/items/banishers"): page(LOGGED_IN.replace("</body>", extra + "</body>"))})
    assert client(fake).item("banishers").uuid == ITEM_UUID


@pytest.mark.parametrize("status", [404, 429, 500, 503])
def test_http_errors_on_reads_are_deku_errors_not_auth(status):
    fake = Fake({("GET", "/items/banishers"): Response(status, BASE, "<html>nope</html>", None),
                 ("GET", f"/wishlist/{WID}"): Response(status, BASE, "<html>nope</html>", None)})
    c = client(fake)
    for call in (lambda: c.item("banishers"), lambda: c.wishlist()):
        with pytest.raises(DekuError, match=str(status)) as e:
            call()
        assert not isinstance(e.value, AuthError)
        assert WID not in str(e.value)
    assert not fake.posts()


def test_login_http_error_is_not_a_credentials_error():
    fake = Fake({("POST", "/login"): Response(429, BASE, "slow down", None)})
    with pytest.raises(DekuError, match="429") as e:
        client(fake).login()
    assert not isinstance(e.value, AuthError)


def test_wishlist_id_never_appears_in_errors():
    fake = Fake({("GET", f"/wishlist/{WID}"): Response(404, BASE, "", None)})
    with pytest.raises(DekuError) as e:
        client(fake).wishlist()
    assert WID not in str(e.value) and "<id>" in str(e.value)


def test_page_html_and_notes_stay_out_of_reprs():
    fake = Fake({("GET", "/items/banishers"): page(LOGGED_IN)})
    item = client(fake).item("banishers")
    assert "Game Pass" not in repr(item) and "4499" not in repr(item)
    assert "Game Pass" not in repr(page(LOGGED_IN))


def test_add_unticks_non_xbox_boxes_but_update_keeps_them():
    # New entries start with every box ticked; add() keeps only Xbox, update() leaves others alone.
    new_uuid_page = WITH_SWITCH.replace(ITEM_UUID, NEW_UUID)
    listed = with_settings(new_uuid_page, fmt="", plats={"xbox_series"})
    final = with_settings(listed, fmt="digital", plats={"xbox_series"}).replace(
        "name='platform_switch' id='z' value='true'  checked >", "name='platform_switch' id='z' value='true'  >")
    fake = Fake({("GET", "/items/celeste"): [page(NOT_LISTED), page(listed), page(final)],
                 ("POST", f"/items/{NEW_UUID}/watch"): redirect("/items/celeste")})
    c = client(fake)
    c.add(c.item("celeste"), "digital", frozenset({"xbox_series"}))
    _, details = fake.posts()
    pairs = details[2]
    assert [v for k, v in pairs if k == "platform_switch"] == ["false"]
    assert [v for k, v in pairs if k == "platform_xbox_series"] == ["false", "true"]

    after = with_settings(WITH_SWITCH, fmt="digital", plats={"xbox_series"})
    fake = Fake({("GET", "/items/banishers"): [page(WITH_SWITCH), page(after)],
                 ("POST", f"/items/{ITEM_UUID}/watch"): redirect("/items/banishers")})
    c = client(fake)
    c.update(c.item("banishers"), "digital", frozenset({"xbox_series"}))
    (_, _, pairs, _), = fake.posts()
    assert [v for k, v in pairs if k == "platform_switch"] == ["false", "true"]


def test_existing_session_dir_is_not_chmodded(tmp_path):
    import http.cookiejar
    d = tmp_path / "shared"
    d.mkdir(mode=0o755)
    t = deku.UrllibTransport(d / "cookies.txt")
    t._jar.set_cookie(http.cookiejar.Cookie(
        0, "s", "v", None, False, "www.dekudeals.com", True, False, "/", True, True, None, True, None, None, {}))
    t._save()
    assert oct(d.stat().st_mode & 0o777) == "0o755"
    assert oct((d / "cookies.txt").stat().st_mode & 0o777) == "0o600"


def test_session_file_errors_become_deku_errors(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    t = deku.UrllibTransport(blocker / "sub" / "cookies.txt")
    with pytest.raises(DekuError):
        t._save()


def test_rate_limited_get_backs_off_and_retries():
    waits = []
    fake = Fake({("GET", "/search?q="): [Response(429, BASE + "/x", "slow down", None), page(SEARCH)]})
    c = DekuClient("e", SECRET, WID, None, transport=fake, sleep=waits.append)
    assert c.search("Hades")[0] == ("Hades", "hades")
    assert deku.RATE_LIMIT_WAITS[0] in waits


def test_rate_limit_that_persists_is_an_error():
    fake = Fake({("GET", "/search?q="): Response(429, BASE + "/x", "slow down", None)})
    c = DekuClient("e", SECRET, WID, None, transport=fake, sleep=lambda s: None)
    with pytest.raises(DekuError, match="429"):
        c.search("Hades")
    assert len(fake.requests) == len(deku.RATE_LIMIT_WAITS) + 1

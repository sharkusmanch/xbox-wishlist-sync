import json
import os
import stat

import pytest

from wsync import xbox
from wsync.xbox import SignInNeeded, TokenStore, XboxError

XUI = {"uhs": "uhs-1", "xid": "2533270000000001", "gtg": "Example Gamer"}


def tok(token, uhs="uhs-1", **extra):
    return 200, {"Token": token, "DisplayClaims": {"xui": [dict(XUI, uhs=uhs, **extra)]}}


class FakeHttp:
    """Answers by URL; records calls and runs an optional hook before each Xbox Live call."""

    def __init__(self, refresh, on_xbl=None):
        self.refresh = list(refresh)  # answers to token requests, in order
        self.calls = []
        self.on_xbl = on_xbl
        self.xsts = {xbox.RP_LIVE: tok("xsts-live"), xbox.RP_STORE: tok("xsts-store")}

    def request(self, method, url, *, form=None, json_body=None, headers=None):
        self.calls.append((method, url, form, json_body))
        if url.endswith("/token"):
            return self.refresh.pop(0)
        if self.on_xbl:
            self.on_xbl()
        if url == xbox.XBL_URL:
            return tok("user-1")
        return self.xsts[json_body["RelyingParty"]]


OK = (200, {"access_token": "access-1", "refresh_token": "rt-new"})
REJECTED = (400, {"error": "invalid_grant"})


def test_xbl3_header_joins_user_hash_and_token():
    assert xbox.xbl3("U1", "TOK") == "XBL3.0 x=U1;TOK"
    s = xbox.Session("U1", "LIVE", "STORE", "1", "G")
    assert s.headers("store")["Authorization"] == "XBL3.0 x=U1;STORE"
    assert s.headers("live", 2)["Authorization"] == "XBL3.0 x=U1;LIVE"
    assert s.headers("live", 2)["x-xbl-contract-version"] == "2"


def test_refresh_keeps_the_old_refresh_token_when_none_is_returned():
    http = FakeHttp([(200, {"access_token": "access-2"})])
    assert xbox.refresh_access(http, "rt-old") == {"access_token": "access-2", "refresh_token": "rt-old"}
    form = http.calls[0][2]
    assert form["client_id"] == xbox.CLIENT_ID and form["scope"] == xbox.SCOPE


def test_xsts_error_names_a_missing_xbox_profile():
    http = FakeHttp([])
    http.xsts[xbox.RP_STORE] = (401, {"XErr": 2148916233})
    with pytest.raises(XboxError, match="no Xbox profile"):
        xbox.xsts_token(http, "user-1", xbox.RP_STORE)


def test_xsts_token_returns_token_and_claims():
    http = FakeHttp([])
    token, claims = xbox.xsts_token(http, "user-1", xbox.RP_LIVE)
    assert token == "xsts-live" and claims["gtg"] == "Example Gamer"
    assert http.calls[0][3]["Properties"]["UserTokens"] == ["user-1"]


def test_open_session_builds_a_session(tmp_path):
    store = TokenStore(tmp_path / "t.json", "rt-seed")
    s = xbox.open_session(FakeHttp([OK]), store, [].append)
    assert (s.uhs, s.live_token, s.store_token) == ("uhs-1", "xsts-live", "xsts-store")
    assert (s.xuid, s.gamertag) == ("2533270000000001", "Example Gamer")
    assert "rt-seed" not in repr(s) and "xsts-live" not in repr(s)


def test_a_rotated_refresh_token_is_saved_before_the_xbox_live_calls(tmp_path):
    path = tmp_path / "t.json"
    TokenStore(path, "").save("rt-old", "file")
    seen = []
    http = FakeHttp([OK], on_xbl=lambda: seen.append(json.loads(path.read_text())["refresh_token"]))
    xbox.open_session(http, TokenStore(path, ""), [].append)
    assert seen and set(seen) == {"rt-new"}


def test_the_file_token_comes_first_and_the_seed_only_when_new(tmp_path):
    path = tmp_path / "t.json"
    store = TokenStore(path, "seed-1")
    assert store.candidates() == [("seed", "seed-1")]
    store.save("rt-a", "seed")  # seed-1 used and recorded
    assert store.candidates() == [("file", "rt-a")]
    store.save("rt-b", "file")  # a file refresh keeps the seed fingerprint
    assert store.candidates() == [("file", "rt-b")]
    # file token rejected, seed is the last one used: sign-in needed
    with pytest.raises(SignInNeeded, match="XBOX_REFRESH_TOKEN"):
        xbox.open_session(FakeHttp([REJECTED]), TokenStore(path, "seed-1"), [].append)
    # a new seed is tried after the rejected file token, used, and recorded
    new = TokenStore(path, "seed-2")
    assert new.candidates() == [("file", "rt-b"), ("seed", "seed-2")]
    http = FakeHttp([REJECTED, OK])
    xbox.open_session(http, new, [].append)
    assert [c[2]["refresh_token"] for c in http.calls if c[2]] == ["rt-b", "seed-2"]
    assert TokenStore(path, "seed-2").candidates() == [("file", "rt-new")]


def test_no_file_and_no_seed_needs_sign_in(tmp_path):
    with pytest.raises(SignInNeeded):
        xbox.open_session(FakeHttp([]), TokenStore(tmp_path / "t.json", ""), [].append)


def test_the_token_file_is_owner_only_and_holds_no_seed(tmp_path):
    path = tmp_path / "sub" / "t.json"
    TokenStore(path, "seed-secret").save("rt-new", "seed")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "seed-secret" not in path.read_text()
    assert not list(path.parent.glob("*.tmp"))


def test_every_token_goes_to_the_scrubber(tmp_path):
    secrets = []
    xbox.open_session(FakeHttp([OK]), TokenStore(tmp_path / "t.json", "rt-seed"), secrets.append)
    assert set(secrets) >= {"rt-seed", "rt-new", "access-1", "user-1", "xsts-live", "xsts-store"}


def test_a_server_error_is_a_failed_read_not_a_sign_in(tmp_path):
    store = TokenStore(tmp_path / "t.json", "rt-seed")
    with pytest.raises(XboxError) as e:
        xbox.open_session(FakeHttp([(503, {})]), store, [].append)
    assert not isinstance(e.value, SignInNeeded)


def test_a_400_without_an_oauth_error_is_a_failed_read(tmp_path):
    with pytest.raises(XboxError) as e:
        xbox.open_session(FakeHttp([(400, {})]), TokenStore(tmp_path / "t.json", "s"), [].append)
    assert not isinstance(e.value, SignInNeeded)


# ---- Collections, orders and the Store catalog

SESSION = xbox.Session("U1", "LIVE", "STORE", "2533270000000001", "Example Gamer")


class Pages:
    """Answers each request with the next (status, body); a raised exception is thrown instead."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, method, url, *, form=None, json_body=None, headers=None):
        self.calls.append((method, url, json_body, headers))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def test_collections_follow_continuation_tokens_with_an_xuid_beneficiary():
    http = Pages((200, {"items": [{"productId": "A"}], "continuationToken": "NEXT"}),
                 (200, {"items": [{"productId": "B"}]}))
    items = xbox.fetch_collections(http, SESSION, market="US")
    assert [i["productId"] for i in items] == ["A", "B"]
    assert http.calls[0][0:2] == ("POST", xbox.COLLECTIONS_URL)
    assert "continuationToken" not in http.calls[0][2]
    body = http.calls[1][2]
    assert body["continuationToken"] == "NEXT"
    assert body["maxPageSize"] == 100 and body["excludeDuplicates"] is True
    assert body["market"] == "US" and body["validityType"] == "All"
    assert body["beneficiaries"][0]["identityType"] == "xuid"
    assert body["beneficiaries"][0]["identityValue"] == SESSION.xuid
    assert body["beneficiaries"][0]["localTicketReference"]
    assert http.calls[0][3]["Authorization"] == "XBL3.0 x=U1;STORE"


def test_orders_follow_next_links():
    http = Pages((200, {"items": [{"orderId": "1"}], "@nextLink": "https://purchase.example/page2"}),
                 (200, {"items": [{"orderId": "2"}]}))
    assert [o["orderId"] for o in xbox.fetch_orders(http, SESSION)] == ["1", "2"]
    assert http.calls[0][0:2] == ("GET", xbox.ORDERS_URL)
    assert "?" not in http.calls[0][1]  # no market or language parameters
    assert http.calls[1][1] == "https://purchase.example/page2"
    assert http.calls[1][3]["Authorization"] == "XBL3.0 x=U1;STORE"


def test_a_failed_page_raises_instead_of_returning_part():
    failing = Pages((200, {"items": [{"orderId": "1"}], "@nextLink": "https://purchase.example/page2?t=SECRET"}),
                    (500, {}))
    with pytest.raises(XboxError) as e:
        xbox.fetch_orders(failing, SESSION)
    assert "SECRET" not in str(e.value) and "purchase.example" not in str(e.value)
    with pytest.raises(XboxError):
        xbox.fetch_collections(Pages((200, {"items": [{"productId": "A"}], "continuationToken": "N"}),
                                     XboxError("network error: URLError")), SESSION)
    with pytest.raises(XboxError):
        xbox.fetch_collections(Pages((200, {"items": "oops"})), SESSION)
    with pytest.raises(XboxError):
        xbox.catalog_lookup(Pages((200, {"Products": []}), (503, {})), [f"P{i}" for i in range(25)])


def product(pid, title="Example Quest", gen="absent", platforms=(), title_id=None):
    p = {"ProductId": pid, "ProductType": "Game", "LocalizedProperties": [{"ProductTitle": title}]}
    if title_id:
        p["AlternateIds"] = [{"IdType": "XboxTitleId", "Value": title_id}]
    if gen != "absent":
        p["Properties"] = {"XboxConsoleGenOptimized": gen}
    if platforms:
        p["DisplaySkuAvailabilities"] = [{"Sku": {"Properties": {"Packages": [
            {"PlatformDependencies": [{"PlatformName": n}]} for n in platforms]}}}]
    return p


def test_catalog_lookup_batches_twenty_ids_per_request_and_reads_the_title_id():
    pids = [f"9EXAMPLE{i:04d}" for i in range(45)]
    pages = [(200, {"Products": [product(p, f"Quest {p}", title_id="123") for p in pids[n * 20:(n + 1) * 20]]})
             for n in range(3)]
    http = Pages(*pages)
    cat = xbox.catalog_lookup(http, pids)
    assert len(http.calls) == 3
    assert cat[pids[44]] == xbox.Product(pids[44], f"Quest {pids[44]}", "Game", "123", (), ())
    first = http.calls[0][1]
    assert first.startswith(xbox.CATALOG_URL + "?")
    assert "market=US" in first and "languages=en-US" in first
    assert ",".join(pids[:20]) in first.replace("%2C", ",") and pids[20] not in first
    assert http.calls[0][3] is None or "Authorization" not in http.calls[0][3]


def test_catalog_lookup_reads_platforms_from_the_sku_packages():
    http = Pages((200, {"Products": [product("9EXAMPLE0003", platforms=["Windows.Desktop", "Windows.Xbox"])]}))
    assert xbox.catalog_lookup(http, ["9EXAMPLE0003"])["9EXAMPLE0003"].platforms == ("PC", "Xbox")


def test_catalog_reads_the_console_generation():
    http = Pages((200, {"Products": [
        product("9EXAMPLE0001", gen=["ConsoleGen8", "ConsoleGen9"]),
        product("9EXAMPLE0002", gen=None),
        product("9EXAMPLE0004")]}))
    products = xbox.catalog_lookup(http, ["9EXAMPLE0001", "9EXAMPLE0002", "9EXAMPLE0004", "9EXAMPLE0005"])
    assert products["9EXAMPLE0001"].gen == ("ConsoleGen8", "ConsoleGen9")
    assert products["9EXAMPLE0002"].gen == ()  # null: a PC-only app
    assert products["9EXAMPLE0004"].gen == ()
    assert "9EXAMPLE0005" not in products  # not returned by the catalog: absent, not a failure

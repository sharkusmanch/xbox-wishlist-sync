import json
import os
import stat
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from wsync import ta
from wsync.errors import TAError as BaseTAError
from wsync.flaresolverr import FlareSolverr, FlareSolverrError, Solution
from wsync.plan import Entry
from wsync.ta import (
    AlreadyListed,
    AuthError,
    HttpResponse,
    IncompleteRead,
    TAClient,
    TAError,
    TAGame,
    choose_ta_page,
    parse_dlc,
    parse_game,
    parse_search,
    parse_wishlist,
)

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.trueachievements.com"
TAG = "Test Gamer"
SECRET = "hunter2-do-not-leak"
HASH = "hash-cookie-value"
TOKEN = "gamer-token-value"
CLEAR = "cf-clearance-value"
UA = "Mozilla/5.0 (test) Chrome/142"
SECRETS = (SECRET, HASH, TOKEN, "sess-value", "txtPassword", "GamerToken", "HashKey")


def fixture(name):
    return (FIX / name).read_text()


LOGIN = fixture("ta_login.html")
WISHLIST = fixture("ta_wishlist.html")
SEARCH = fixture("ta_search.html")
SEARCH_V2 = fixture("ta_search_v2.html")
GAME = fixture("ta_game_page.html")
DLC = fixture("ta_dlc_page.html")
GATED = "<html><body class=\"ta\"><main>Please log in to view this page</main></body></html>"
LOGGED_IN = "<html><body class='ta g-li d'></body></html>"
# The logged-out wishlist: public, but without the owner's heading or remove buttons.
PUBLIC_WISHLIST = (
    WISHLIST.replace('class="ta g-li d"', 'class="ta"')
    .replace(">My Wish List<", ">Test Gamer's Wishlist<")
)


def resp(html="", cookies=None, status=200, location=None, headers=()):
    h = [("set-cookie", f"{k}={v}; path=/; secure; HttpOnly") for k, v in (cookies or {}).items()]
    if location:
        h.append(("location", location))
    return HttpResponse(status, html, tuple(h) + tuple(headers))


CHALLENGE = resp("<html><title>Just a moment...</title></html>", status=403, headers=[("cf-mitigated", "challenge")])


def sol(html="<html></html>", cookies=None, url=BASE + "/", status=200, user_agent=UA):
    return {
        "status": "ok",
        "message": "",
        "solution": {
            "url": url,
            "status": status,
            "response": html,
            "cookies": [{"name": k, "value": v} for k, v in (cookies or {}).items()],
            "userAgent": user_agent,
        },
    }


class FakeFS:
    """Scripted FlareSolverr transport; records every payload it is sent."""

    def __init__(self, answers=None):
        self.answers = list(answers or [sol(cookies={"cf_clearance": CLEAR, "anon": "x"})])
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        out = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return out(payload) if callable(out) else out


class FakeHttp:
    """Scripted curl_cffi stand-in: routes are (method, url-prefix) -> responses (the last repeats).

    A prefix starting with "=" matches that URL exactly.
    """

    def __init__(self, routes):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.calls = []

    def request(self, method, url, body, headers):
        cookie = headers.get("Cookie", "")
        cookies = dict(kv.split("=", 1) for kv in cookie.split("; ")) if cookie else {}
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers), "cookies": cookies})
        for (m, prefix), queue in self.routes.items():
            exact = prefix.startswith("=")
            if method == m and (url == BASE + prefix[1:] if exact else url.startswith(BASE + prefix)):
                out = queue.pop(0) if len(queue) > 1 else queue[0]
                return out(url, body, headers) if callable(out) else out
        raise AssertionError(f"unexpected request {method} {url}")

    def urls(self, method="GET"):
        return [c["url"][len(BASE):] for c in self.calls if c["method"] == method]


def ok_d(**kw):
    d = {"__type": "cbAjaxFunctions+GenericAjaxResponse", "success": True, "errors": None, "information": "", "html": ""}
    d.update(kw)
    return {"d": d}


def ajax(*answers):
    """AJAX answers as (status, payload) pairs, in order (the last repeats)."""
    return [resp(p if isinstance(p, str) else json.dumps(p), status=s) for s, p in answers]


def login_routes(cookies=None):
    after = {"GamerToken": TOKEN, "HashKey": HASH, "GamerID": "42"}
    after.update(cookies or {})
    return {
        ("GET", "/login"): resp(LOGIN, {"ASP.NET_SessionId": "sess-value"}),
        ("POST", "/login"): resp("", after, status=302, location="/"),
        ("GET", "=/"): resp(LOGGED_IN),
    }


def client(http, fs=None, session_path=None, **kw):
    return TAClient(TAG, SECRET, FlareSolverr("http://fs.invalid/v1", transport=fs or FakeFS()),
                    session_path, http=http, **kw)


def logged_in(routes, fs=None, **kw):
    """A client that already holds a session (no login round trip)."""
    http = FakeHttp(routes)
    c = client(http, fs, **kw)
    c._cookies = {"GamerToken": TOKEN, "HashKey": HASH, "GamerID": "42"}
    return c, http


def assert_anonymous(fs):
    """FlareSolverr logs request bodies and cookies: it must only ever see anonymous GETs."""
    for payload in fs.calls:
        assert set(payload) <= {"cmd", "url", "maxTimeout"}, payload.keys()
        assert payload["cmd"] == "request.get"
        assert "cookies" not in payload and "postData" not in payload
        text = json.dumps(payload)
        assert not any(s in text for s in SECRETS)
        assert "Test" not in text and "gamer" not in payload["url"].lower()


# ---------------------------------------------------------------- FlareSolverr


def test_flaresolverr_get_is_anonymous_and_returns_solution():
    fs = FakeFS([sol(GAME, {"cf_clearance": CLEAR}, url=BASE + "/game/X")])
    s = FlareSolverr("http://fs.invalid/v1", transport=fs).get(BASE + "/game/X")
    assert fs.calls == [{"cmd": "request.get", "url": BASE + "/game/X", "maxTimeout": 90000}]
    assert s.html == GAME and s.cookies == {"cf_clearance": CLEAR} and s.user_agent == UA
    assert s.url == BASE + "/game/X" and s.status == 200
    assert CLEAR not in repr(s) and "Kingdom" not in repr(s)


def test_flaresolverr_client_cannot_send_cookies_or_a_body():
    f = FlareSolverr("http://fs.invalid/v1", transport=FakeFS())
    assert not hasattr(f, "post")
    with pytest.raises(TypeError):
        f.get(BASE + "/x", {"GamerToken": TOKEN})
    with pytest.raises(TypeError):
        f.get(BASE + "/x", cookies={"GamerToken": TOKEN})


def test_flaresolverr_errors_share_the_ta_error_base_and_hide_the_url():
    assert issubclass(FlareSolverrError, BaseTAError) and issubclass(FlareSolverrError, TAError)
    assert TAError is BaseTAError
    s = Solution("<html>", {}, UA, url=BASE + "/gamer/Test+Gamer/wishlist")
    assert "Test" not in repr(s)


def test_flaresolverr_http_exception_is_a_flaresolverr_error(monkeypatch):
    import http.client
    import urllib.request

    def broken(*a, **kw):
        raise http.client.IncompleteRead(b"")  # an HTTPException, not an OSError

    monkeypatch.setattr(urllib.request, "urlopen", broken)
    with pytest.raises(FlareSolverrError):
        FlareSolverr("http://fs.invalid/v1").get(BASE + "/x")


def test_flaresolverr_error_status_raises_without_leaking_the_url():
    def boom(payload):
        return {"status": "error", "message": f"Error solving the challenge. {payload['url']} timed out"}

    with pytest.raises(FlareSolverrError) as e:
        FlareSolverr("http://fs.invalid/v1", transport=FakeFS([boom])).get(BASE + "/secret-path")
    assert BASE not in str(e.value) and "secret-path" not in str(e.value)


# ---------------------------------------------------------------- transport


def test_set_cookie_parsing():
    assert ta._set_cookie("A=1; path=/; HttpOnly") == ("A", "1")
    assert ta._set_cookie("A=; expires=Thu, 01-Jan-1970 00:00:01 GMT; path=/") == ("A", None)
    assert ta._set_cookie("A=1; Max-Age=0") == ("A", None)
    assert ta._set_cookie("A=1; expires=Fri, 01-Jan-2100 00:00:00 GMT") == ("A", "1")
    assert ta._set_cookie("garbage") is None


def test_challenge_detection():
    assert ta.challenged(CHALLENGE)
    assert ta.challenged(resp(status=403))
    assert ta.challenged(resp("<title>Just a moment...</title>", status=503))
    assert not ta.challenged(resp("Service Unavailable", status=503))
    assert not ta.challenged(resp(GAME))


def test_curl_transport_uses_chrome_and_never_follows_redirects(monkeypatch):
    import curl_cffi.requests as cr

    seen = {}

    class R:
        status_code = 302
        text = ""

        class headers:
            @staticmethod
            def multi_items():
                return [("Set-Cookie", "A=1"), ("Location", "/x")]

    def fake(method, url, **kw):
        seen.update(kw, method=method, url=url)
        return R()

    monkeypatch.setattr(cr, "request", fake)
    r = ta.CurlTransport().request("POST", BASE + "/login", "a=b", {"Cookie": "A=1"})
    assert seen["impersonate"] == "chrome" and seen["allow_redirects"] is False
    assert seen["data"] == "a=b" and seen["headers"] == {"Cookie": "A=1"} and "cookies" not in seen
    assert r.status == 302 and r.header_list("set-cookie") == ["A=1"] and r.header("location") == "/x"


# ---------------------------------------------------------------- Cloudflare clearance


def test_challenge_fetches_an_anonymous_clearance_and_retries_with_it():
    fs = FakeFS()
    c, http = logged_in({("GET", "/game/X"): [CHALLENGE, resp(GAME)], ("GET", "/game/Y"): resp(GAME)}, fs=fs)
    assert c.game("X").id == 13982
    assert len(fs.calls) == 1 and fs.calls[0]["url"] == ta.CLEARANCE_URL
    first, retry = http.calls
    assert "cf_clearance" not in first["cookies"] and "User-Agent" not in first["headers"]
    assert retry["cookies"]["cf_clearance"] == CLEAR and retry["headers"]["User-Agent"] == UA
    assert retry["cookies"]["GamerToken"] == TOKEN
    assert "anon" not in retry["cookies"]  # FlareSolverr's own anonymous cookies are not mixed in
    c.game("Y")  # the clearance is reused, not fetched per request
    assert len(fs.calls) == 1 and http.calls[-1]["cookies"]["cf_clearance"] == CLEAR
    assert_anonymous(fs)


def test_still_challenged_after_a_fresh_clearance_is_a_ta_error():
    fs = FakeFS()
    c, http = logged_in({("GET", "/game/X"): CHALLENGE}, fs=fs)
    with pytest.raises(TAError) as e:
        c.game("X")
    assert not isinstance(e.value, AuthError) and "403" in str(e.value)
    assert len(http.calls) == 2 and len(fs.calls) == 1
    # the next request may refresh once more (bounded per request)
    with pytest.raises(TAError):
        c.game("X")
    assert len(http.calls) == 4 and len(fs.calls) == 2


def test_clearance_fetch_failure_is_a_ta_error():
    fs = FakeFS([{"status": "error", "message": "timeout"}])
    c, _ = logged_in({("GET", "/game/X"): CHALLENGE}, fs=fs)
    with pytest.raises(TAError):
        c.game("X")


def test_flaresolverr_never_sees_credentials_or_session_cookies_across_a_whole_run():
    """Login, wishlist, search, game, DLC, D2D page and writes, each meeting a challenge first."""
    fs = FakeFS()
    routes = {
        ("GET", "/login"): [CHALLENGE, resp(LOGIN, {"ASP.NET_SessionId": "sess-value"})],
        ("POST", "/login"): [CHALLENGE, resp("", {"GamerToken": TOKEN, "HashKey": HASH}, status=302, location="/")],
        ("GET", "=/"): resp(LOGGED_IN),
        ("GET", "/gamer/Test+Gamer/wishlist"): [CHALLENGE, resp(WISHLIST)],
        ("GET", "/game/Kingdom-Come-Deliverance-II/dlc"): [CHALLENGE, resp(DLC)],
        ("GET", "/game/Kingdom-Come-Deliverance-II"): [CHALLENGE, resp(GAME)],
        ("GET", "/search-results"): [CHALLENGE, resp(SEARCH_V2.replace("page=2", "page=9"))],
        ("GET", "/disc-to-digital"): [CHALLENGE, resp("<html>list</html>")],
        ("POST", "/ajaxfunctions.aspx/"): [CHALLENGE] + ajax((200, ok_d())),
    }
    http = FakeHttp(routes)
    c = client(http, fs)
    c.login()
    c.wishlist()
    c.search("Final Fantasy")
    c.dlc("Kingdom-Come-Deliverance-II")
    c.get_html(BASE + "/disc-to-digital/games")
    c.add(13982, 11986)
    c.remove(13982, 11986)
    assert len(fs.calls) >= 7
    assert_anonymous(fs)
    # and everything that carried them went through the curl transport
    assert any(SECRET in (call["body"] or "") for call in http.calls)
    assert any(call["cookies"].get("GamerToken") == TOKEN for call in http.calls)


# Pages fetched directly (not through a browser) carry no `g-li` body class: TA's JavaScript adds
# that. The server-rendered header differs instead (observed live 2026-11-01).
SERVER_LOGGED_IN_HEADER = ('<header><nav><a href="/login" title="Log in" rel="nofollow" class="login hd">'
                           '<i class="fa fa-user-circle"></i></a></nav></header>')
SERVER_LOGGED_OUT_HEADER = ('<header><nav><div class="login-full hd"><span data-tgl="m-login" class="multi">'
                            '<span>Log in</span></span><ul class="m-login"><li><a href="/login">Log in</a></li></ul>'
                            '</div></nav></header>')


def server_rendered(page, logged_in_header=True):
    page = page.replace('class="ta g-li d"', 'class="ta"')
    header = SERVER_LOGGED_IN_HEADER if logged_in_header else SERVER_LOGGED_OUT_HEADER
    return page.replace("<main>", header + "<main>", 1)


def test_logged_in_reads_the_server_rendered_header():
    assert ta.logged_in(server_rendered(WISHLIST))
    assert not ta.logged_in(server_rendered(WISHLIST, logged_in_header=False))
    assert not ta.logged_in(server_rendered(LOGIN, logged_in_header=False))
    both = server_rendered(WISHLIST).replace("<main>", SERVER_LOGGED_OUT_HEADER + "<main>", 1)
    assert not ta.logged_in(both)
    assert not ta.logged_in("<html><body class=\"ta\"></body></html>")
    assert ta.logged_in(WISHLIST)  # a browser-rendered page (g-li) still counts


def test_server_rendered_wishlist_reads_without_logging_in_again():
    routes = {("GET", "/gamer/Test+Gamer/wishlist"): resp(server_rendered(WISHLIST)),
              ("GET", "/game/Kingdom-Come-Deliverance-II"): resp(server_rendered(GAME))}
    c, http = logged_in(routes)
    assert len(c.wishlist()) == 4
    assert http.urls("POST") == []


def test_server_rendered_logged_out_wishlist_logs_in_once():
    routes = login_routes()
    public = server_rendered(PUBLIC_WISHLIST, logged_in_header=False)
    routes[("GET", "/gamer/Test+Gamer/wishlist")] = [resp(public), resp(server_rendered(WISHLIST))]
    routes[("GET", "/game/Kingdom-Come-Deliverance-II")] = resp(GAME)
    c, http = logged_in(routes)
    assert len(c.wishlist()) == 4
    assert http.urls("POST") == ["/login"]


# ---------------------------------------------------------------- session


def test_login_posts_the_form_through_curl_with_the_get_cookies():
    routes = login_routes()
    page = LOGIN.replace("</form>", '<input type="checkbox" name="chkRemember" value="True" checked="">\n</form>')
    routes[("GET", "/login")] = resp(page, {"ASP.NET_SessionId": "sess-value"})
    fs = FakeFS()
    http = FakeHttp(routes)
    c = client(http, fs)
    c.login()
    post = next(call for call in http.calls if call["method"] == "POST")
    assert post["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert post["headers"]["Referer"] == BASE + "/login" and post["headers"]["Origin"] == BASE
    fields = {k: v[0] for k, v in parse_qs(post["body"], keep_blank_values=True).items()}
    assert fields["txtGamerTag"] == TAG and fields["txtPassword"] == SECRET
    assert fields["Command"] == "btnLogin_click" and fields["Argument"] == ""
    assert fields["__VIEWSTATE"] == "VIEWSTATE-PLACEHOLDER" and fields["__VIEWSTATEGENERATOR"]
    # checkboxes and the password box's own (empty) value are not sent as form fields
    assert "chkRemember" not in fields
    # the GET's cookies go with the POST; the redirect after it is followed with the new session
    assert post["cookies"] == {"ASP.NET_SessionId": "sess-value"}
    assert http.calls[-1]["method"] == "GET" and http.calls[-1]["body"] is None
    assert "Content-Type" not in http.calls[-1]["headers"] and "Origin" not in http.calls[-1]["headers"]
    assert http.calls[-1]["cookies"]["GamerToken"] == TOKEN
    assert c._cookies["GamerToken"] == TOKEN and c._cookies["HashKey"] == HASH
    assert fs.calls == []  # no challenge, so FlareSolverr was never asked


def test_login_without_gamertoken_is_auth_error_and_hides_the_password():
    routes = login_routes()
    routes[("POST", "/login")] = resp("<html>Invalid login</html>", {"ASP.NET_SessionId": "sess2"})
    c = client(FakeHttp(routes))
    with pytest.raises(AuthError) as e:
        c.login()
    assert SECRET not in str(e.value) and TAG not in str(e.value)
    assert SECRET not in repr(c) and TAG not in repr(c)


def test_login_http_error_hides_the_password():
    routes = login_routes()
    routes[("POST", "/login")] = resp("boom " + SECRET, status=500)
    c = client(FakeHttp(routes))
    with pytest.raises(TAError) as e:
        c.login()
    assert "500" in str(e.value) and SECRET not in str(e.value)


def test_session_is_saved_outside_the_repo_mode_600_without_clearance(tmp_path):
    path = tmp_path / "wsync-ta" / "session.json"
    routes = login_routes()
    routes[("GET", "/login")] = [CHALLENGE, resp(LOGIN, {"ASP.NET_SessionId": "sess-value"})]
    c = client(FakeHttp(routes), session_path=path)
    c.login()
    assert stat.S_IMODE(os.stat(path).st_mode) & 0o777 == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) & 0o777 == 0o700  # setgid may be inherited
    saved = json.loads(path.read_text())
    assert saved["GamerToken"] == TOKEN and "cf_clearance" not in saved
    # a new client picks the session up without logging in
    http = FakeHttp({("GET", "/gamer/Test+Gamer/wishlist"): resp(WISHLIST),
                     ("GET", "/game/Kingdom-Come-Deliverance-II"): resp(GAME)})
    client(http, session_path=path).wishlist()
    assert not any(call["url"].endswith("/login") for call in http.calls)
    assert http.calls[0]["cookies"]["GamerToken"] == TOKEN


def test_cookies_set_and_cleared_by_pages_are_kept():
    routes = {("GET", "/game/X"): resp(GAME, headers=[
        ("set-cookie", "Fresh=1; path=/"),
        ("set-cookie", "GamerID=; expires=Thu, 01-Jan-1970 00:00:01 GMT; path=/"),
        ("set-cookie", "cf_clearance=renewed; path=/"),
    ])}
    c, _ = logged_in(routes)
    c._clearance = (CLEAR, UA)
    c.game("X")
    assert c._cookies["Fresh"] == "1" and "GamerID" not in c._cookies and "cf_clearance" not in c._cookies
    assert c._clearance == ("renewed", UA)


def test_redirects_off_the_site_are_refused():
    c, http = logged_in({("GET", "/game/X"): resp(status=302, location="https://evil.example/steal")})
    with pytest.raises(TAError) as e:
        c.game("X")
    assert "redirected" in str(e.value) and len(http.calls) == 1


def test_redirect_loops_are_bounded():
    c, http = logged_in({("GET", "/game/X"): resp(status=302, location="/game/X")})
    with pytest.raises(TAError):
        c.game("X")
    assert len(http.calls) == ta.MAX_REDIRECTS + 1


def test_only_ta_urls_are_requested():
    c, http = logged_in({})
    with pytest.raises(ValueError):
        c.get_html("https://evil.example/")
    assert http.calls == []


def test_gated_page_logs_in_once_and_retries():
    routes = login_routes()
    routes[("GET", "/gamer/Test+Gamer/wishlist")] = [resp(PUBLIC_WISHLIST), resp(WISHLIST)]
    routes[("GET", "/game/Kingdom-Come-Deliverance-II")] = resp(GAME)
    http = FakeHttp(routes)
    c = client(http)
    c._cookies = {"GamerToken": "stale"}
    assert len(c.wishlist()) == 4
    assert http.urls("POST") == ["/login"]
    reads = [call for call in http.calls if call["url"].endswith("/wishlist")]
    assert [r["cookies"]["GamerToken"] for r in reads] == ["stale", TOKEN]


def test_please_log_in_page_logs_in_once_then_fails_loudly():
    routes = login_routes()
    routes[("GET", "/game/X")] = resp(GATED)
    http = FakeHttp(routes)
    c = client(http)
    with pytest.raises(AuthError):
        c.game("X")
    assert http.urls("POST") == ["/login"]


def test_http_error_status_is_checked_before_login_heuristics():
    routes = login_routes()
    routes[("GET", "/game/X")] = resp(GATED, status=500)
    c, http = logged_in(routes)
    with pytest.raises(TAError) as e:
        c.game("X")
    assert not isinstance(e.value, AuthError) and "500" in str(e.value)
    assert http.urls("POST") == []


# ---------------------------------------------------------------- reads


def test_wishlist_keys_games_and_dlc_and_resolves_the_dlc_game_id():
    c, http = logged_in({("GET", "/gamer/Test+Gamer/wishlist"): resp(WISHLIST),
                         ("GET", "/game/Kingdom-Come-Deliverance-II"): resp(GAME)})
    got = c.wishlist()
    assert got == {
        "13982:11986": Entry("13982:11986", "Kingdom Come: Deliverance II - Horse Races"),
        "1873": Entry("1873", "LEGO Batman (JP)"),
        "5548": Entry("5548", "LEGO Dimensions (Xbox 360)"),
        "5074": Entry("5074", "LEGO The Hobbit (Xbox 360)"),
    }
    assert c.game_ids == {"Kingdom-Come-Deliverance-II": 13982}
    # a known slug is not fetched again
    c.wishlist()
    assert http.urls().count("/game/Kingdom-Come-Deliverance-II") == 1


def test_wishlist_duplicate_key_is_incomplete():
    first = WISHLIST.index('<div class="ach-panel nc"><a href="https://www.trueachievements.com/game/LEGO-Batman-Japanese')
    end = WISHLIST.index("</div>\n</div>", first) + len("</div>\n</div>")
    doubled = WISHLIST[:end] + WISHLIST[first:end] + WISHLIST[end:]
    c, _ = logged_in({("GET", "/gamer/Test+Gamer/wishlist"): resp(doubled),
                      ("GET", "/game/Kingdom-Come-Deliverance-II"): resp(GAME)})
    with pytest.raises(IncompleteRead):
        c.wishlist()


def test_wishlist_uses_a_seeded_game_id_cache():
    c, http = logged_in({("GET", "/gamer/Test+Gamer/wishlist"): resp(WISHLIST)},
                        game_ids={"Kingdom-Come-Deliverance-II": 13982})
    assert "13982:11986" in c.wishlist()
    assert http.urls() == ["/gamer/Test+Gamer/wishlist"]


def test_wishlist_incomplete_read_raises():
    broken = WISHLIST.replace("Postback('btnRemoveFromWishList', 5074)", "")
    c, _ = logged_in({("GET", "/gamer/Test+Gamer/wishlist"): resp(broken),
                      ("GET", "/game/Kingdom-Come-Deliverance-II"): resp(GAME)})
    with pytest.raises(IncompleteRead):
        c.wishlist()


def test_wishlist_still_public_after_relogin_is_auth_error():
    routes = login_routes()
    routes[("GET", "/gamer/Test+Gamer/wishlist")] = resp(PUBLIC_WISHLIST)
    http = FakeHttp(routes)
    c = client(http)
    with pytest.raises(AuthError):
        c.wishlist()
    assert http.urls("POST") == ["/login"]


def test_search_quotes_the_query_and_returns_games():
    # TA may still answer with the old layout; the parser reads both.
    c, http = logged_in({("GET", "/search-results"): resp(SEARCH)})
    assert ("Kingdom Come: Deliverance II", "Kingdom-Come-Deliverance-II") in c.search("Kingdom Come & Δ")
    assert http.calls[0]["url"] == BASE + "/search-results?search=Kingdom+Come+%26+%CE%94&cat=games"


def test_search_single_hit_follows_the_redirect_to_the_game():
    page = GAME.replace("News and Videos", "Achievements")
    c, http = logged_in({
        ("GET", "/search-results"): resp(status=302, location="/game/Kingdom-Come-Deliverance-II/achievements"),
        ("GET", "/game/"): resp(page),
    })
    assert c.search("kingdom come deliverance ii") == [("Kingdom Come: Deliverance II", "Kingdom-Come-Deliverance-II")]
    assert http.urls()[1] == "/game/Kingdom-Come-Deliverance-II/achievements"


# ---------------------------------------------------------------- parsing


def test_parse_wishlist_reads_games_and_dlc_rows():
    rows = parse_wishlist(WISHLIST)
    assert [(r.kind, r.id, r.title) for r in rows] == [
        ("dlc", 11986, "Kingdom Come: Deliverance II - Horse Races"),
        ("game", 1873, "LEGO Batman (JP)"),
        ("game", 5548, "LEGO Dimensions (Xbox 360)"),
        ("game", 5074, "LEGO The Hobbit (Xbox 360)"),
    ]
    assert rows[0].game_slug == "Kingdom-Come-Deliverance-II"
    assert rows[1].game_slug == "LEGO-Batman-Japanese"


def test_parse_wishlist_row_without_remove_id_is_incomplete():
    broken = WISHLIST.replace("Postback('btnRemoveFromWishList', 5548)", "Postback('somethingElse')")
    with pytest.raises(IncompleteRead):
        parse_wishlist(broken)


def test_parse_wishlist_truncated_page_is_incomplete():
    cut = WISHLIST[: WISHLIST.index('id="divButtons"')]
    with pytest.raises(IncompleteRead):
        parse_wishlist(cut)


def test_parse_wishlist_paged_page_is_incomplete():
    paged = WISHLIST.replace('<div id="divButtons"', '<ul class="pagination"><li><a href="?page=2">2</a></li></ul><div id="divButtons"')
    with pytest.raises(IncompleteRead):
        parse_wishlist(paged)


def test_parse_wishlist_renamed_panel_class_is_incomplete_not_empty():
    renamed = WISHLIST.replace('class="ach-panel nc"', 'class="wish-panel nc"')
    with pytest.raises(IncompleteRead):
        parse_wishlist(renamed)


def test_parse_wishlist_extra_remove_button_outside_rows_is_incomplete():
    extra = WISHLIST.replace('<div id="divButtons"', "<a href=\"javascript:Postback('btnRemoveFromWishList', 77)\">x</a><div id=\"divButtons\"")
    with pytest.raises(IncompleteRead):
        parse_wishlist(extra)


def test_parse_wishlist_missing_buttons_block_is_incomplete():
    no_buttons = WISHLIST.replace('id="divButtons"', 'id="somethingElse"')
    assert "</main>" in no_buttons
    with pytest.raises(IncompleteRead):
        parse_wishlist(no_buttons)


def test_parse_wishlist_misplaced_main_end_is_incomplete():
    moved = WISHLIST.replace("</main>", "").replace("<main>", "<main></main>")
    with pytest.raises(IncompleteRead):
        parse_wishlist(moved)


def test_parse_wishlist_zero_rows_is_empty():
    head = WISHLIST[: WISHLIST.index('<div class="ach-panel')]
    empty = head + WISHLIST[WISHLIST.index('<div id="divButtons"'):]
    assert parse_wishlist(empty) == []
    # without the buttons block as well (an empty list's markup is unobserved)
    bare = head + "</main></form></body></html>"
    assert parse_wishlist(bare) == []


def test_parse_wishlist_someone_elses_list_is_an_error():
    with pytest.raises(TAError):
        parse_wishlist(PUBLIC_WISHLIST.replace('class="ta"', 'class="ta g-li d"'))


def test_parse_search_keeps_games_only():
    assert parse_search(SEARCH, BASE + "/searchresults.aspx?search=kingdom%20come") == [
        ("Kingdom Come: Deliverance", "Kingdom-Come-Deliverance"),
        ("Kingdom Come: Deliverance (Windows)", "Kingdom-Come-Deliverance-Windows"),
        ("Kingdom Come: Deliverance II", "Kingdom-Come-Deliverance-II"),
    ]


def test_parse_search_single_hit_redirects_to_the_game():
    page = GAME.replace("News and Videos", "Achievements")
    assert parse_search(page, BASE + "/game/Kingdom-Come-Deliverance-II/achievements") == [
        ("Kingdom Come: Deliverance II", "Kingdom-Come-Deliverance-II")
    ]


def test_parse_search_no_results():
    page = '<html><body class="ta g-li d"><main><div id="oSearchResultsHolder"><div class="informationpanel">There are no matching search results, please change your search terms</div></div></main></body></html>'
    assert parse_search(page, BASE + "/searchresults.aspx?search=zz") == []


def test_parse_game_page():
    g = parse_game(GAME, "Kingdom-Come-Deliverance-II")
    assert g == TAGame(13982, "Kingdom Come: Deliverance II", "Xbox Series X|S", "Kingdom-Come-Deliverance-II",
                       ("Xbox Series X|S", "Windows"))


def test_parse_game_page_of_an_unreleased_game_has_a_negative_placeholder_id():
    page = GAME.replace('data-game="13982"', 'data-game="-7827"')
    assert parse_game(page, "Persona-6").id == -7827


def test_placeholder_ids_are_never_written():
    c, _ = logged_in({})
    with pytest.raises(ValueError):
        c.add(-7827)


def test_parse_game_page_without_id_is_an_error():
    with pytest.raises(TAError):
        parse_game(GAME.replace('data-game="13982"', ""), "x")


def test_parse_dlc_page():
    assert parse_dlc(DLC) == [
        ("Hardcore Mode", 10913),
        ("Brushes with Death", 11026),
        ("Horse Races", 11986),
        ("Legacy of the Forge", 11568),
        ("Mysteria Ecclesiae", 11871),
    ]


def test_parse_dlc_page_without_packs():
    page = DLC[: DLC.index('<div class="pnl-hd')] + "</main></body></html>"
    assert parse_dlc(page) == []


def test_parse_dlc_rejects_other_pages():
    with pytest.raises(TAError):
        parse_dlc(GAME)


# ---------------------------------------------------------------- page choice


def page(platform, n=1):
    return TAGame(n, f"Game {n}", platform, f"Game-{n}", (platform,))


def test_choose_ta_page_never_picks_windows_or_360_by_title_or_slug():
    win_title = TAGame(1, "Game (Windows)", "Xbox Series X|S", "Game-PC", ("Xbox Series X|S",))
    win_slug = TAGame(2, "Game", "Xbox Series X|S", "Game-Windows", ("Xbox Series X|S",))
    x360_title = TAGame(3, "Game (Xbox 360)", "Xbox One", "Game-Old", ("Xbox One",))
    x360_slug = TAGame(4, "Game", "Xbox One", "Game-Xbox-360", ("Xbox One",))
    one = TAGame(5, "Game (Xbox One)", "Xbox One", "Game-Xbox-One", ("Xbox One",))
    assert choose_ta_page([win_title, win_slug, x360_title, x360_slug]) is None
    assert choose_ta_page([win_title, win_slug, x360_title, x360_slug, one]) is one


def test_choose_ta_page_prefers_series_then_one():
    win, x360, one, series = page("Windows", 1), page("Xbox 360", 2), page("Xbox One", 3), page("Xbox Series X|S", 4)
    assert choose_ta_page([win, x360, one, series]) is series
    assert choose_ta_page([win, one, x360]) is one
    assert choose_ta_page([win, x360]) is None
    assert choose_ta_page([]) is None




def test_parse_search_v2_reads_game_rows():
    assert parse_search(SEARCH_V2, V2_URL) == [
        ("FINAL FANTASY VII REMAKE INTERGRADE", "FINAL-FANTASY-VII-REMAKE-INTERGRADE"),
        ("FINAL FANTASY RESONANCE", "Final-Fantasy-Resonance"),
        ("FINAL FANTASY VII REBIRTH", "FINAL-FANTASY-VII-REBIRTH"),
    ]


def test_parse_search_v2_skips_series_rows_and_other_sections():
    series = ('<a class="sst-game-row" href="/series/Final-Fantasy"><span class="sst-game-info">'
              '<span class="sst-game-name">FINAL FANTASY Series</span><span class="sst-game-meta"></span></span></a>')
    hdr = '<div class="section-hdr"><span class="title">Games <span class="count">45 total</span></span></div>'
    page = SEARCH_V2.replace('<div class="section">\n', '<div class="section">' + hdr + series)
    assert [s for _, s in parse_search(page, V2_URL)] == [
        "FINAL-FANTASY-VII-REMAKE-INTERGRADE", "Final-Fantasy-Resonance", "FINAL-FANTASY-VII-REBIRTH"]


def test_parse_search_v2_ignores_the_matched_alias():
    # Seen live 2026-11-01: a row found through an alias names it after the title.
    alias = ('<span class="sst-game-name">FINAL FANTASY RESONANCE</span><span class="sst-matched-name">Matched: '
             '&ldquo;<span class="hl">FF RESONANCE</span>&rdquo;</span>')
    page = SEARCH_V2.replace('<span class="sst-game-name"><span class="hl">FINAL FANTASY</span> RESONANCE</span>', alias)
    assert alias in page
    assert ("FINAL FANTASY RESONANCE", "Final-Fantasy-Resonance") in parse_search(page, V2_URL)


def test_parse_search_v2_no_results():
    page = ('<html><body class="ta d"><main><h2 class="results-heading">Search results for “zz” '
            '<span class="count">0 results</span></h2><div class="section empty">'
            '<div class="empty-note">No games match “zz”.</div></div></main></body></html>')
    assert parse_search(page, BASE + "/search-results?search=zz&cat=games") == []


def test_parse_search_v2_unreadable_row_is_an_error():
    page = SEARCH_V2.replace('<span class="sst-game-name">', '<span class="sst-title">', 1)
    with pytest.raises(BaseTAError, match="search"):
        parse_search(page, V2_URL)


def test_parse_search_v2_without_a_count_is_an_error():
    page = SEARCH_V2.replace('<span class="count">45 results</span>', "")
    with pytest.raises(BaseTAError, match="count"):
        parse_search(page, V2_URL)
    with pytest.raises(BaseTAError):  # no heading at all: not a results page we know
        parse_search(SEARCH_V2.replace('class="results-heading"', 'class="x"'), V2_URL)


def test_parse_search_v2_count_without_rows_is_an_error():
    page = SEARCH_V2.replace('class="sst-game-row"', 'class="sst-row-v3"')
    with pytest.raises(BaseTAError, match="45 results but no game rows"):
        parse_search(page, V2_URL)


def test_parse_search_v2_overview_uses_the_games_section_count():
    page = ('<main><h2 class="results-heading">Search results for “P” <span class="count">5 results across '
            '2 categories</span></h2><div id="srOverviewSections"><div class="section" id="sr-games">'
            '<div class="section-hdr"><span class="title">Games <span class="count">1 total</span></span></div>'
            '<a class="sst-game-row" href="/game/Persona-6/achievements"><span class="sst-game-info">'
            '<span class="sst-game-name"><span class="hl">Persona 6</span></span><span class="sst-game-meta">'
            '</span></span></a></div><div class="section" id="sr-news"><div class="section-hdr"><span class="title">'
            'News <span class="count">4 total</span></span></div><a class="sst-game-row" href="/game/X/dlc/Y">'
            '<span>a row in another section is ignored</span></a></div></div></main>')
    assert parse_search(page, BASE + "/search-results?search=P") == [("Persona 6", "Persona-6")]
    with pytest.raises(BaseTAError):
        parse_search(page.replace("sst-game-row", "sst-other"), BASE + "/search-results?search=P")
    empty = page.replace("1 total", "0 total").replace("sst-game-row", "sst-other")
    assert parse_search(empty, BASE + "/search-results?search=P") == []




V2_URL = BASE + "/search-results?search=Final+Fantasy&cat=games"


def test_search_follows_pages_up_to_the_cap():
    last = SEARCH_V2.replace("&amp;page=2", "&amp;page=1")  # a page linking no further page
    c, http = logged_in({("GET", "/search-results"): [resp(SEARCH_V2), resp(last)]})
    got = c.search("Final Fantasy")
    assert len(got) == 3  # same rows twice, de-duplicated
    assert http.urls() == ["/search-results?search=Final+Fantasy&cat=games",
                           "/search-results?search=Final+Fantasy&cat=games&page=2"]
    c, http = logged_in({("GET", "/search-results"): resp(SEARCH_V2.replace("page=2", "page=9"))})
    c.search("Final Fantasy")
    assert len(http.urls()) == 1  # a link to some other page isn't "next"
    routes = {  # every page links its successor; specific routes first (prefix match)
        ("GET", f"/search-results?search=Final+Fantasy&cat=games&page={n}"):
            resp(SEARCH_V2.replace("&amp;page=2", f"&amp;page={n + 1}"))
        for n in range(2, 10)
    }
    routes[("GET", "/search-results")] = resp(SEARCH_V2)
    c, http = logged_in(routes)
    got = c.search("Final Fantasy")
    assert len(http.urls()) == ta.SEARCH_PAGES
    assert got.complete is False  # pages remained: the caller must not read this as complete


def test_search_that_reads_every_page_is_complete():
    c, _ = logged_in({("GET", "/search-results"): resp(SEARCH_V2.replace("page=2", "page=9"))})
    assert c.search("Final Fantasy").complete is True


def test_game_reads_the_news_page():
    c, http = logged_in({("GET", "/game/Kingdom-Come-Deliverance-II"): resp(GAME)})
    assert c.game("Kingdom-Come-Deliverance-II").id == 13982
    assert http.urls() == ["/game/Kingdom-Come-Deliverance-II"]


def test_dlc_by_slug_and_by_id():
    c, http = logged_in({
        ("GET", "/game/Kingdom-Come-Deliverance-II/dlc"): resp(DLC),
        ("GET", "/game.aspx?gameid=13982"): resp(status=301, location=BASE + "/game/Kingdom-Come-Deliverance-II/achievements"),
        ("GET", "/game/Kingdom-Come-Deliverance-II/achievements"): resp("<html></html>"),
    })
    assert c.dlc("Kingdom-Come-Deliverance-II")[2] == ("Horse Races", 11986)
    assert c.dlc(13982)[0] == ("Hardcore Mode", 10913)
    assert http.urls() == ["/game/Kingdom-Come-Deliverance-II/dlc", "/game.aspx?gameid=13982",
                           "/game/Kingdom-Come-Deliverance-II/achievements",
                           "/game/Kingdom-Come-Deliverance-II/dlc"]


def test_get_html_returns_page_for_the_d2d_fetcher():
    c, _ = logged_in({("GET", "/disc-to-digital/games"): resp("<html>list</html>")})
    assert c.get_html(BASE + "/disc-to-digital/games") == "<html>list</html>"


# ---------------------------------------------------------------- writes


AJAX = "/ajaxfunctions.aspx/"


def test_add_payload_shape_headers_and_cookies():
    c, http = logged_in({("POST", AJAX): ajax((200, ok_d()))})
    c._clearance = (CLEAR, UA)
    c.add(13982, 11986)
    call = http.calls[0]
    assert call["url"] == BASE + "/ajaxfunctions.aspx/AddToWishlist"
    assert json.loads(call["body"]) == {"Wishlist": {"GameID": 13982, "DLCID": 11986, "HashKey": HASH}}
    assert call["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert call["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert call["headers"]["User-Agent"] == UA
    assert call["headers"]["Origin"] == BASE
    assert call["headers"]["Referer"].startswith(BASE + "/")
    assert call["cookies"]["cf_clearance"] == CLEAR and call["cookies"]["GamerToken"] == TOKEN


def test_add_base_game_sends_dlcid_zero_and_remove_uses_its_endpoint():
    fs = FakeFS()
    c, http = logged_in({("POST", AJAX): ajax((200, ok_d()))}, fs=fs)
    c.add(1873)
    c.remove(1873)
    assert json.loads(http.calls[0]["body"])["Wishlist"] == {"GameID": 1873, "DLCID": 0, "HashKey": HASH}
    assert http.calls[1]["url"] == BASE + "/ajaxfunctions.aspx/RemoveFromWishlist"
    assert fs.calls == []  # no challenge, no FlareSolverr round trip


def test_success_when_errors_empty_even_without_success_flag():
    c, _ = logged_in({("POST", AJAX): ajax((200, ok_d(success=False, errors=[])))})
    c.add(1873)


def test_add_already_on_the_list_raises_already_listed():
    c, http = logged_in({("POST", AJAX): ajax((200, ok_d(success=False, errors=["This Game is already on your wishlist"])))})
    with pytest.raises(AlreadyListed):
        c.add(1873)
    assert len(http.calls) == 1


def test_remove_failure_raises_with_the_site_message():
    c, _ = logged_in({("POST", AJAX): ajax((200, ok_d(success=False, errors=["No wishlist items to remove"])))})
    with pytest.raises(TAError) as e:
        c.remove(1873)
    assert "No wishlist items to remove" in str(e.value)
    assert HASH not in str(e.value) and TOKEN not in str(e.value)


def test_write_logged_out_relogins_once_then_succeeds():
    routes = login_routes({"HashKey": "fresh-hash"})
    routes[("POST", AJAX)] = ajax((200, ok_d(success=False, errors=["The Gamer must be specified"])), (200, ok_d()))
    c, http = logged_in(routes)
    c.add(1873)
    assert http.urls("POST").count("/login") == 1
    writes = [call for call in http.calls if AJAX in call["url"]]
    assert len(writes) == 2 and json.loads(writes[1]["body"])["Wishlist"]["HashKey"] == "fresh-hash"


def test_write_logged_out_twice_is_auth_error():
    routes = login_routes()
    routes[("POST", AJAX)] = ajax((200, ok_d(success=False, errors=["The Gamer must be specified"])))
    c, http = logged_in(routes)
    with pytest.raises(AuthError):
        c.remove(1873)
    assert http.urls("POST").count("/login") == 1
    assert len([call for call in http.calls if AJAX in call["url"]]) == 2


def test_write_without_a_hashkey_logs_in_first():
    routes = login_routes()
    routes[("POST", AJAX)] = ajax((200, ok_d()))
    c, http = logged_in(routes)
    c._cookies.pop("HashKey")
    c.add(1873)
    assert http.urls("POST") == ["/login", AJAX + "AddToWishlist"]


def test_write_cloudflare_challenge_refreshes_clearance_once():
    fs = FakeFS()
    c, http = logged_in({("POST", AJAX): [CHALLENGE] + ajax((200, ok_d()))}, fs=fs)
    c.add(1873)
    assert len(fs.calls) == 1 and http.calls[1]["cookies"]["cf_clearance"] == CLEAR
    assert_anonymous(fs)
    fs2 = FakeFS()
    c2, http2 = logged_in({("POST", AJAX): CHALLENGE}, fs=fs2)
    with pytest.raises(TAError):
        c2.add(1873)
    assert len(http2.calls) == 2 and len(fs2.calls) == 1


def test_write_redirect_is_an_error_and_never_followed():
    c, http = logged_in({("POST", AJAX): resp(status=302, location="/login")})
    with pytest.raises(TAError) as e:
        c.add(1873)
    assert "302" in str(e.value) and len(http.calls) == 1


def test_write_non_json_answer_is_an_error():
    c, _ = logged_in({("POST", AJAX): ajax((200, "<html>oops</html>"))})
    with pytest.raises(TAError):
        c.add(1873)


def test_bad_ids_are_rejected_before_any_request():
    fs = FakeFS()
    c, http = logged_in({("POST", AJAX): ajax((200, ok_d()))}, fs=fs)
    for bad in ("1873", 0, -1, True):
        with pytest.raises(ValueError):
            c.add(bad)
    with pytest.raises(ValueError):
        c.remove(1873, "5")
    assert http.calls == [] and fs.calls == []


def test_errors_never_carry_the_gamertag():
    c, _ = logged_in({("GET", "/gamer/Test+Gamer/wishlist"): resp(GATED, status=500)})
    with pytest.raises(TAError) as e:
        c.wishlist()
    assert "Test" not in str(e.value) and "Gamer/" not in str(e.value)

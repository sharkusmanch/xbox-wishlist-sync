"""Microsoft sign-in for the library step: a rotating refresh token becomes Xbox Live and Store tokens.

Chain: refresh token -> Microsoft access token -> Xbox Live user token -> XSTS tokens (one for Xbox
Live, one for the Microsoft Store). Microsoft rotates the refresh token on every use, so the new one
is written to the token file before any Xbox Live call. The job never runs a device-code login: the
first token is a seed (env XBOX_REFRESH_TOKEN) made by the games repo's scripts/xbox-library.py.
Logs and errors name no token, XUID or gamertag; every token is handed to `on_secret` (the scrubber)
the moment it exists.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

CLIENT_ID = "1f907974-e22b-4810-a9de-d9647380c97e"  # xbox.com's public sign-in client
SCOPE = "XboxLive.signin offline_access"
AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
RP_LIVE = "http://xboxlive.com"  # gamertag, XUID
RP_STORE = "http://mp.microsoft.com/"  # Collections, orders
TIMEOUT = 60
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

XSTS_ERRORS = {
    2148916233: "this Microsoft account has no Xbox profile",
    2148916235: "Xbox Live isn't available in this account's region",
    2148916236: "this account needs adult verification",
    2148916237: "this account needs adult verification",
    2148916238: "this is a child account: add it to a Microsoft family first",
}

SIGN_IN_FIX = (
    "run the games repo's `scripts/xbox-library.py --token-file <tmp> login`, then put the new "
    "refresh token in OpenBao secret/apps/tools/xbox-wishlist-sync key XBOX_REFRESH_TOKEN"
)


class XboxError(Exception):
    """A Microsoft or Xbox call failed: the read is incomplete, so nothing may be planned from it."""


class SignInNeeded(XboxError):
    """No saved or seeded refresh token works: report it, don't fail the run."""


class Http(Protocol):
    def request(self, method: str, url: str, *, form: dict | None = None,
                json_body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class UrllibHttp:
    """HTTPS transport that never follows redirects and returns (status, parsed JSON or {})."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, url, *, form=None, json_body=None, headers=None):
        hdrs = {"User-Agent": UA, "Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        # Failures name only the exception class: a URL or message can carry a token.
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with self._opener.open(req, timeout=TIMEOUT) as resp:
                    status, raw = resp.status, resp.read()
            except urllib.error.HTTPError as e:
                status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            raise XboxError(f"network error: {type(e).__name__}") from None
        try:
            body = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            body = {}
        return status, body if isinstance(body, dict) else {}


def xbl3(uhs: str, token: str) -> str:
    return f"XBL3.0 x={uhs};{token}"


@dataclass
class Session:
    uhs: str
    live_token: str = field(repr=False)
    store_token: str = field(repr=False)
    xuid: str = field(repr=False)
    gamertag: str = field(repr=False)

    def headers(self, rp: str, contract: str | None = None) -> dict:
        """Request headers for `rp` = "store" (Collections, orders) or "live" (Xbox Live)."""
        h = {
            "Authorization": xbl3(self.uhs, self.store_token if rp == "store" else self.live_token),
            "MS-CV": uuid.uuid4().hex[:16] + ".0",
            "Accept": "application/json",
            "Accept-Language": "en-US",
        }
        if contract:
            h["x-xbl-contract-version"] = str(contract)
        return h


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class TokenStore:
    """The refresh token on disk (0600, atomic) beside the SHA-256 of the last seed used."""

    def __init__(self, path: Path, seed: str = ""):
        self.path = Path(path)
        self.seed = seed or ""

    def _read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def candidates(self) -> list[tuple[str, str]]:
        data = self._read()
        out: list[tuple[str, str]] = []
        if data:
            out.append(("file", data["refresh_token"]))
        if self.seed and (not data or data.get("seed_sha256") != _sha(self.seed)):
            out.append(("seed", self.seed))
        return out

    def save(self, token: str, source: str) -> None:
        data = self._read() or {}
        fingerprint = _sha(self.seed) if source == "seed" else data.get("seed_sha256")
        out = {"refresh_token": token}
        if fingerprint:
            out["seed_sha256"] = fingerprint
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(out, fh)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)


def refresh_access(http: Http, refresh_token: str) -> dict | None:
    """Trade a refresh token for an access token. None: Microsoft rejected the token (try another).

    Any other failure is an XboxError (a failed read). When no new refresh token comes back, the old
    one stays valid and is returned.
    """
    status, tok = http.request("POST", f"{AUTHORITY}/token", form={
        "grant_type": "refresh_token", "client_id": CLIENT_ID,
        "refresh_token": refresh_token, "scope": SCOPE})
    if status == 400 and tok.get("error"):
        return None
    if status != 200 or not tok.get("access_token"):
        raise XboxError(f"Microsoft sign-in failed: HTTP {status}")
    return {"access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token") or refresh_token}


def xbl_user_token(http: Http, access_token: str) -> tuple[str, str]:
    status, j = http.request(
        "POST", XBL_URL, headers={"x-xbl-contract-version": "1"},
        json_body={"RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT",
                   "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                                  "RpsTicket": f"d={access_token}"}})
    try:
        return j["Token"], j["DisplayClaims"]["xui"][0]["uhs"]
    except (KeyError, IndexError, TypeError):
        raise XboxError(f"Xbox Live sign-in failed: HTTP {status}") from None


def xsts_token(http: Http, user_token: str, rp: str) -> tuple[str, dict]:
    status, j = http.request(
        "POST", XSTS_URL, headers={"x-xbl-contract-version": "1"},
        json_body={"RelyingParty": rp, "TokenType": "JWT",
                   "Properties": {"UserTokens": [user_token], "SandboxId": "RETAIL"}})
    try:
        if status != 200:
            raise KeyError
        return j["Token"], j["DisplayClaims"]["xui"][0]
    except (KeyError, IndexError, TypeError):
        x = j.get("XErr")
        why = XSTS_ERRORS.get(x) or (f"XErr {x}" if x else "")
        raise XboxError(f"Xbox token for {rp} refused: HTTP {status}" + (f", {why}" if why else "")) from None


def open_session(http: Http, store: TokenStore, on_secret: Callable[[str], None]) -> Session:
    """Sign in with the first working refresh token (file, then a new seed) and open a Session."""
    cands = store.candidates()
    for _, token in cands:
        on_secret(token)
    tok, source = None, ""
    for source, token in cands:
        tok = refresh_access(http, token)
        if tok:
            break
    if not tok:
        raise SignInNeeded("Microsoft sign-in needed: no saved or seeded refresh token works; " + SIGN_IN_FIX)
    on_secret(tok["refresh_token"])
    on_secret(tok["access_token"])
    # The rotated token goes to disk before any Xbox Live call: a crash after this loses nothing.
    if source == "seed" or tok["refresh_token"] != dict(cands)["file"]:
        store.save(tok["refresh_token"], source)
    user, _ = xbl_user_token(http, tok["access_token"])
    on_secret(user)
    live, claims = xsts_token(http, user, RP_LIVE)
    on_secret(live)
    shop, _ = xsts_token(http, user, RP_STORE)
    on_secret(shop)
    return Session(uhs=claims["uhs"], live_token=live, store_token=shop,
                   xuid=str(claims.get("xid") or ""), gamertag=str(claims.get("gtg") or ""))

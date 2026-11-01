"""A small FlareSolverr v3 client, used only to obtain a Cloudflare clearance.

FlareSolverr logs every request it is sent (URL, cookies, POST body) at its default log level,
so this client can only make an anonymous, cookie-less `request.get`: there is no way to pass it
cookies or a form. Callers use the answer's `cf_clearance` cookie and user agent to make their own
requests (with any credentials) from a Chrome-impersonating client.

Errors never repeat the request: URLs stay out of messages.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from wsync.errors import TAError

MAX_TIMEOUT_MS = 90000
HTTP_TIMEOUT = 150  # seconds; FlareSolverr's own deadline plus headroom


class FlareSolverrError(TAError):
    """FlareSolverr couldn't fetch the page (challenge unsolved, timeout, proxy down)."""


@dataclass(frozen=True)
class Solution:
    html: str = field(repr=False)
    cookies: dict[str, str] = field(repr=False)
    user_agent: str
    url: str = field(default="", repr=False)
    status: int = 200


Transport = Callable[[dict], dict]


def _scrub(message: str) -> str:
    """FlareSolverr messages can quote the URL; keep them short and URL-free."""
    return re.sub(r"\S+://\S+", "<url>", str(message))[:200]


class FlareSolverr:
    def __init__(self, url: str, *, transport: Transport | None = None, timeout: float = HTTP_TIMEOUT):
        self._url = url
        self._timeout = timeout
        self._transport = transport or self._post_json

    def __repr__(self) -> str:
        return "FlareSolverr()"

    def get(self, url: str) -> Solution:
        """An anonymous GET: no cookies and no body are ever sent (FlareSolverr logs both)."""
        payload = {"cmd": "request.get", "url": url, "maxTimeout": MAX_TIMEOUT_MS}
        what = payload["cmd"]
        answer = self._transport(payload)
        if not isinstance(answer, dict) or answer.get("status") != "ok" or not isinstance(answer.get("solution"), dict):
            message = answer.get("message", "") if isinstance(answer, dict) else ""
            raise FlareSolverrError(f"FlareSolverr {what} failed: {_scrub(message) or 'no solution'}")
        s = answer["solution"]
        try:
            return Solution(
                html=s.get("response") or "",
                cookies={c["name"]: c["value"] for c in s.get("cookies") or []},
                user_agent=s.get("userAgent") or "",
                url=s.get("url") or "",
                status=int(s.get("status") or 0),
            )
        except (KeyError, TypeError, ValueError):
            raise FlareSolverrError(f"FlareSolverr {what} answered an unreadable solution") from None

    def _post_json(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self._url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # FlareSolverr answers 500 with a JSON body explaining the failure.
            try:
                raw = e.read()
            except (http.client.HTTPException, OSError) as inner:
                raise FlareSolverrError(f"FlareSolverr answered HTTP {e.code} ({type(inner).__name__})") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise FlareSolverrError(f"FlareSolverr unreachable ({type(e).__name__})") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise FlareSolverrError("FlareSolverr answered something that isn't JSON") from None

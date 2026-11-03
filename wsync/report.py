"""Reporting: Vikunja problem tasks (deduplicated by a marker) and Apprise notifications.

Vikunja API (v2.x, checked against the 2.7.1 server's /api/v1/docs.json):
  - GET  /projects?page&per_page            list projects (paginated; x-pagination-total-pages header)
  - PUT  /projects                          create a project
  - GET  /tasks?filter=done = false && project = <id>&page&per_page   list a project's open tasks
  - PUT  /projects/{id}/tasks               create a task
  - POST /tasks/{id}                        update a task (we re-send the whole task, so fields we
                                            don't manage survive the update)

Descriptions are HTML in Vikunja v2. Each task description ends with its own
`<p>wsync-key: <product_id>:<kind></p>`; the key is parsed back from the *text* of the description,
so whatever re-wrapping the server does to the HTML doesn't lose it.

The Vikunja token is only ever sent in the Authorization header: it is not in URLs, bodies,
reprs, logs or exception text.
"""

from __future__ import annotations

import html
import http.client
import json
import logging
import re
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from wsync.errors import ReportError
from wsync.plan import AMBIGUOUS_PREFIX, Entry, ListPlan, Plan, Problem

log = logging.getLogger(__name__)

TIMEOUT = 30
PER_PAGE = 50
MAX_PAGES = 100
PLAN_REVIEW_KEY = "plan-review:dry-run"
COLLECTION_REVIEW_KEY = "collection-review:dry-run"
COLLECTION_REVIEW_TITLE = "Review the Deku collection plan"
# Tasks that aren't problem tasks: the problem sync never closes them.
_REVIEW_KEYS = frozenset({PLAN_REVIEW_KEY, COLLECTION_REVIEW_KEY})
_MARKER = re.compile(r"^wsync-key:\s*(\S+)\s*$", re.MULTILINE)
_HINT = ("add an override in the xbox-wishlist-sync overrides ConfigMap (overrides.json): "
         "`\"{pid}\": {{\"{field}\": \"<slug>\"}}`, or `\"skip\"` as the value to stop mirroring it there")
_KIND_TEXT = {
    "unmatched-deku": ("Deku Deals", "deku"),
    "unmatched-ta": ("TrueAchievements", "ta"),
}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    text: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)  # lower-cased names


class Transport(Protocol):
    def request(self, method: str, url: str, body: dict | None = None,
                headers: dict[str, str] | None = None) -> HttpResponse: ...


class UrllibTransport:
    def request(self, method, url, body=None, headers=None):
        # Every failure becomes a ReportError naming only the exception class: the URL can carry a
        # secret path (Apprise keys), and urllib puts it in some ValueError messages.
        try:
            data = json.dumps(body).encode() if body is not None else None
            hdrs = {"Accept": "application/json"}
            if data is not None:
                hdrs["Content-Type"] = "application/json"
            hdrs.update(headers or {})
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return HttpResponse(resp.status, resp.read().decode("utf-8", "replace"),
                                    {k.lower(): v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as e:
            try:
                return HttpResponse(e.code, e.read().decode("utf-8", "replace"),
                                    {k.lower(): v for k, v in e.headers.items()})
            except (OSError, http.client.HTTPException, ValueError) as e2:
                raise ReportError(f"network error: {type(e2).__name__}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            raise ReportError(f"network error: {type(e).__name__}") from None


# ---- description <-> text ---------------------------------------------------------------


def _inline(text: str) -> str:
    return re.sub(r"`([^`]*)`", r"<code>\1</code>", html.escape(text, quote=False))


def render_description(body: str, key: str) -> str:
    """Small Markdown subset (## headings, '- ' bullets, `code`) -> HTML, then the marker paragraph."""
    out: list[str] = []
    in_list = False
    for line in body.splitlines():
        line = line.rstrip()
        bullet = line.startswith("- ")
        if in_list and not bullet:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line[2:])}</li>")
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{min(level + 1, 6)}>{_inline(line[level:].strip())}</h{min(level + 1, 6)}>")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    out.append(f"<p>wsync-key: {html.escape(key, quote=False)}</p>")
    return "".join(out)


def plain_text(description: str) -> str:
    """Text of an HTML description, one line per block element, whitespace collapsed."""
    text = re.sub(r"(?i)<br\s*/?>|</(p|li|h[1-6]|div|ul|ol|pre|blockquote)>", "\n", description or "")
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def task_key(description: str) -> str | None:
    found = _MARKER.findall(plain_text(description))
    return found[-1] if found else None


def _ident(obj) -> int:
    """The numeric id of an API object, or a ReportError when the response is malformed."""
    ident = obj.get("id") if isinstance(obj, dict) else None
    if not isinstance(ident, int) or isinstance(ident, bool):
        raise ReportError("Vikunja answered with an object that has no numeric id")
    return ident


# ---- Vikunja ------------------------------------------------------------------------------


class Vikunja:
    def __init__(self, base_url: str, token: str, project_title: str = "Wishlist Sync",
                 *, transport: Transport | None = None):
        self._base = base_url.rstrip("/") + "/api/v1"
        self._token = token
        self.project_title = project_title
        self._http = transport or UrllibTransport()
        self._project_id: int | None = None
        self._cache: dict[str, dict] | None = None
        self._extras: dict[str, list[dict]] = {}

    def __repr__(self) -> str:
        return f"Vikunja(base={self._base!r}, project={self.project_title!r})"

    def _call(self, method: str, path: str, body: dict | None = None):
        resp = self._http.request(method, self._base + path, body,
                                  {"Authorization": f"Bearer {self._token}"})
        if not 200 <= resp.status < 300:
            raise ReportError(f"Vikunja {method} {path.split('?')[0]} answered HTTP {resp.status}")
        try:
            return json.loads(resp.text) if resp.text else None, resp.headers
        except ValueError:
            raise ReportError(f"Vikunja {method} {path.split('?')[0]} answered non-JSON") from None

    def _paged(self, path: str) -> list[dict]:
        sep = "&" if "?" in path else "?"
        rows: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            data, headers = self._call("GET", f"{path}{sep}per_page={PER_PAGE}&page={page}")
            if not isinstance(data, list):
                raise ReportError(f"Vikunja GET {path.split('?')[0]} answered an unexpected shape")
            if not all(isinstance(r, dict) for r in data):
                raise ReportError(f"Vikunja GET {path.split('?')[0]} answered an unexpected shape")
            rows.extend(data)
            total = headers.get("x-pagination-total-pages")
            if total is not None:
                try:
                    last = int(total)
                except (TypeError, ValueError):
                    raise ReportError("Vikunja sent a malformed pagination header") from None
                if page >= last:
                    break
            elif len(data) < PER_PAGE:
                break
        else:
            raise ReportError("Vikunja listing did not end")
        return rows

    def ensure_project(self) -> int:
        if self._project_id is None:
            for proj in self._paged("/projects"):
                if proj.get("title") == self.project_title:
                    self._project_id = _ident(proj)
                    break
            else:
                data, _ = self._call("PUT", "/projects", {"title": self.project_title})
                self._project_id = _ident(data)
        return self._project_id

    def open_tasks(self) -> dict[str, dict]:
        """Open tasks in the project carrying a wsync marker, keyed by marker (lowest id wins)."""
        pid = self.ensure_project()
        rows = self._paged("/tasks?" + urllib.parse.urlencode({"filter": f"done = false && project = {pid}"}))
        found: dict[str, dict] = {}
        extras: dict[str, list[dict]] = {}
        for task in sorted(rows, key=_ident):
            if task.get("done") or task.get("project_id") != pid:
                continue
            key = task_key(task.get("description") or "")
            if key is None:
                continue
            if key in found:
                extras.setdefault(key, []).append(task)
            else:
                found[key] = task
        self._cache, self._extras = found, extras
        return dict(found)

    def _tasks(self) -> dict[str, dict]:
        if self._cache is None:
            self.open_tasks()
        return self._cache

    def upsert(self, key: str, title: str, body: str) -> bool:
        """Create the task for `key`, or update it if the text changed. True when anything was written."""
        description = render_description(body, key)
        existing = self._tasks().get(key)
        if existing is None:
            data, _ = self._call("PUT", f"/projects/{self.ensure_project()}/tasks",
                                 {"title": title, "description": description})
            _ident(data)
            self._cache[key] = {**data, "title": title, "description": description}
            return True
        if (existing.get("title") == title
                and plain_text(existing.get("description", "")) == plain_text(description)):
            return False
        updated = {**existing, "title": title, "description": description}
        self._call("POST", f"/tasks/{existing['id']}", updated)
        self._cache[key] = updated
        return True

    def close(self, key: str) -> None:
        """Close every open task carrying `key` (the primary and any duplicates)."""
        existing = self._tasks().pop(key, None)
        if existing is not None:
            self._call("POST", f"/tasks/{existing['id']}", {**existing, "done": True})
        self.close_duplicates(key)

    def close_duplicates(self, key: str) -> None:
        """Keep the lowest-id open task for `key`; close the others."""
        self._tasks()
        for dup in self._extras.pop(key, []):
            self._call("POST", f"/tasks/{dup['id']}", {**dup, "done": True})


# ---- problems and the plan review -----------------------------------------------------------


def problem_key(p: Problem) -> str:
    return f"{p.product_id}:{p.kind}"


def problem_task(p: Problem) -> tuple[str, str]:
    if p.kind == "d2d-disputed":
        title = f"Disc to Digital sources disagree: {p.title}"
        fix = ("Informational: the game is treated as Disc-to-Digital capable (format `either`). "
               "This task closes itself once the sources agree.")
        problem = f"Disc to Digital: {p.detail}"
    else:
        site, field_name = _KIND_TEXT[p.kind]
        title = f"No confident {site} match: {p.title}"
        fix = "Fix: " + _HINT.format(pid=p.product_id, field=field_name)
        problem = f"{site}: {p.detail}"
    body = "\n".join([f"Game: {p.title}", f"Product ID: {p.product_id}", f"Problem: {problem}", fix])
    return title, body


def sync_problem_tasks(vk: Vikunja, problems: list[Problem], keep_kinds=(), keep_keys=()) -> None:
    """Open/refresh a task per problem and close the rest.

    Tasks of a kind in `keep_kinds`, or with a key in `keep_keys` ("<productId>:<kind>"), are left
    open even when absent from `problems`: the run couldn't check them (a list was skipped, its
    read failed, or the item's lookup failed or was deferred), so absence isn't a fix.
    """
    current = {problem_key(p): p for p in problems}
    before = vk.open_tasks().keys()
    for key, p in current.items():
        vk.upsert(key, *problem_task(p))
        vk.close_duplicates(key)
    for key in before - current.keys() - _REVIEW_KEYS:
        if key.rsplit(":", 1)[-1] in keep_kinds or key in keep_keys:
            continue
        vk.close(key)


def _is_empty(plan: Plan) -> bool:
    return not any(lp.adds or lp.removes or lp.updates or lp.paused_removes for lp in (plan.deku, plan.ta))


def _fmt_entry(e: Entry) -> str:
    extra = ", ".join(x for x in (e.format, "+".join(sorted(e.platforms))) if x)
    return f"{e.title} (`{e.target}`{', ' + extra if extra else ''})"


def _order(entries):
    return sorted(entries, key=lambda e: (e.title, e.target))


def _reason(reason: str | None) -> str:
    # The planner lists ambiguous titles in feed order; sort them so a reshuffled feed is not a change.
    # (Unresolved lookups are only counted there, so a flaky lookup's title never shows up here.)
    if not reason:
        return "unknown"
    parts = reason.split("; ")
    for i, part in enumerate(parts):
        if part.startswith(AMBIGUOUS_PREFIX):
            parts[i] = AMBIGUOUS_PREFIX + ", ".join(sorted(part[len(AMBIGUOUS_PREFIX):].split(", ")))
    return "; ".join(parts)


def _list_section(name: str, lp: ListPlan) -> list[str]:
    lines = [f"## {name}"]
    for label, entries in (("Add", lp.adds), ("Remove", lp.removes)):
        lines.append(f"### {label} ({len(entries)})")
        lines += [f"- {_fmt_entry(e)}" for e in _order(entries)]
    lines.append(f"### Update ({len(lp.updates)})")
    for cur, want in sorted(lp.updates, key=lambda u: (u[1].title, u[1].target)):
        lines.append(f"- {want.title} (`{want.target}`): format {cur.format} → {want.format}; "
                     f"platforms {'+'.join(sorted(cur.platforms)) or 'none'} → "
                     f"{'+'.join(sorted(want.platforms)) or 'none'}")
    lines.append(f"### Removals paused ({len(lp.paused_removes)})")
    if lp.paused_removes or lp.pause_reason:
        lines.append(f"Reason: {_reason(lp.pause_reason)}")
    lines += [f"- {_fmt_entry(e)}" for e in _order(lp.paused_removes)]
    return lines


def plan_review_body(plan: Plan) -> str:
    lines = ["Dry run: nothing has been written to Deku Deals or TrueAchievements.",
             f"Summary: {summary(plan, True) or 'no changes'}"]
    lines += _list_section("Deku Deals", plan.deku) + _list_section("TrueAchievements", plan.ta)
    if plan.skipped_unannounced:
        lines.append("## Skipped (unannounced, no page yet)")
        lines += [f"- {t}" for t in sorted(plan.skipped_unannounced)]
    return "\n".join(lines)


def plan_review_task(vk: Vikunja, plan: Plan, *, dry_run: bool = True) -> bool:
    """Keep the "review the plan" task current. True only when its text changed (send the ping then)."""
    if not dry_run or _is_empty(plan):
        vk.close(PLAN_REVIEW_KEY)
        return False
    changed = vk.upsert(PLAN_REVIEW_KEY, "Review the wishlist sync plan", plan_review_body(plan))
    vk.close_duplicates(PLAN_REVIEW_KEY)
    return changed


def collection_review_task(vk: Vikunja, body: str | None, *, dry_run: bool = True) -> bool:
    """Keep the Deku collection step's "review the plan" task current (dry run) or close it (live).

    True only when the task's text changed (send the "plan ready" ping then).
    """
    if not dry_run:
        vk.close(COLLECTION_REVIEW_KEY)
        return False
    changed = vk.upsert(COLLECTION_REVIEW_KEY, COLLECTION_REVIEW_TITLE, body or "")
    vk.close_duplicates(COLLECTION_REVIEW_KEY)
    return changed


def summary(plan: Plan, applied: bool) -> str | None:
    parts: list[str] = []
    for sign, attr in (("+", "adds"), ("−", "removes")):
        for name, lp in (("Deku", plan.deku), ("TA", plan.ta)):
            n = len(getattr(lp, attr))
            if n:
                parts.append(f"{sign}{n} {name}")
    fmt = len(plan.deku.updates) + len(plan.ta.updates)
    if fmt:
        parts.append(f"{fmt} format change{'s' if fmt != 1 else ''}")
    if not parts:
        return None
    text = ", ".join(parts)
    return text if applied else f"Dry run: {text}"


# ---- Apprise ---------------------------------------------------------------------------------


def notify(apprise_url: str, title: str, body: str, *, transport: Transport | None = None) -> None:
    """POST {title, body} to Apprise. Failures are logged (without the URL) and swallowed."""
    try:
        resp = (transport or UrllibTransport()).request("POST", apprise_url, {"title": title, "body": body})
        if not 200 <= resp.status < 300:
            log.warning("Apprise notification failed: HTTP %s", resp.status)
    except Exception as e:  # noqa: BLE001 - a notification must never take the run down; log the class only
        log.warning("Apprise notification failed: %s", type(e).__name__)

import json
import urllib.parse

import pytest

from wsync import report
from wsync.errors import ReportError
from wsync.plan import Entry, ListPlan, Plan, Problem
from wsync.report import HttpResponse, Vikunja, notify, plan_review_task, summary, sync_problem_tasks

BASE = "http://vikunja.test:3456"
TOKEN = "tk_super-secret-token"
PID = "9NBLGGH4R2R6"


class FakeVikunja:
    """Just enough of the Vikunja v2 API: projects, create, update, list with pagination."""

    def __init__(self, projects=None, tasks=None, per_page=2):
        self.projects = list(projects or [])
        self.tasks = {t["id"]: dict(t) for t in (tasks or [])}
        self.requests = []
        self.per_page = per_page
        self.next_id = 100

    def request(self, method, url, body=None, headers=None):
        self.requests.append((method, url, body, dict(headers or {})))
        assert url.startswith(BASE + "/api/v1/")
        parsed = urllib.parse.urlparse(url)
        path, q = parsed.path[len("/api/v1"):], urllib.parse.parse_qs(parsed.query)
        if method == "GET" and path == "/projects":
            return self._page(self.projects, q)
        if method == "PUT" and path == "/projects":
            proj = {"id": 7, "title": body["title"]}
            self.projects.append(proj)
            return HttpResponse(201, json.dumps(proj), {})
        if method == "GET" and path == "/tasks":
            assert "done = false" in q["filter"][0]
            return self._page([t for t in self.tasks.values() if not t["done"]], q)
        if method == "PUT" and path.endswith("/tasks"):
            self.next_id += 1
            task = {"id": self.next_id, "project_id": 7, "done": False, **body}
            self.tasks[task["id"]] = task
            return HttpResponse(201, json.dumps(task), {})
        if method == "POST" and path.startswith("/tasks/"):
            tid = int(path.rsplit("/", 1)[1])
            self.tasks[tid] = {**self.tasks[tid], **body}
            return HttpResponse(200, json.dumps(self.tasks[tid]), {})
        raise AssertionError(f"unexpected {method} {path}")

    def _page(self, rows, q):
        page = int(q.get("page", ["1"])[0])
        n = self.per_page
        pages = max(1, -(-len(rows) // n))
        return HttpResponse(200, json.dumps(rows[(page - 1) * n : page * n]),
                            {"x-pagination-total-pages": str(pages)})

    def calls(self, method, suffix=""):
        return [r for r in self.requests if r[0] == method and urllib.parse.urlparse(r[1]).path.endswith(suffix)]


def vk_with(fake):
    return Vikunja(BASE, TOKEN, transport=fake)


def problem(pid=PID, title="Ōkami & Friends™", kind="unmatched-deku", detail="no page found"):
    return Problem(pid, title, kind, detail)


def test_ensure_project_finds_existing_across_pages():
    fake = FakeVikunja(projects=[{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 9, "title": "Wishlist Sync"}])
    assert vk_with(fake).ensure_project() == 9
    assert not fake.calls("PUT")


def test_ensure_project_creates_when_missing():
    fake = FakeVikunja()
    assert vk_with(fake).ensure_project() == 7
    assert fake.calls("PUT", "/projects")[0][2] == {"title": "Wishlist Sync"}


def test_token_only_in_authorization_header():
    fake = FakeVikunja()
    vk = vk_with(fake)
    vk.ensure_project()
    for method, url, body, headers in fake.requests:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in url and TOKEN not in json.dumps(body)
    assert TOKEN not in repr(vk)


def test_upsert_is_idempotent_one_create_zero_updates():
    fake = FakeVikunja()
    vk = vk_with(fake)
    assert vk.upsert(f"{PID}:unmatched-deku", "Fix it", "line one\nline two") is True
    assert vk.upsert(f"{PID}:unmatched-deku", "Fix it", "line one\nline two") is False
    # even from a fresh client (state comes from the server)
    assert vk_with(fake).upsert(f"{PID}:unmatched-deku", "Fix it", "line one\nline two") is False
    assert len(fake.calls("PUT", "/tasks")) == 1
    assert not fake.calls("POST")


def test_upsert_updates_when_body_changes():
    fake = FakeVikunja()
    vk = vk_with(fake)
    vk.upsert("k:unmatched-ta", "T", "old")
    assert vk.upsert("k:unmatched-ta", "T", "new") is True
    assert len(fake.calls("PUT", "/tasks")) == 1
    (post,) = fake.calls("POST")
    assert "new" in post[2]["description"] and "old" not in post[2]["description"]
    assert "wsync-key: k:unmatched-ta" in post[2]["description"]


def test_marker_roundtrips_through_html_wrapping():
    fake = FakeVikunja()
    vk = vk_with(fake)
    vk.upsert("plan-review:dry-run", "Plan", "## Deku\n- add: A & B\n- add: C")
    desc = next(iter(fake.tasks.values()))["description"]
    assert desc.rstrip().endswith("<p>wsync-key: plan-review:dry-run</p>")
    assert set(vk_with(fake).open_tasks()) == {"plan-review:dry-run"}
    # the server may re-wrap or re-escape the HTML; the marker is still found
    for t in fake.tasks.values():
        t["description"] = f"<div>\n<p>stuff &amp; more</p>\n<p>wsync-key:&nbsp;{PID}:d2d-disputed </p>\n</div>"
    assert set(vk_with(fake).open_tasks()) == {f"{PID}:d2d-disputed"}


def test_open_tasks_paginates_and_ignores_foreign_tasks():
    tasks = [{"id": i, "project_id": 7, "done": False, "title": f"t{i}",
              "description": f"<p>wsync-key: P{i}:unmatched-ta</p>"} for i in range(1, 6)]
    tasks.append({"id": 50, "project_id": 7, "done": False, "title": "mine", "description": "<p>no marker</p>"})
    tasks.append({"id": 51, "project_id": 8, "done": False, "title": "other project",
                  "description": "<p>wsync-key: X:unmatched-ta</p>"})
    fake = FakeVikunja(projects=[{"id": 7, "title": "Wishlist Sync"}], tasks=tasks, per_page=2)
    got = vk_with(fake).open_tasks()
    assert set(got) == {f"P{i}:unmatched-ta" for i in range(1, 6)}
    assert len(fake.calls("GET", "/tasks")) == 4  # 6 + 1 rows in 2-per-page chunks


def test_resolved_problem_is_closed():
    fake = FakeVikunja()
    vk = vk_with(fake)
    sync_problem_tasks(vk, [problem(pid="A"), problem(pid="B", kind="unmatched-ta")])
    assert len(fake.tasks) == 2
    sync_problem_tasks(vk_with(fake), [problem(pid="B", kind="unmatched-ta")])
    done = {t["description"].count("wsync-key: A:unmatched-deku") for t in fake.tasks.values() if t["done"]}
    assert done == {1}
    assert sum(1 for t in fake.tasks.values() if not t["done"]) == 1
    assert len(fake.calls("PUT", "/tasks")) == 2


def test_close_sends_done_true_and_keeps_other_fields():
    fake = FakeVikunja()
    vk = vk_with(fake)
    vk.upsert("A:unmatched-deku", "Title", "body")
    vk.close("A:unmatched-deku")
    (post,) = fake.calls("POST")
    assert post[2]["done"] is True and post[2]["title"] == "Title"
    vk.close("A:unmatched-deku")  # already closed: nothing to do
    vk.close("nope:unmatched-deku")
    assert len(fake.calls("POST")) == 1


def test_problem_task_text_has_fix_hint_and_no_secrets():
    fake = FakeVikunja()
    sync_problem_tasks(vk_with(fake), [problem(), problem(pid="B", kind="unmatched-ta", title="T"),
                                       problem(pid="C", kind="d2d-disputed", title="D", detail="sources disagree")])
    blob = json.dumps(list(fake.tasks.values()))
    assert TOKEN not in blob
    t = next(t for t in fake.tasks.values() if PID in t["description"])
    assert "Ōkami &amp; Friends™" in t["description"]
    assert f'"{PID}": {{"deku": "&lt;slug&gt;"}}' in t["description"].replace("<code>", "").replace("</code>", "")
    assert "xbox-wishlist-sync overrides ConfigMap" in t["description"]
    assert "skip" in t["description"] and "no page found" in t["description"]


def test_http_error_is_reportable_without_token():
    class Bad:
        def request(self, *a, **k):
            return HttpResponse(401, "bad token tk_super-secret-token", {})

    with pytest.raises(ReportError) as e:
        Vikunja(BASE, TOKEN, transport=Bad()).ensure_project()
    assert TOKEN not in str(e.value) and "401" in str(e.value)


def entry(t, **kw):
    return Entry(t, t, **kw)


def test_summary_none_for_empty_plan():
    assert summary(Plan(), True) is None
    # paused removals are not changes
    p = Plan(deku=ListPlan(paused_removes=[entry("x")], pause_reason="r"))
    assert summary(p, True) is None


def test_summary_exact_string():
    plan = Plan(
        deku=ListPlan(adds=[entry("a"), entry("b")], updates=[(entry("c", format="digital"), entry("c", format="either"))]),
        ta=ListPlan(removes=[entry("z")]),
    )
    assert summary(plan, True) == "+2 Deku, −1 TA, 1 format change"
    assert summary(plan, False) == "Dry run: +2 Deku, −1 TA, 1 format change"


def full_plan():
    return Plan(
        deku=ListPlan(
            adds=[entry("new-game", format="either")],
            removes=[entry("old-game")],
            updates=[(entry("u", format="digital"), entry("u", format="either"))],
            paused_removes=[entry("held")],
            pause_reason="ambiguous match for: Mystery",
        ),
        ta=ListPlan(adds=[entry("ta-new")]),
    )


def test_plan_review_idempotent_and_returns_changed_flag():
    fake = FakeVikunja()
    vk = vk_with(fake)
    assert plan_review_task(vk, full_plan()) is True
    assert plan_review_task(vk, full_plan()) is False
    assert plan_review_task(vk_with(fake), full_plan()) is False
    assert len(fake.calls("PUT", "/tasks")) == 1 and not fake.calls("POST")
    desc = next(iter(fake.tasks.values()))["description"]
    for needle in ("new-game", "old-game", "held", "ambiguous match for: Mystery", "wsync-key: plan-review:dry-run"):
        assert needle in desc
    changed = full_plan()
    changed.ta.adds.append(entry("another"))
    assert plan_review_task(vk_with(fake), changed) is True
    assert len(fake.calls("POST")) == 1


def test_plan_review_closed_when_not_dry_run_or_plan_empty():
    fake = FakeVikunja()
    vk = vk_with(fake)
    plan_review_task(vk, full_plan())
    assert plan_review_task(vk_with(fake), full_plan(), dry_run=False) is False
    assert all(t["done"] for t in fake.tasks.values())
    fake2 = FakeVikunja()
    plan_review_task(vk_with(fake2), full_plan())
    assert plan_review_task(vk_with(fake2), Plan()) is False
    assert all(t["done"] for t in fake2.tasks.values())


def test_notify_posts_json():
    sent = []

    class T:
        def request(self, method, url, body=None, headers=None):
            sent.append((method, url, body))
            return HttpResponse(200, "ok", {})

    notify("http://apprise/notify/x", "T", "B", transport=T())
    assert sent == [("POST", "http://apprise/notify/x", {"title": "T", "body": "B"})]


def test_notify_swallows_failures(caplog):
    class Boom:
        def request(self, *a, **k):
            raise ReportError("network error: URLError")

    notify("http://apprise/notify/secretkey", "T", "B", transport=Boom())

    class Bad:
        def request(self, *a, **k):
            return HttpResponse(500, "x", {})

    notify("http://apprise/notify/secretkey", "T", "B", transport=Bad())
    assert "secretkey" not in caplog.text


def test_urllib_transport_maps_network_errors(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(report.urllib.request, "urlopen", boom)
    with pytest.raises(ReportError):
        report.UrllibTransport().request("GET", "http://127.0.0.1:1/x")


def test_problem_sync_leaves_the_plan_review_task_alone():
    fake = FakeVikunja()
    plan_review_task(vk_with(fake), full_plan())
    sync_problem_tasks(vk_with(fake), [])
    assert not any(t["done"] for t in fake.tasks.values())


# ---- hardening ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["", "apprise-no-scheme/notify/SECRETKEY", "ftp://h/notify/SECRETKEY"])
def test_notify_with_bad_url_never_raises_or_logs_the_url(url, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        notify(url, "T", "B")
    assert "SECRETKEY" not in caplog.text and url not in caplog.text or url == ""
    assert "notification failed" in caplog.text


def test_notify_swallows_unexpected_exception_types(caplog):
    class Weird:
        def request(self, *a, **k):
            raise KeyError("http://apprise/notify/SECRETKEY")

    notify("http://apprise/notify/SECRETKEY", "T", "B", transport=Weird())
    assert "SECRETKEY" not in caplog.text and "KeyError" in caplog.text


@pytest.mark.parametrize("exc", [ValueError("unknown url type: 'x/SECRETKEY'"),
                                 __import__("http.client").client.IncompleteRead(b"x")])
def test_urllib_transport_wraps_value_and_http_errors(monkeypatch, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(report.urllib.request, "urlopen", boom)
    with pytest.raises(ReportError) as e:
        report.UrllibTransport().request("POST", "http://h/notify/SECRETKEY", {"a": 1})
    assert "SECRETKEY" not in str(e.value) and type(exc).__name__ in str(e.value)


def test_duplicate_open_tasks_keep_lowest_id_and_close_the_rest():
    title, body = report.problem_task(problem(pid="A"))
    desc = report.render_description(body, "A:unmatched-deku")
    tasks = [{"id": i, "project_id": 7, "done": False, "title": title, "description": desc} for i in (30, 10, 20)]
    fake = FakeVikunja(projects=[{"id": 7, "title": "Wishlist Sync"}], tasks=tasks, per_page=50)
    sync_problem_tasks(vk_with(fake), [problem(pid="A")])
    assert [t["id"] for t in fake.tasks.values() if not t["done"]] == [10]
    assert sorted(r[1].rsplit("/", 1)[1] for r in fake.calls("POST")) == ["20", "30"]
    assert not fake.calls("PUT", "/tasks")


def test_duplicate_plan_review_tasks_are_reconciled_and_stale_key_closes_all():
    desc = "<p>wsync-key: plan-review:dry-run</p>"
    tasks = [{"id": i, "project_id": 7, "done": False, "title": "T", "description": desc} for i in (5, 6)]
    fake = FakeVikunja(projects=[{"id": 7, "title": "Wishlist Sync"}], tasks=tasks, per_page=50)
    plan_review_task(vk_with(fake), full_plan())
    assert [t["id"] for t in fake.tasks.values() if not t["done"]] == [5]
    stale = "<p>wsync-key: Z:unmatched-ta</p>"
    fake = FakeVikunja(projects=[{"id": 7, "title": "Wishlist Sync"}], per_page=50,
                       tasks=[{"id": i, "project_id": 7, "done": False, "title": "T", "description": stale}
                              for i in (1, 2)])
    sync_problem_tasks(vk_with(fake), [])
    assert all(t["done"] for t in fake.tasks.values())


class Canned:
    def __init__(self, *responses):
        self.responses = list(responses)

    def request(self, *a, **k):
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


@pytest.mark.parametrize("resp", [
    HttpResponse(200, "", {}),
    HttpResponse(200, "{}", {}),
    HttpResponse(200, "not json", {}),
    HttpResponse(200, "[1, 2]", {}),
    HttpResponse(200, "[]", {"x-pagination-total-pages": "many"}),
    HttpResponse(200, '[{"title": "x"}]', {"x-pagination-total-pages": "1"}),
])
def test_malformed_list_responses_raise_report_error(resp):
    with pytest.raises(ReportError):
        Vikunja(BASE, TOKEN, transport=Canned(resp)).ensure_project()


@pytest.mark.parametrize("created", ["", "null", "[]", '{"title": "no id"}', '{"id": "x"}'])
def test_malformed_create_responses_raise_report_error(created):
    ok_list = HttpResponse(200, "[]", {"x-pagination-total-pages": "1"})
    with pytest.raises(ReportError):
        Vikunja(BASE, TOKEN, transport=Canned(ok_list, HttpResponse(201, created, {}))).ensure_project()
    vk = Vikunja(BASE, TOKEN, transport=Canned(
        ok_list, HttpResponse(201, '{"id": 7}', {}), ok_list, HttpResponse(201, created, {})))
    with pytest.raises(ReportError):
        vk.upsert("k:unmatched-ta", "T", "b")


def test_malformed_open_task_row_raises_report_error():
    vk = Vikunja(BASE, TOKEN, transport=Canned(
        HttpResponse(200, '[{"id": 7, "title": "Wishlist Sync"}]', {"x-pagination-total-pages": "1"}),
        HttpResponse(200, '[{"project_id": 7, "done": false, "description": "<p>wsync-key: a:b</p>"}]',
                     {"x-pagination-total-pages": "1"})))
    with pytest.raises(ReportError):
        vk.open_tasks()


def test_plan_review_body_is_deterministic_for_reordered_plans():
    def make(order):
        a, b, c = entry("alpha"), entry("beta"), entry("gamma")
        u1 = (entry("u1", format="digital"), entry("u1", format="either"))
        u2 = (entry("u2", format="digital"), entry("u2", format="either"))
        seq = lambda xs: [xs[i] for i in order]  # noqa: E731
        return Plan(
            deku=ListPlan(adds=seq([a, b, c]), removes=seq([c, a, b]), updates=[u1, u2] if order[0] == 0 else [u2, u1],
                          paused_removes=seq([b, c, a]), pause_reason="ambiguous match for: " + ", ".join(
                              ["Zed", "Alpha", "Mid"] if order[0] == 0 else ["Mid", "Zed", "Alpha"])),
            ta=ListPlan(adds=seq([c, b, a])),
            skipped_unannounced=["Q", "B"] if order[0] == 0 else ["B", "Q"],
        )

    p1, p2 = make([0, 1, 2]), make([2, 0, 1])
    assert report.plan_review_body(p1) == report.plan_review_body(p2)
    fake = FakeVikunja()
    assert plan_review_task(vk_with(fake), p1) is True
    assert plan_review_task(vk_with(fake), p2) is False
    assert not fake.calls("POST")


def test_sync_keeps_tasks_of_kinds_the_run_could_not_check():
    fake = FakeVikunja()
    vk = vk_with(fake)
    sync_problem_tasks(vk, [problem(), problem(pid="B", kind="unmatched-ta", title="T"),
                            problem(pid="C", kind="d2d-disputed", title="D")])
    # The next run couldn't read TA or check D2D: only Deku's resolved problem closes.
    sync_problem_tasks(vk_with(fake), [], keep_kinds={"unmatched-ta", "d2d-disputed"})
    open_keys = {report.task_key(t["description"]) for t in fake.tasks.values() if not t["done"]}
    assert open_keys == {"B:unmatched-ta", "C:d2d-disputed"}


def test_a_flaky_lookup_does_not_change_the_review_body():
    """Which item's lookup flaked this hour must not show up in the review (no spurious ping)."""
    from wsync.feed import XboxItem
    from wsync.match import Match
    from wsync.plan import build_plan, transient

    items = [XboxItem(pid, title, "game", "released", ("xbox",)) for pid, title in
             (("A", "Alpha"), ("B", "Beta"), ("C", "Gamma"))]
    current = {"stale": Entry("stale", "Stale", "digital", frozenset())}
    ambiguous = Match("ambiguous", None, ["Alpha One", "Alpha Two"])
    none = Match("none", None, [])

    def body(flaky):
        dm = {"A": ambiguous, "B": none, "C": none}
        dm[flaky] = transient(f"lookup failed ({flaky}Error)")
        return report.plan_review_body(build_plan(items, dm, {}, {}, current, {}, {}))

    assert body("B") == body("C")
    assert "Alpha" in body("B")  # a genuinely ambiguous title is still named
    assert "Beta" not in body("B") and "Gamma" not in body("C")
    assert "1 lookup retried next run" in body("B")
    fake = FakeVikunja()
    assert plan_review_task(vk_with(fake), build_plan(items, {"A": ambiguous, "B": transient("x"), "C": none},
                                                      {}, {}, current, {}, {})) is True
    assert plan_review_task(vk_with(fake), build_plan(items, {"A": ambiguous, "B": none, "C": transient("y")},
                                                      {}, {}, current, {}, {})) is False


# ---- Deku collection review task --------------------------------------------------------------


def test_collection_review_upserts_in_place_and_returns_changed_flag():
    from wsync.report import COLLECTION_REVIEW_KEY, collection_review_task

    fake = FakeVikunja()
    assert collection_review_task(vk_with(fake), "## Would add (1)\n- Alpha (`alpha`, digital, xbox_series)") is True
    assert collection_review_task(vk_with(fake), "## Would add (1)\n- Alpha (`alpha`, digital, xbox_series)") is False
    assert len(fake.calls("PUT", "/tasks")) == 1 and not fake.calls("POST")
    task = next(iter(fake.tasks.values()))
    assert task["title"] == "Review the Deku collection plan"
    assert f"wsync-key: {COLLECTION_REVIEW_KEY}" in task["description"]
    assert COLLECTION_REVIEW_KEY == "collection-review:dry-run"
    assert collection_review_task(vk_with(fake), "## Would add (0)") is True
    assert len(fake.calls("POST")) == 1 and len(fake.tasks) == 1


def test_collection_review_closed_on_a_live_run():
    from wsync.report import collection_review_task

    fake = FakeVikunja()
    collection_review_task(vk_with(fake), "plan")
    assert collection_review_task(vk_with(fake), None, dry_run=False) is False
    assert all(t["done"] for t in fake.tasks.values())


def test_problem_sync_and_wishlist_review_leave_the_collection_review_task_alone():
    from wsync.report import collection_review_task

    fake = FakeVikunja()
    collection_review_task(vk_with(fake), "plan")
    sync_problem_tasks(vk_with(fake), [])
    sync_problem_tasks(vk_with(fake), [problem()], keep_kinds=())
    plan_review_task(vk_with(fake), Plan())  # empty wishlist plan: closes only its own task
    plan_review_task(vk_with(fake), full_plan(), dry_run=False)
    coll = [t for t in fake.tasks.values() if t["title"] == "Review the Deku collection plan"]
    assert len(coll) == 1 and not coll[0]["done"]

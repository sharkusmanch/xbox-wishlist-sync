"""The library step's reporting: the dry-run plan task, the live "check by hand" task (decision A), the Microsoft
sign-in task and the pings. Task text may carry Store product IDs (they are how an override is keyed); log lines
carry counts and titles only. A reporting failure is an issue of its own and never breaks the step."""

import hashlib
import json

from wsync.report import LIBRARY_CHECK_KEY, LIBRARY_CHECK_TITLE, LIBRARY_REVIEW_KEY, LIBRARY_REVIEW_TITLE, \
    LIBRARY_SIGNIN_KEY, LIBRARY_SIGNIN_TITLE

SIGNIN_BODY = (
    "The library step could not sign in to Microsoft: the stored refresh token was rejected and the seed in "
    "XBOX_REFRESH_TOKEN did not work either. Nothing is read or written until it is fixed.\n\n"
    "Fix: on the games repo run `scripts/xbox-library.py --token-file <tmp> login`, approve the code at "
    "microsoft.com/link, then put the token from that file into OpenBao `secret/apps/tools/xbox-wishlist-sync` "
    "as `XBOX_REFRESH_TOKEN`. The step tries again at the next run once the secret changes, else at tomorrow's "
    "pass. This task closes itself after the first working sign-in."
)


def _fmt_sets(a) -> str:
    return ", ".join(label for _, label in a.sets)


def _lists(plan, rows):
    """(would add, would fix, check by hand): sorted tuples of plain text."""
    rows = rows or {}
    adds, fixes = [], []
    for a in plan.actions:
        if a.kind == "add":
            adds.append((a.title, a.slug, _fmt_sets(a)))
        else:
            row = rows.get(a.ta_id)
            was = f"{row.ownership or 'no ownership'}, {row.media or 'no media'}" if row else "unknown"
            fixes.append((a.title, was, _fmt_sets(a)))
    check = [(r.title, r.pid, r.kind, r.reason) for r in plan.review]
    return sorted(adds), sorted(fixes), sorted(check)


def review_signature(plan, rows=None) -> str:
    """What the "plan ready" ping tracks: the three lists only; the waiting count moves as lookups drain."""
    adds, fixes, check = _lists(plan, rows)
    return hashlib.sha256(json.dumps([adds, fixes, check], ensure_ascii=False).encode("utf-8")).hexdigest()


def review_body(plan, rows=None) -> tuple[str, dict]:
    """The dry-run task text (deterministic) and its counts. `rows` maps TA game id -> its collection row."""
    adds, fixes, check = _lists(plan, rows)
    counts = {"would_add": len(adds), "would_fix": len(fixes), "check": len(check), "ok": plan.ok,
              "waiting": len(plan.lookups)}
    lines = [
        "Dry run: nothing has been written to the TrueAchievements Game Collection. Source: the Microsoft "
        "licences and orders.",
        f"Summary: would add {len(adds)}; would fix {len(fixes)}; check by hand {len(check)}; already right "
        f"{plan.ok}; waiting for a page lookup {len(plan.lookups)}",
        f"## Would add ({len(adds)})",
    ]
    lines += [f"- {t} (`{s}`, {sets})" for t, s, sets in adds]
    lines.append(f"## Would fix ({len(fixes)})")
    lines += [f"- {t}: {was} -> {to}" for t, was, to in fixes]
    lines.append(f"## Check by hand ({len(check)})")
    if check:
        lines.append("Nothing is written for these. Fix one with a `library` override keyed by its Store product "
                     "ID (`{\"<productId>\": {\"ta\": \"<slug>\" | \"skip\"}}`).")
    lines += [f"- {t} (`{p}`, {k}): {why}" for t, p, k, why in check]
    lines += ["## Counts", f"- Already right on TrueAchievements: {plan.ok}",
              f"- Waiting for a page lookup: {len(plan.lookups)}"]
    return "\n".join(lines), counts


def _unseen(plan, seen) -> list:
    return sorted((r for r in plan.review if r.pid not in seen and r.kind != "no-page"),
                  key=lambda r: (r.title, r.pid, r.kind))


def check_body(plan, seen) -> str | None:
    """The live "check by hand" task text: review items not seen at go-live, no-page games never. None when empty."""
    items = _unseen(plan, set(seen))
    if not items:
        return None
    lines = [f"{len(items)} game(s) bought since the library sync went live need a look. Nothing is written for "
             "them. Fix one with a `library` override keyed by its Store product ID "
             "(`{\"<productId>\": {\"ta\": \"<slug>\" | \"skip\"}}`)."]
    lines += [f"- {r.title} (`{r.pid}`): {r.reason}" for r in items]
    return "\n".join(lines)


def _check_signature(plan, seen) -> str:
    items = [[r.title, r.pid, r.kind] for r in _unseen(plan, set(seen))]
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest()


def baseline(lib: dict, plan, *, dry: bool) -> None:
    """Decision A: the first live pass marks every game the plan has not settled as seen, before any write:
    every review item and every game still waiting for a page lookup. Later runs never change it."""
    if dry or lib.get("seen") is not None:
        return
    pids = {r.pid for r in plan.review}
    for g in plan.lookups:
        pids.update(g.pids)
    lib["seen"] = sorted(pids)


def _task(run, key: str, title: str, body: str | None) -> bool | None:
    """Upsert (or close, body None) one task; None when it failed (an issue)."""
    try:
        return run.reporter.library_task(key, title, body)
    except Exception as e:  # noqa: BLE001 - reporting must not take the step down
        run.issue(f"Vikunja library task '{title}' not {'closed' if body is None else 'updated'} "
                  f"({type(e).__name__})")
        return None


def report_plan(run, res, lib: dict, plan, rows: list) -> None:
    by_id = {r.ta_id: r for r in rows}
    if res.dry_run:
        _dry(run, lib, plan, by_id)
    else:
        _live(run, res, lib, plan)


def _dry(run, lib: dict, plan, rows: dict) -> None:
    lib.pop("check_sig", None)
    _task(run, LIBRARY_CHECK_KEY, LIBRARY_CHECK_TITLE, None)
    body, counts = review_body(plan, rows)
    sig = review_signature(plan, rows)
    if _task(run, LIBRARY_REVIEW_KEY, LIBRARY_REVIEW_TITLE, body) is None:
        return
    if sig != lib.get("review_sig"):  # pinged only when the lists change, not the waiting count
        lib["review_sig"] = sig
        if counts["would_add"] or counts["would_fix"] or counts["check"]:
            run.notify("Library sync plan ready",
                       f"Dry run: would add {counts['would_add']} to the TA library; would fix "
                       f"{counts['would_fix']}; check by hand {counts['check']}; waiting for a lookup "
                       f"{counts['waiting']}.\nReview the plan in Vikunja (Wishlist Sync).")


def _live(run, res, lib: dict, plan) -> None:
    lib.pop("review_sig", None)  # a later dry run pings its first plan again
    _task(run, LIBRARY_REVIEW_KEY, LIBRARY_REVIEW_TITLE, None)
    seen = lib.get("seen") or ()
    body = check_body(plan, seen)
    if _task(run, LIBRARY_CHECK_KEY, LIBRARY_CHECK_TITLE, body) is not None:
        if body is None:
            lib.pop("check_sig", None)
        else:
            sig = _check_signature(plan, seen)
            if sig != lib.get("check_sig"):
                lib["check_sig"] = sig
                run.notify(f"Library sync: {len(_unseen(plan, set(seen)))} to check by hand", body)
    if res.written:
        run.notify("Library sync", f"+{len(res.written)} TA library\n"
                   + "\n".join(f"- {t}" for t in sorted(res.written)))


def signin_failed(run, *, new: bool) -> None:
    """The sign-in failed this run: the task (each failing run, unchanged text is no change), one ping per episode."""
    _task(run, LIBRARY_SIGNIN_KEY, LIBRARY_SIGNIN_TITLE, SIGNIN_BODY)
    if new:
        run.notify("Library sync: Microsoft sign-in needed",
                   "The library step could not sign in to Microsoft. See the task in Vikunja (Wishlist Sync) "
                   "for the fix.")


def signin_ok(run) -> None:
    _task(run, LIBRARY_SIGNIN_KEY, LIBRARY_SIGNIN_TITLE, None)

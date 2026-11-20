"""The library step's reporting: the dry-run plan task, the live "check by hand" task (decision A), the Microsoft
sign-in task and the pings. Task text may carry Store product IDs (they are how an override is keyed); log lines
carry counts and titles only. A reporting failure is an issue of its own and never breaks the step."""

import hashlib
import json

from wsync.owned import shown_title
from wsync.report import LIBRARY_CHECK_KEY, LIBRARY_CHECK_TITLE, LIBRARY_REVIEW_KEY, LIBRARY_REVIEW_TITLE, \
    LIBRARY_SIGNIN_KEY, LIBRARY_SIGNIN_TITLE

SIGNIN_BODY = (
    "The library step could not sign in to Microsoft: the stored refresh token was rejected and the seed in "
    "XBOX_REFRESH_TOKEN did not work either. Nothing is read or written until it is fixed.\n\n"
    "Fix: on the games repo run `scripts/xbox-library.py --token-file <tmp> login`, approve the code at "
    "microsoft.com/link, then put the token from that file into OpenBao `secret/apps/tools/xbox-wishlist-sync` "
    "as `XBOX_REFRESH_TOKEN`, then delete the temp file (it holds a live refresh token plus the XUID and gamertag). "
    "The step tries again at the next run once the secret changes, else at tomorrow's "
    "pass. This task closes itself after the first working sign-in."
)


def _fmt_sets(a) -> str:
    return ", ".join(label for _, label in a.sets)


def _key(game) -> str:
    return min(game.pids)  # the game's lookup key (`library.lookup_key`)


def _lists(lib: dict, plan, rows):
    """(would add, would fix, check by hand): sorted lists of plain-text entries.

    A game waiting for a (re-)lookup keeps the entries it last had (`lib["listed"]`, by game key), so a pending
    lookup never drops an item from a report or its signature. Updated here: the plan's games, plus the carried
    ones; everything else is forgotten."""
    rows = rows or {}
    now: dict[str, dict] = {}

    def entry(key):
        return now.setdefault(key, {"add": [], "fix": [], "check": []})
    for a in plan.actions:
        if a.kind == "add":
            entry(_key(a.game))["add"].append([a.title, a.slug, _fmt_sets(a)])
        else:
            row = rows.get(a.ta_id)
            was = f"{row.ownership or 'no ownership'}, {row.media or 'no media'}" if row else "unknown"
            entry(_key(a.game))["fix"].append([a.title, was, _fmt_sets(a)])
    for r in plan.review:
        entry(r.pid)["check"].append([r.title, r.pid, r.kind, r.reason])
    stored = lib.get("listed") if isinstance(lib.get("listed"), dict) else {}
    for g in plan.lookups:
        if _key(g) not in now and _key(g) in stored:
            now[_key(g)] = stored[_key(g)]
    lib["listed"] = now
    out = tuple(sorted(item for e in now.values() for item in e[kind]) for kind in ("add", "fix", "check"))
    return out


def _signature(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def review_body(plan, rows=None, lists=None) -> tuple[str, dict]:
    """The dry-run task text (deterministic) and its counts. `rows` maps TA game id -> its collection row;
    `lists` the (add, fix, check) entries when carried-forward ones are wanted."""
    adds, fixes, check = lists if lists is not None else _lists({}, plan, rows)
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


def _unseen(check, seen) -> list:
    """Check entries to list live: not seen at go-live and not "no-page"; a mistyped override always shows."""
    return [c for c in check if c[2] == "override" or (c[1] not in seen and c[2] != "no-page")]


def check_body(check, seen) -> str | None:
    """The live "check by hand" task text from the check entries. None when nothing is left to list."""
    items = _unseen(check, set(seen))
    if not items:
        return None
    lines = [f"{len(items)} game(s) need a look. Nothing is written for them. Fix one with a `library` override "
             "keyed by its Store product ID (`{\"<productId>\": {\"ta\": \"<slug>\" | \"skip\"}}`)."]
    lines += [f"- {t} (`{p}`): {why}" for t, p, k, why in items]
    return "\n".join(lines)


def baseline(lib: dict, plan, *, dry: bool) -> None:
    """Decision A: the first live run marks seen the review items present when it starts, from its plan before its
    lookups and writes: the plan's, and the ones a game waiting for a re-lookup carries from earlier runs (`_lists`),
    which a dry run already listed. A game waiting for its first lookup is not marked, the ones that run looks up
    included: a review item such a lookup produces reaches the check task. Later runs never change it."""
    if dry or lib.get("seen") is not None:
        return
    pids = {r.pid for r in plan.review}
    stored = lib.get("listed") if isinstance(lib.get("listed"), dict) else {}
    for g in plan.lookups:
        carried = stored.get(_key(g))
        if isinstance(carried, dict):
            pids.update(c[1] for c in carried.get("check") or () if isinstance(c, list) and len(c) > 1)
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
    lists = _lists(lib, plan, {r.ta_id: r for r in rows})
    if res.dry_run:
        _dry(run, res, lib, plan, lists)
    else:
        _live(run, res, lib, lists)


def _dry(run, res, lib: dict, plan, lists) -> None:
    lib.pop("check_sig", None)
    _task(run, LIBRARY_CHECK_KEY, LIBRARY_CHECK_TITLE, None)
    body, counts = review_body(plan, lists=lists)
    sig = _signature(*lists)
    if _task(run, LIBRARY_REVIEW_KEY, LIBRARY_REVIEW_TITLE, body) is None:
        return
    if res.pending:
        return  # the pass is still growing the lists: one ping, when the plan is complete (a failed lookup aside)
    if sig != lib.get("review_sig"):
        lib["review_sig"] = sig
        if counts["would_add"] or counts["would_fix"] or counts["check"]:
            run.notify("Library sync plan ready",
                       f"Dry run: would add {counts['would_add']} to the TA library; would fix "
                       f"{counts['would_fix']}; check by hand {counts['check']}.\n"
                       "Review the plan in Vikunja (Wishlist Sync).")


def _live(run, res, lib: dict, lists) -> None:
    lib.pop("review_sig", None)  # a later dry run pings its first plan again
    _task(run, LIBRARY_REVIEW_KEY, LIBRARY_REVIEW_TITLE, None)
    seen = set(lib.get("seen") or ())
    check = lists[2]
    body = check_body(check, seen)
    if _task(run, LIBRARY_CHECK_KEY, LIBRARY_CHECK_TITLE, body) is not None:
        if body is None:
            lib.pop("check_sig", None)
        else:
            items = _unseen(check, seen)
            sig = _signature([[t, p, k] for t, p, k, _ in items])
            if sig != lib.get("check_sig"):
                lib["check_sig"] = sig
                run.notify(f"Library sync: {len(items)} to check by hand",
                           "\n".join(f"- {shown_title(t, (p,))}" for t, p, *_ in items)
                           + "\nSee the task in Vikunja (Wishlist Sync).")
    if res.written:
        run.notify("Library sync", f"+{len(res.written)} TA library\n"
                   + "\n".join(f"- {t}" for t in sorted(res.written)))


def signin_failed(run, lib: dict, *, new: bool) -> None:
    """The sign-in failed this run: the task (each failing run, unchanged text is no change), one ping per episode.
    `lib["signin_task"]` records that the task may be open, until a close succeeds."""
    lib["signin_task"] = True
    _task(run, LIBRARY_SIGNIN_KEY, LIBRARY_SIGNIN_TITLE, SIGNIN_BODY)
    if new:
        run.notify("Library sync: Microsoft sign-in needed",
                   "The library step could not sign in to Microsoft. See the task in Vikunja (Wishlist Sync) "
                   "for the fix.")


def signin_ok(run, lib: dict) -> None:
    """A working sign-in: close the sign-in task if it may be open; a failed close is retried next run."""
    if lib.get("signin_task") and _task(run, LIBRARY_SIGNIN_KEY, LIBRARY_SIGNIN_TITLE, None) is not None:
        lib.pop("signin_task", None)

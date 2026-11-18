"""Persistent run state, stored as JSON and replaced atomically."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class State:
    # productId -> {"deku": uuid|None, "deku_slug": str|None, "ta": {"game": int, "dlc": int}|None}
    mapping: dict[str, dict] = field(default_factory=dict)
    # productId -> {"verdict": str, "checked": iso}
    d2d: dict[str, dict] = field(default_factory=dict)
    # {"fetched": iso, "yes": [...], "no": [...]}
    ta_lists: dict = field(default_factory=dict)
    last_feed_count: int | None = None
    # TA game slug -> TA game id, for reading DLC rows on the TA wishlist
    ta_game_ids: dict[str, int] = field(default_factory=dict)
    # digest of the last run's failure (None after a clean run): failure pings are deduplicated
    last_failure: str | None = None
    # Deku collection step. {"fetched": iso, "rows": [OwnedRow as dict]}: the last full TA read, plus
    # {"last_attempt": iso, "error": str} after a failed read (the next try waits 6 h)
    ta_collection: dict = field(default_factory=dict)
    # TA game id (as text) -> {"status": "matched"|"added"|"found"|"none", "slug": str|None, "checked": iso,
    #                          "title": str, ...}  (see wsync.collection)
    collection: dict[str, dict] = field(default_factory=dict)
    # Deku item slug -> {"since": iso, "titles": [TA titles], "baseline": [owned public ids before]}:
    # collection adds started on that item and not confirmed. Written (and saved) before the create
    # request, removed only once the item page or the share page shows an Xbox / unset entry: the step never calls own()
    # on an item listed here, whatever TA row, override or redirect leads to it.
    collection_unconfirmed: dict[str, dict] = field(default_factory=dict)
    # digest of the dry-run collection review's lists (would add, unmatched, check by hand): the
    # "plan ready" ping goes out only when it changes
    collection_review: str | None = None
    # the same digest for the Xbox 360 review (COLLECTION_360_DRY_RUN): would add and unmatched 360 games
    collection_360_review: str | None = None
    # Library step (Microsoft -> TA, see wsync.library):
    #   "last_pass": "YYYY-MM-DD"  the UTC day of the last complete daily pass;
    #   "failed": {"at": iso, "issue": str}  a failed Microsoft or TA read: repeated, without a read, for 6 h;
    #   "signin_failed": {"day": "YYYY-MM-DD", "seed": SHA-256 of XBOX_REFRESH_TOKEN}  a sign-in that needed a new
    #              token: not tried again that day with the same seed; removed by a working sign-in;
    #   "pass": {"day": "YYYY-MM-DD", "rows": [OwnedRow as dict], "done": [TA game id]}  the TA collection the
    #              day's pass read in its first run, and the TA ids it has written since; dropped when it completes;
    #   "lists": {"games-with-gold" | "ea-play": {"fetched": iso, "ids": [TA game id], "tried": iso}};
    #   "pages": {lookup key: {"checked": iso, "slugs": [addresses tried], "pages": [TAGame as dict]}};
    #   "writing": {TA game id: {"kind", "slug", "ta_id", "title", "sets", "expect" (the labels the plan saw per
    #              field), "pids", "since", "added", "sets_done", "tried" (the pass day a write error kept it)}}
    #              write-ahead: saved before a write's first request and after each, removed after its last;
    #   "seen": [Store product id]  review items already reported (decision A).
    library: dict = field(default_factory=dict)


def load_state(path: Path) -> State:
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State()
    data = json.loads(raw)
    return State(
        mapping=data.get("mapping") or {},
        d2d=data.get("d2d") or {},
        ta_lists=data.get("ta_lists") or {},
        last_feed_count=data.get("last_feed_count"),
        ta_game_ids=data.get("ta_game_ids") or {},
        last_failure=data.get("last_failure"),
        ta_collection=data.get("ta_collection") or {},
        collection=data.get("collection") or {},
        collection_unconfirmed=data.get("collection_unconfirmed") or {},
        collection_review=data.get("collection_review"),
        collection_360_review=data.get("collection_360_review"),
        library=data.get("library") or {},
    )


def save_state(path: Path, state: State) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(state), fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

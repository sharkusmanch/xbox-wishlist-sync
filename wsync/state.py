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

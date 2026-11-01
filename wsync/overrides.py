"""Manual match overrides: a small JSON file mapping Xbox productIds to Deku / TA pages.

    {
      "9NB0ZZ452WF1": {"deku": "fable", "ta": "fable-xbox-series-x-s"},
      "9P8LNZ7X5GVP": {"deku": "skip", "ta": "skip", "note": "unannounced"},
      "9ABCDEF12345": {"ta": "elden-ring", "ta_dlc": 1234}
    }

- `deku`: a Deku Deals item slug, or "skip".
- `ta`: a TrueAchievements game slug (for DLC, the parent game's slug), or "skip".
- `ta_dlc`: a TrueAchievements DLC id; forces that pack for a DLC item.
- `note`: free text for humans, ignored.

"skip" means the item isn't mirrored to that list at all: no lookup, no problem task, and an
entry for it already on that list is removed like any other entry that isn't on the Xbox list.

A missing file means no overrides. A file that isn't valid is an error: the run must not guess.
"""

from __future__ import annotations

import json
from pathlib import Path

SKIP = "skip"
_KEYS = {"deku", "ta", "ta_dlc", "note"}


class OverridesError(Exception):
    """The overrides file exists but can't be used."""


def _slug(pid: str, key: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OverridesError(f"override {pid}: {key} must be a non-empty slug or \"skip\"")
    return value.strip()


def load_overrides(path: Path) -> dict[str, dict]:
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise OverridesError(f"overrides file unreadable ({type(e).__name__})") from None
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise OverridesError(f"overrides file is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise OverridesError("overrides file must be a JSON object keyed by productId")
    out: dict[str, dict] = {}
    for pid, entry in data.items():
        if not pid.strip():
            raise OverridesError("override with an empty productId")
        if not isinstance(entry, dict):
            raise OverridesError(f"override {pid}: must be an object")
        unknown = set(entry) - _KEYS
        if unknown:
            raise OverridesError(f"override {pid}: unknown keys {sorted(unknown)}")
        clean: dict = {}
        for key in ("deku", "ta"):
            if key in entry:
                clean[key] = _slug(pid, key, entry[key])
        if "ta_dlc" in entry:
            dlc = entry["ta_dlc"]
            if not isinstance(dlc, int) or isinstance(dlc, bool) or dlc < 1:
                raise OverridesError(f"override {pid}: ta_dlc must be a positive integer")
            if clean.get("ta") == SKIP:
                raise OverridesError(f"override {pid}: ta_dlc conflicts with ta \"skip\"")
            clean["ta_dlc"] = dlc
        if "note" in entry and not isinstance(entry["note"], str):
            raise OverridesError(f"override {pid}: note must be text")
        out[pid.strip()] = clean
    return out

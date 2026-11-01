"""Load the Xbox wishlist feed, refusing to proceed on a stale, empty or shrunken file."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

GAME_KINDS = frozenset({"game", "bundle", "demo"})


class FeedError(Exception):
    """The feed can't be trusted; the run must abort without writing anything."""


@dataclass(frozen=True)
class XboxItem:
    product_id: str
    title: str
    product_kind: str
    kind: str
    platforms: tuple[str, ...]


def is_dlc(item: XboxItem) -> bool:
    return item.product_kind not in GAME_KINDS


def _parse(record: object) -> XboxItem:
    if not isinstance(record, dict):
        raise FeedError(f"feed record is not an object: {record!r}")
    for key in ("productId", "title"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise FeedError(f"feed record missing {key}: {record!r}")
    return XboxItem(
        product_id=record["productId"],
        title=record["title"],
        product_kind=record.get("productKind") or "game",
        kind=record.get("kind") or "",
        platforms=tuple(record.get("platforms") or ()),
    )


def load_feed(
    path: Path,
    *,
    max_age: timedelta,
    last_count: int | None,
    min_ratio: float = 0.7,
) -> list[XboxItem]:
    path = Path(path)
    try:
        age = time.time() - path.stat().st_mtime
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FeedError(f"feed unreadable: {exc}") from exc
    if age > max_age.total_seconds():
        raise FeedError(f"feed is stale: {age / 3600:.1f} h old, limit {max_age}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise FeedError(f"feed is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise FeedError("feed is not a JSON list")
    if not data:
        raise FeedError("feed is empty")
    items = [_parse(r) for r in data]
    check_count(len(items), last_count, min_ratio)
    return items


def check_count(count: int, last_count: int | None, min_ratio: float = 0.7) -> None:
    """The shrink guard: refuse a feed much shorter than the last accepted one."""
    if last_count is not None and count < last_count * min_ratio:
        raise FeedError(
            f"feed shrunk: {count} items, last run had {last_count} "
            f"(minimum ratio {min_ratio})"
        )

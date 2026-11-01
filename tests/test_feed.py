import json
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from wsync.feed import FeedError, XboxItem, is_dlc, load_feed


def record(product_id, title, product_kind="game", platforms=None):
    return {
        "productId": product_id,
        "title": title,
        "productKind": product_kind,
        "kind": "unannounced",
        "unlock": None,
        "dateOnly": False,
        "standard": None,
        "earlyAccess": [],
        "editions": [],
        "gamePass": [],
        "gamePassAtLaunch": False,
        "platforms": platforms if platforms is not None else ["XboxOne", "XboxSeriesX", "PC"],
        "preorder": False,
        "catalogMissing": False,
        "added": "2026-10-21T05:52:28Z",
    }


def write_feed(tmp_path: Path, data, age: timedelta = timedelta(0)) -> Path:
    path = tmp_path / "wishlist.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    if age:
        old = time.time() - age.total_seconds()
        os.utime(path, (old, old))
    return path


def load(path, **kw):
    kw.setdefault("max_age", timedelta(hours=3))
    kw.setdefault("last_count", None)
    return load_feed(path, **kw)


def test_load_feed_parses_records(tmp_path):
    path = write_feed(
        tmp_path,
        [
            record("9MSR3D5MXXTK", "The Expanse: Osiris Reborn"),
            record("BXYZ", "Some DLC", product_kind="durable", platforms=["XboxSeriesX"]),
        ],
    )
    items = load(path)
    assert items == [
        XboxItem("9MSR3D5MXXTK", "The Expanse: Osiris Reborn", "game", "unannounced",
                 ("XboxOne", "XboxSeriesX", "PC")),
        XboxItem("BXYZ", "Some DLC", "durable", "unannounced", ("XboxSeriesX",)),
    ]


def test_is_dlc():
    def item(kind):
        return XboxItem("X", "T", kind, "released", ())

    assert not is_dlc(item("game"))
    assert not is_dlc(item("bundle"))
    assert not is_dlc(item("demo"))
    assert is_dlc(item("durable"))
    assert is_dlc(item("consumable"))


def test_feed_too_old_raises(tmp_path):
    path = write_feed(tmp_path, [record("A", "A")], age=timedelta(hours=5))
    with pytest.raises(FeedError, match="stale"):
        load(path)


def test_feed_within_max_age_loads(tmp_path):
    path = write_feed(tmp_path, [record("A", "A")], age=timedelta(hours=1))
    assert len(load(path)) == 1


def test_feed_missing_raises(tmp_path):
    with pytest.raises(FeedError):
        load(tmp_path / "nope.json")


def test_feed_empty_raises(tmp_path):
    with pytest.raises(FeedError, match="empty"):
        load(write_feed(tmp_path, []))


def test_feed_shrunk_raises(tmp_path):
    path = write_feed(tmp_path, [record(f"P{i}", f"G{i}") for i in range(10)])
    with pytest.raises(FeedError, match="shrunk"):
        load(path, last_count=51)


def test_feed_small_shrink_allowed(tmp_path):
    path = write_feed(tmp_path, [record(f"P{i}", f"G{i}") for i in range(40)])
    assert len(load(path, last_count=51)) == 40


def test_feed_invalid_json_raises(tmp_path):
    with pytest.raises(FeedError, match="JSON"):
        load(write_feed(tmp_path, "{not json"))


def test_feed_not_a_list_raises(tmp_path):
    with pytest.raises(FeedError):
        load(write_feed(tmp_path, {"items": []}))


def test_feed_record_missing_fields_raises(tmp_path):
    with pytest.raises(FeedError, match="productId"):
        load(write_feed(tmp_path, [{"title": "No id"}]))

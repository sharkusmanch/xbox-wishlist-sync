import json

from wsync.state import State, load_state, save_state


def test_state_roundtrip_and_missing_file_gives_empty_state(tmp_path):
    path = tmp_path / "state" / "state.json"
    empty = load_state(path)
    assert empty == State(mapping={}, d2d={}, ta_lists={}, last_feed_count=None)

    state = State(
        mapping={"P1": {"deku": "uuid-1", "deku_slug": "game-one", "ta": {"game": 1, "dlc": 2}}},
        d2d={"P1": {"verdict": "yes", "checked": "2026-11-01T00:00:00Z"}},
        ta_lists={"fetched": "2026-11-01T00:00:00Z", "yes": ["a"], "no": ["b"]},
        last_feed_count=51,
    )
    save_state(path, state)
    assert load_state(path) == state
    assert json.loads(path.read_text())["last_feed_count"] == 51
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_save_state_is_atomic_and_overwrites(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(mapping={}, d2d={}, ta_lists={}, last_feed_count=1))
    save_state(path, State(mapping={}, d2d={}, ta_lists={}, last_feed_count=2))
    assert load_state(path).last_feed_count == 2
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_ta_game_ids_roundtrip_and_old_files_load(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(ta_game_ids={"elden-ring": 9001}))
    assert load_state(path).ta_game_ids == {"elden-ring": 9001}
    path.write_text(json.dumps({"mapping": {}, "last_feed_count": 3}))
    assert load_state(path).ta_game_ids == {}

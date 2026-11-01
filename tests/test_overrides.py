import json

import pytest

from wsync.overrides import OverridesError, load_overrides


def write(tmp_path, data):
    path = tmp_path / "overrides.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_missing_file_means_no_overrides(tmp_path):
    assert load_overrides(tmp_path / "nope.json") == {}


def test_valid_overrides_load(tmp_path):
    path = write(tmp_path, {
        "9NB0ZZ452WF1": {"deku": "fable", "ta": "skip"},
        "9P8LNZ7X5GVP": {"deku": "skip", "note": "unannounced"},
        "9DLC00000001": {"ta": "elden-ring", "ta_dlc": 1234},
    })
    got = load_overrides(path)
    assert got["9NB0ZZ452WF1"] == {"deku": "fable", "ta": "skip"}
    assert got["9P8LNZ7X5GVP"] == {"deku": "skip"}
    assert got["9DLC00000001"] == {"ta": "elden-ring", "ta_dlc": 1234}


def test_empty_object_is_fine(tmp_path):
    assert load_overrides(write(tmp_path, "{}")) == {}


def test_invalid_json_fails(tmp_path):
    with pytest.raises(OverridesError, match="JSON"):
        load_overrides(write(tmp_path, "{not json"))


@pytest.mark.parametrize("data", [
    [],
    {"P1": "skip"},
    {"P1": {"deku": ""}},
    {"P1": {"deku": 5}},
    {"P1": {"steam": "x"}},
    {"P1": {"ta_dlc": "12"}},
    {"P1": {"ta_dlc": True}},
    {"P1": {"ta_dlc": 0}},
    {"P1": {"ta": "skip", "ta_dlc": 3}},
    {"": {"deku": "x"}},
])
def test_bad_shapes_fail(tmp_path, data):
    with pytest.raises(OverridesError):
        load_overrides(write(tmp_path, data))

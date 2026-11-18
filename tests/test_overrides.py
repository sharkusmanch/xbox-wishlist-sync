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


def test_load_overrides_ignores_collection_key(tmp_path):
    path = write(tmp_path, {
        "9NB0ZZ452WF1": {"deku": "fable"},
        "collection": {"123": {"deku": "some-game"}},
    })
    assert load_overrides(path) == {"9NB0ZZ452WF1": {"deku": "fable"}}


def test_collection_overrides_load(tmp_path):
    from wsync.overrides import load_collection_overrides

    path = write(tmp_path, {
        "P1": {"deku": "x"},
        "collection": {"123": {"deku": "some-game"}, "456": {"deku": "skip", "note": "why"}},
    })
    assert load_collection_overrides(path) == {123: {"deku": "some-game"}, 456: {"deku": "skip"}}


def test_collection_overrides_missing_file_or_key(tmp_path):
    from wsync.overrides import load_collection_overrides

    assert load_collection_overrides(tmp_path / "nope.json") == {}
    assert load_collection_overrides(write(tmp_path, {"P1": {"deku": "x"}})) == {}


@pytest.mark.parametrize("coll", [
    [],
    "x",
    {"abc": {"deku": "x"}},
    {"0": {"deku": "x"}},
    {"-4": {"deku": "x"}},
    {"1": "skip"},
    {"1": {}},
    {"1": {"deku": ""}},
    {"1": {"deku": 3}},
    {"1": {"ta": "x"}},
    {"1": {"note": 4, "deku": "x"}},
])
def test_collection_overrides_bad_shapes_fail(tmp_path, coll):
    from wsync.overrides import load_collection_overrides

    with pytest.raises(OverridesError):
        load_collection_overrides(write(tmp_path, {"collection": coll}))


def test_collection_overrides_invalid_json_fails(tmp_path):
    from wsync.overrides import load_collection_overrides

    with pytest.raises(OverridesError, match="JSON"):
        load_collection_overrides(write(tmp_path, "{nope"))


def test_collection_override_keys_normalising_to_one_id_fail(tmp_path):
    from wsync.overrides import load_collection_overrides

    with pytest.raises(OverridesError, match="more than once"):
        load_collection_overrides(write(tmp_path, {"collection": {"1": {"deku": "a"}, " 01": {"deku": "b"}}}))


def test_load_overrides_ignores_the_library_section(tmp_path):
    path = write(tmp_path, {"9ZZTEST00009": {"deku": "example-quest"},
                            "library": {"9ZZTEST00001": {"ta": "Example-Quest"}}})
    assert load_overrides(path) == {"9ZZTEST00009": {"deku": "example-quest"}}


def test_load_library_overrides_validates_the_section(tmp_path):
    from wsync.overrides import load_library_overrides

    path = write(tmp_path, {
        "9ZZTEST00009": {"deku": "example-quest"},
        "collection": {"123": {"deku": "example-quest"}},
        "library": {"9ZZTEST00001": {"ta": "Example-Quest", "note": "TA names it differently"},
                    " 9ZZTEST00002 ": {"ta": "skip"}},
    })
    assert load_library_overrides(path) == {"9ZZTEST00001": {"ta": "Example-Quest"}, "9ZZTEST00002": {"ta": "skip"}}
    assert load_library_overrides(tmp_path / "nope.json") == {}
    assert load_library_overrides(write(tmp_path, {"9ZZTEST00009": {"deku": "x"}})) == {}
    for bad in ([], "x", {"": {"ta": "x"}}, {" ": {"ta": "x"}}, {"P1": "skip"}, {"P1": {}}, {"P1": {"note": "x"}},
                {"P1": {"ta": ""}}, {"P1": {"ta": 3}}, {"P1": {"ta": "x", "deku": "y"}}, {"P1": {"ta": "x", "note": 4}},
                {"P1": {"ta": "a"}, " P1": {"ta": "b"}}):
        with pytest.raises(OverridesError):
            load_library_overrides(write(tmp_path, {"library": bad}))
    with pytest.raises(OverridesError, match="JSON"):
        load_library_overrides(write(tmp_path, "{nope"))

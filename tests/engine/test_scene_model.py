import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rpghome.models.scene import Bed, ClipRef, Scene, SceneError, scene_from_data, scene_from_json

EXAMPLES = Path(__file__).parents[2] / "examples"


def test_json_round_trip():
    scene = Scene(
        name="cave",
        bed=Bed(
            clips=(ClipRef(path="a/x.wav", gain_db=-3.5), ClipRef(path="b.wav")),
            hold_s=(100, 120),
            crossfade_s=20,
            loop_crossfade_s=2,
            gain_db=-2,
        ),
        gain_db=1,
    )
    data = json.loads(json.dumps(scene.model_dump(mode="json")))
    assert scene_from_data(data) == scene
    assert scene_from_json(json.dumps(data)) == scene
    assert data == {
        "name": "cave",
        "bed": {
            "clips": [{"path": "a/x.wav", "gain_db": -3.5}, {"path": "b.wav", "gain_db": 0.0}],
            "hold_s": [100.0, 120.0],
            "crossfade_s": 20.0,
            "loop_crossfade_s": 2.0,
            "gain_db": -2.0,
        },
        "gain_db": 1.0,
    }


def test_numbers_are_floats_and_ranges_tuples():
    bed = Bed(clips=[ClipRef(path="a.wav")], hold_s=[100, 120], crossfade_s=20)
    assert bed.clips == (ClipRef(path="a.wav"),)
    assert bed.hold_s == (100.0, 120.0) and type(bed.hold_s[0]) is float
    assert type(bed.crossfade_s) is float


def test_the_example_scene_loads():
    scene = scene_from_json((EXAMPLES / "cave.json").read_bytes())
    assert scene.name == "cave" and len(scene.bed.clips) == 3
    assert scene.bed.hold_s == (150.0, 210.0)


def test_defaults():
    scene = scene_from_data({"name": "s", "bed": {"clips": [{"path": "c.wav"}]}})
    bed = scene.bed
    assert bed.hold_s == (150.0, 210.0)
    assert (bed.crossfade_s, bed.loop_crossfade_s, bed.gain_db) == (35.0, 4.0, 0.0)
    assert bed.clips[0].gain_db == 0.0 and scene.gain_db == 0.0
    assert scene_from_data({"name": "quiet"}).bed is None


def test_models_are_frozen():
    clip = ClipRef(path="a.wav")
    with pytest.raises(ValidationError, match="frozen"):
        clip.gain_db = 3.0


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"name": ""}, "name: Value error, scene name must be a non-empty string"),
        ({"bed": None}, "name: Field required"),
        ({"name": 3}, "name: Input should be a valid string"),
        ({"name": "s", "colour": 1}, "colour: Extra inputs are not permitted"),
        ({"name": "s", "gain_db": "loud"}, "gain_db: Input should be a valid number"),
        ({"name": "s", "gain_db": True}, "gain_db: Input should be a valid number"),
        ({"name": "s", "gain_db": 99}, "gain_db: Input should be less than or equal to 24"),
        ({"name": "s", "bed": {"clips": []}}, "bed.clips: Tuple should have at least 1 item"),
        ({"name": "s", "bed": {}}, "bed.clips: Field required"),
        ({"name": "s", "bed": {"clips": [{"path": "/abs.wav"}]}}, "bed.clips.0.path: .*relative"),
        ({"name": "s", "bed": {"clips": [{"path": "../up.wav"}]}}, "bed.clips.0.path: .*relative"),
        ({"name": "s", "bed": {"clips": [{"path": "a\\b.wav"}]}}, "bed.clips.0.path: .*relative"),
        ({"name": "s", "bed": {"clips": [{"path": " "}]}}, "bed.clips.0.path: .*non-empty"),
        (
            {"name": "s", "bed": {"clips": [{"path": "a.wav"}, {"path": "b.wav", "gain": 1}]}},
            "bed.clips.1.gain: Extra inputs are not permitted",
        ),
        (
            {"name": "s", "bed": {"clips": [{"path": "a.wav"}], "hold_s": [10, 5]}},
            r"bed.hold_s: Value error, hold_s must satisfy 0 < min <= max, got \[10.0, 5.0\]",
        ),
        ({"name": "s", "bed": {"clips": [{"path": "a.wav"}], "hold_s": [0, 5]}}, "bed.hold_s"),
        ({"name": "s", "bed": {"clips": [{"path": "a.wav"}], "hold_s": [10]}}, "bed.hold_s.1"),
        ({"name": "s", "bed": {"clips": [{"path": "a.wav"}], "hold_s": 10}}, "bed.hold_s"),
        (
            {
                "name": "s",
                "bed": {"clips": [{"path": "a.wav"}], "hold_s": [10, 20], "crossfade_s": 11},
            },
            r"bed: Value error, crossfade_s must be within 0..hold_s min \(10.0\), got 11.0",
        ),
        (
            {"name": "s", "bed": {"clips": [{"path": "a.wav"}], "loop_crossfade_s": -1}},
            "bed.loop_crossfade_s: Input should be greater than or equal to 0",
        ),
        (
            {"name": "s", "bed": {"clips": [{"path": "a.wav"}], "gain_db": float("nan")}},
            "bed.gain_db: Input should be a finite number",
        ),
        ("cave", "Input should be a valid dictionary"),
    ],
)
def test_invalid_scenes_are_rejected(data, message):
    with pytest.raises(SceneError, match=message):
        scene_from_data(data)


def test_errors_list_every_field():
    data = {"name": "", "gain_db": 99, "bed": {"clips": [{"path": "/a.wav"}]}}
    with pytest.raises(SceneError) as info:
        scene_from_data(data)
    message = str(info.value)
    for path in ("name:", "bed.clips.0.path:", "gain_db:"):
        assert path in message


def test_invalid_json_is_a_scene_error():
    with pytest.raises(SceneError, match="Invalid JSON"):
        scene_from_json("{nope")

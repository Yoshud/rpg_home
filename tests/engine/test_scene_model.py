import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rpghome.models.scene import (
    Bed,
    ClipRef,
    Reverb,
    Scene,
    SceneError,
    Spots,
    scene_from_data,
    scene_from_json,
)

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
        "spots": [],
        "reverb": None,
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
    assert [s.name for s in scene.spots] == ["drops", "stones"]
    assert scene.spots[1].distance_m == 4 and scene.reverb == Reverb()
    assert all(s.source_weights == {"moss": 3, "epidemic": 1} for s in scene.spots)


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


def spots_scene():
    drops = Spots(
        name="drops",
        clips=(
            ClipRef(path="spots_moss/drop.wav", gain_db=2.5, source="moss", perspective="close"),
            ClipRef(
                path="spots_epidemic/drop_a.wav",
                gain_db=-1,
                source="epidemic",
                perspective=3,
                dry=True,
            ),
        ),
        interval_s=(1.5, 12),
        mean_interval_s=4,
        gain_db=(-10, -2),
        pitch_semitones=2,
        source_weights={"moss": 3, "epidemic": 1},
        slice=True,
    )
    stones = Spots(name="stones", clips=(ClipRef(path="r.wav"),), interval_s=(15, 80), distance_m=6)
    return Scene(name="cave", spots=(drops, stones), reverb=Reverb(decay_s=2, wet_db=-12, seed=5))


def test_spots_and_reverb_round_trip():
    scene = spots_scene()
    data = json.loads(json.dumps(scene.model_dump(mode="json")))
    assert scene_from_data(data) == scene
    assert scene_from_json(json.dumps(data)) == scene
    assert data["spots"][0]["clips"][1] == {
        "path": "spots_epidemic/drop_a.wav",
        "gain_db": -1.0,
        "source": "epidemic",
        "perspective": 3.0,
        "dry": True,
    }
    assert data["spots"][1] == {
        "name": "stones",
        "clips": [{"path": "r.wav", "gain_db": 0.0}],
        "interval_s": [15.0, 80.0],
        "mean_interval_s": 47.5,
        "gain_db": [0.0, 0.0],
        "pitch_semitones": 0.0,
        "pan": 0.8,
        "distance_m": 6.0,
        "source_weights": {},
        "slice": False,
    }
    assert data["reverb"] == {"decay_s": 2.0, "wet_db": -12.0, "seed": 5}


def test_spots_defaults_and_old_scenes_load():
    scene = scene_from_data({"name": "s", "bed": {"clips": [{"path": "c.wav"}]}})
    assert scene.spots == () and scene.reverb is None
    assert scene.bed.clips[0].model_dump(mode="json") == {"path": "c.wav", "gain_db": 0.0}
    clip = scene.bed.clips[0]
    assert (clip.source, clip.perspective, clip.dry) == (None, None, False)
    sp = Spots.model_validate({"name": "d", "clips": [{"path": "a.wav"}], "interval_s": [2, 6]})
    assert sp.mean_interval_s == 4.0 and sp.gain_db == (0.0, 0.0) and sp.pan == 0.8
    assert (sp.pitch_semitones, sp.distance_m, sp.source_weights, sp.slice) == (0, None, {}, False)
    assert Reverb.model_validate({}) == Reverb(decay_s=2.2, wet_db=-10.0, seed=3)
    assert scene_from_data({"name": "s", "reverb": None}).reverb is None


def test_spots_are_hashable():
    assert hash(spots_scene()) == hash(spots_scene())


def test_clip_distance_from_perspective():
    distances = [ClipRef(path="a", perspective=p).distance_m for p in ("close", "medium", "far")]
    assert distances == [1.0, 5.0, 20.0]
    assert ClipRef(path="a").distance_m == 1.0  # unknown = close
    assert ClipRef(path="a", perspective=7).distance_m == 7.0


def test_weights_default_to_one_for_unlisted_sources():
    sp = Spots(
        name="d",
        clips=(ClipRef(path="a", source="moss"), ClipRef(path="b")),
        interval_s=(1, 2),
        source_weights={"moss": 3},
    )
    assert [sp.weight(c) for c in sp.clips] == [3.0, 1.0]


SPOT = {"name": "d", "clips": [{"path": "a.wav"}], "interval_s": [1, 2]}


@pytest.mark.parametrize(
    ("spot", "message"),
    [
        ({"name": " "}, "spots.0.name: .*non-empty"),
        ({"interval_s": [0, 2]}, r"spots.0.interval_s: .*0 < min <= max"),
        ({"interval_s": [3, 2]}, r"spots.0.interval_s: .*0 < min <= max"),
        ({"interval_s": [1]}, "spots.0.interval_s.1: Field required"),
        ({"interval_s": None}, "spots.0.interval_s: Input should be a valid tuple"),
        ({"mean_interval_s": -1}, "spots.0.mean_interval_s: Input should be greater than 0"),
        ({"gain_db": [0, -5]}, "spots.0.gain_db: .*min <= max"),
        ({"gain_db": [-100, 0]}, "spots.0.gain_db.0: Input should be greater than or equal"),
        ({"pitch_semitones": 30}, "spots.0.pitch_semitones: Input should be less than"),
        ({"pan": 1.5}, "spots.0.pan: Input should be less than"),
        ({"pan": "0.5"}, "spots.0.pan: Input should be a valid number"),
        ({"distance_m": 0}, "spots.0.distance_m: Input should be greater than 0"),
        (
            {"source_weights": {"freesound": 1}},
            r"spots.0.source_weights.freesound.\[key\]: Input should be",
        ),
        ({"source_weights": {"moss": -1}}, "spots.0.source_weights.moss: .*greater than"),
        ({"source_weights": {"other": 0}}, "spots.0: Value error, every clip has source weight 0"),
        ({"slice": "yes"}, "spots.0.slice: Input should be a valid boolean"),
        ({"clips": []}, "spots.0.clips: Tuple should have at least 1 item"),
        ({"clips": [{"path": "a.wav", "source": "web"}]}, "spots.0.clips.0.source: Input should"),
        (
            {"clips": [{"path": "a.wav", "perspective": "near"}]},
            "spots.0.clips.0.perspective: Value error, perspective must be close, medium, far "
            "or metres within 0..1000, got 'near'",
        ),
        ({"clips": [{"path": "a.wav", "perspective": -2}]}, "clips.0.perspective: .*got -2;"),
        ({"clips": [{"path": "a.wav", "perspective": 0}]}, "clips.0.perspective: .*got 0;"),
        ({"clips": [{"path": "a.wav", "perspective": True}]}, "spots.0.clips.0.perspective"),
        ({"clips": [{"path": "a.wav", "dry": 1}]}, "spots.0.clips.0.dry: Input should be a valid"),
        ({"volume": 1}, "spots.0.volume: Extra inputs are not permitted"),
    ],
)
def test_invalid_spots_are_rejected(spot, message):
    with pytest.raises(SceneError, match=message):
        scene_from_data({"name": "s", "spots": [{**SPOT, **spot}]})


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"name": "s", "spots": [SPOT, SPOT]}, "spots: Value error, spots names must be unique"),
        ({"name": "s", "spots": {}}, "spots: Input should be a valid tuple"),
        ({"name": "s", "reverb": {"decay_s": 0}}, "reverb.decay_s: Input should be greater"),
        ({"name": "s", "reverb": {"wet_db": 50}}, "reverb.wet_db: Input should be less"),
        ({"name": "s", "reverb": {"seed": 1.5}}, "reverb.seed: Input should be a valid integer"),
        ({"name": "s", "reverb": {"seed": True}}, "reverb.seed: Input should be a valid integer"),
        ({"name": "s", "reverb": {"seed": -1}}, "reverb.seed: Input should be greater"),
        ({"name": "s", "reverb": {"size": 1}}, "reverb.size: Extra inputs are not permitted"),
    ],
)
def test_invalid_scene_spots_and_reverb(data, message):
    with pytest.raises(SceneError, match=message):
        scene_from_data(data)

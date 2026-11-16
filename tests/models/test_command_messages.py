"""The command messages on rpghome/cmd/<command>: what parses and what is refused, by name."""

import pytest

from rpghome.models.commands import (
    CommandError,
    FxCommand,
    OverlayCommand,
    SceneCommand,
    SetCommand,
    StopCommand,
    error_event,
    ok_event,
    parse_command,
)

SCENE = {"name": "cave", "bed": {"clips": [{"path": "cave/a.wav", "gain_db": -20.0}]}}


def parsed(command, payload):
    return parse_command(command, payload)


def test_scene_carries_the_scene_data_and_an_optional_transition():
    cmd = parsed("scene", {"scene": SCENE, "transition_s": 12.5, "id": "m1"})
    assert isinstance(cmd, SceneCommand) and cmd.command == "scene"
    assert cmd.scene.name == "cave" and cmd.scene.bed.clips[0].path == "cave/a.wav"
    assert cmd.transition_s == 12.5 and cmd.id == "m1"
    cmd = parsed("scene", {"scene": SCENE})
    assert cmd.transition_s is None and cmd.id is None  # the engine's own default then


def test_stop_and_set_are_small():
    assert parsed("stop", {"fade_s": 2, "id": "m2"}) == StopCommand(fade_s=2.0, id="m2")
    assert parsed("stop", {}) == StopCommand()
    cmd = parsed("set", {"name": "bed_gain_db", "value": -6.0})
    assert isinstance(cmd, SetCommand) and cmd.name == "bed_gain_db" and cmd.value == -6.0
    cmd = parsed("set", {"name": "density", "value": 2})  # 2.0 is fine too, not only dB
    assert cmd.name == "density" and cmd.value == 2.0


def test_overlay_and_fx_are_part_of_the_schema():
    assert parsed("overlay", {"name": "fire", "on": False, "fade_s": 20}) == OverlayCommand(
        name="fire", on=False, fade_s=20.0
    )
    assert parsed("fx", {"name": "fireball", "distance_m": 2500}) == FxCommand(
        name="fireball", distance_m=2500.0
    )


@pytest.mark.parametrize(
    ("command", "payload", "message"),
    [
        ("dance", {}, "unknown command 'dance'"),
        ("scene", {}, "scene command: scene: Field required"),
        (
            "scene",
            {"scene": {"bed": {"clips": [{"path": "a.wav"}]}}},
            "scene command: scene.name: Field required",
        ),
        (
            "scene",
            {"scene": {"name": "s", "bed": {"clips": [{"path": "a.wav"}], "hold_s": [10, 5]}}},
            "scene command: scene.bed.hold_s: Value error, hold_s must satisfy 0 < min <= max",
        ),
        ("scene", {"scene": SCENE, "fade_s": 3}, "fade_s: Extra inputs are not permitted"),
        (
            "scene",
            {"scene": SCENE, "transition_s": "slow"},
            "transition_s: Input should be a valid number",
        ),
        ("scene", {"scene": SCENE, "transition_s": -1}, "transition_s must be within 0..3600"),
        ("scene", {"scene": SCENE, "id": 7}, "id: Input should be a valid string"),
        ("scene", {"scene": SCENE, "id": ""}, "id must be a non-empty string"),
        ("scene", "not an object", "must be a JSON object, got str"),
        ("scene", ["scene"], "must be a JSON object, got list"),
        ("stop", {"fade_s": "x"}, "fade_s: Input should be a valid number"),
        ("stop", {"name": "x"}, "stop command: name: Extra inputs are not permitted"),
        (
            "set",
            {"name": "volume", "value": -6},
            "name: Input should be 'master_gain_db', 'bed_gain_db' or 'density'",
        ),
        ("set", {"name": "master_gain_db"}, "value: Field required"),
        (
            "set",
            {"name": "master_gain_db", "value": True},
            "value: Input should be a valid number",
        ),
        (
            "set",
            {"name": "master_gain_db", "value": 900},
            "master_gain_db must be within -90..24 dB",
        ),
        ("set", {"name": "density", "value": 0}, "density must be within 0.1..10"),
        ("set", {"name": "density", "value": 900}, "density must be within 0.1..10"),
        (
            "set",
            {"name": "", "value": 0},
            "name: Input should be 'master_gain_db', 'bed_gain_db' or 'density'",
        ),
        ("overlay", {}, "overlay command: name: Field required"),
        ("overlay", {"name": "fire", "on": "yes"}, "on: Input should be a valid boolean"),
        ("fx", {"name": "x", "distance_m": -5}, "distance_m must be >= 0"),
    ],
)
def test_a_bad_command_is_refused_by_naming_the_field(command, payload, message):
    with pytest.raises(CommandError) as caught:
        parse_command(command, payload)
    assert message in str(caught.value), str(caught.value)


def test_events_echo_the_id_and_an_error_names_the_command():
    assert ok_event("scene", "m1") == {"type": "ok", "command": "scene", "id": "m1"}
    assert ok_event("stop") == {"type": "ok", "command": "stop"}
    assert error_event("overlay", "overlay is not supported by this engine yet", "m9") == {
        "type": "error",
        "command": "overlay",
        "error": "overlay is not supported by this engine yet",
        "id": "m9",
    }

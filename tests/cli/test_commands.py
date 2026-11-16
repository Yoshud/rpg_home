"""The CLI commands over a bus: an InMemoryBus and a stand-in engine answer them."""

import json

import pytest
import typer
from typer.testing import CliRunner

from rpghome.bus import topics
from rpghome.bus.memory import InMemoryBus
from rpghome.cli import main as cli
from rpghome.models.commands import error_event, ok_event

runner = CliRunner()


class FakeEngine:
    """Answers every command with ok, or with the error registered for it."""

    def __init__(self, bus):
        self.bus = bus
        self.sent: list[tuple[str, dict]] = []
        self.refused: dict[str, str] = {}
        bus.subscribe(topics.CMD_FILTER, self.on_command)

    def on_command(self, topic, payload):
        command = topics.command_of(topic)
        self.sent.append((command, payload))
        if command in self.refused:
            self.bus.publish(
                topics.EVENTS, error_event(command, self.refused[command], payload.get("id"))
            )
        else:
            self.bus.publish(topics.EVENTS, ok_event(command, payload.get("id")))

    def last(self):
        return self.sent[-1]


@pytest.fixture
def engine(monkeypatch):
    """The CLI talks to this bus instead of the broker, with an engine that announced itself."""
    bus = InMemoryBus()
    fake = FakeEngine(bus)
    bus.publish(topics.ONLINE, topics.ONLINE_ON, retain=True)  # what the CLI reads before sending

    def _with_bus(action):
        raise typer.Exit(action(bus))

    monkeypatch.setattr(cli, "_with_bus", _with_bus)
    return fake


def scene_file(tmp_path, name="cave"):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"name": name, "bed": {"clips": [{"path": "cave/a.wav"}]}}))
    return path


def test_scene_sends_the_scene_data_and_prints_the_ok_event(engine, tmp_path):
    result = runner.invoke(cli.app, ["scene", str(scene_file(tmp_path))])
    assert result.exit_code == 0, result.output
    command, payload = engine.last()
    assert command == "scene"
    assert payload["scene"]["name"] == "cave"
    assert payload["scene"]["bed"]["clips"][0]["path"] == "cave/a.wav"
    assert "transition_s" not in payload and "id" in payload
    assert json.loads(result.output.strip()) == {
        "type": "ok",
        "command": "scene",
        "id": payload["id"],
    }


def test_scene_passes_transition_s(engine, tmp_path):
    runner.invoke(cli.app, ["scene", str(scene_file(tmp_path)), "--transition", "12.5"])
    assert engine.last()[1]["transition_s"] == 12.5


def test_stop_and_set_send_their_payloads(engine):
    runner.invoke(cli.app, ["stop", "--fade", "1.5"])
    command, payload = engine.last()
    assert command == "stop" and payload["fade_s"] == 1.5 and payload["id"]
    runner.invoke(cli.app, ["stop"])
    assert "fade_s" not in engine.last()[1]  # the engine's own fade then
    runner.invoke(cli.app, ["set", "bed_gain_db", "-6"])
    command, payload = engine.last()
    assert command == "set" and payload["name"] == "bed_gain_db" and payload["value"] == -6.0


def test_set_needs_exactly_a_name_and_a_value(engine):
    result = runner.invoke(cli.app, ["set", "master_gain_db"])
    assert result.exit_code == 1
    assert "usage: rpghome set NAME VALUE" in result.output
    assert engine.sent == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["set", "volume", "-6"],
            "Input should be 'master_gain_db', 'bed_gain_db' or 'density'",
        ),
        (["set", "master_gain_db", "loud"], "value must be a number"),
        (["set", "master_gain_db", "900"], "must be within -90..24 dB"),
        (["set", "density", "0"], "density must be within 0.1..10"),
    ],
)
def test_a_bad_set_is_refused_before_it_is_sent(engine, args, message, tmp_path):
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert message in result.output
    assert engine.sent == []  # nothing went to the bus


def test_a_bad_scene_file_is_reported_without_a_bus_round_trip(engine, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text('{"bed": {"clips": [{"path": "a.wav"}]}}')
    result = runner.invoke(cli.app, ["scene", str(broken)])
    assert result.exit_code == 1 and "invalid scene" in result.output
    missing = runner.invoke(cli.app, ["scene", str(tmp_path / "gone.json")])
    assert missing.exit_code == 1 and "cannot read" in missing.output
    assert engine.sent == []  # nothing went to the bus


def test_an_error_event_from_the_engine_is_printed_and_exits_1(engine, tmp_path):
    engine.refused["scene"] = "clip not found: cave/a.wav"
    result = runner.invoke(cli.app, ["scene", str(scene_file(tmp_path))])
    assert result.exit_code == 1
    event = json.loads(result.output.strip())
    assert event["type"] == "error" and "clip not found" in event["error"]


def test_no_answer_from_the_engine_times_out(engine, monkeypatch):
    monkeypatch.setattr(cli, "REPLY_TIMEOUT_S", 0.2)
    engine.bus._subs.clear()  # nobody answers
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "did not answer within 0.2 s" in result.output


def test_a_command_is_not_sent_when_the_engine_is_offline(engine):
    engine.bus.publish(topics.ONLINE, topics.ONLINE_OFF, retain=True)  # the engine's last will
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "engine is offline" in result.output
    assert engine.sent == []  # nothing went to the broker


def test_a_command_is_not_sent_when_no_engine_has_announced_itself(engine):
    engine.bus.retained.clear()  # no online flag on the broker: nobody is there to answer
    result = runner.invoke(cli.app, ["set", "master_gain_db", "-6"])
    assert result.exit_code == 1
    assert "engine is offline" in result.output
    assert engine.sent == []


def test_scene_is_not_sent_to_an_offline_engine(engine, tmp_path):
    engine.bus.publish(topics.ONLINE, topics.ONLINE_OFF, retain=True)
    result = runner.invoke(cli.app, ["scene", str(scene_file(tmp_path))])
    assert result.exit_code == 1 and "engine is offline" in result.output
    assert engine.sent == []


def test_scene_waits_longer_than_stop_and_set(engine, monkeypatch, tmp_path):
    """`scene` gives the engine time to read its clips; `stop` and `set` do not need it."""
    waits: dict[str, float] = {}
    real = cli.send_command

    def spy(bus, command, payload, timeout_s=None):
        waits[command] = timeout_s
        return real(bus, command, payload, timeout_s)

    monkeypatch.setattr(cli, "send_command", spy)
    runner.invoke(cli.app, ["scene", str(scene_file(tmp_path))])
    runner.invoke(cli.app, ["stop"])
    runner.invoke(cli.app, ["set", "master_gain_db", "-6"])
    assert waits == {
        "scene": cli.SCENE_TIMEOUT_S,
        "stop": cli.REPLY_TIMEOUT_S,
        "set": cli.REPLY_TIMEOUT_S,
    }
    assert cli.SCENE_TIMEOUT_S > cli.REPLY_TIMEOUT_S


@pytest.mark.parametrize(
    "args",
    [
        ["stop", "--timeout", "0.2"],
        ["set", "--timeout", "0.2", "master_gain_db", "-6"],  # `set` takes options as well
        ["set", "master_gain_db", "-6", "--timeout", "0.2"],
    ],
)
def test_timeout_is_how_long_the_answer_is_watched_for(engine, args):
    engine.bus._subs.clear()  # nobody answers, and the online flag is still retained
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1, result.output
    assert "did not answer within 0.2 s" in result.output


def test_scene_takes_a_timeout_too(engine, tmp_path):
    engine.bus._subs.clear()
    result = runner.invoke(cli.app, ["scene", str(scene_file(tmp_path)), "--timeout", "0.2"])
    assert result.exit_code == 1 and "did not answer within 0.2 s" in result.output


def test_a_timeout_that_is_not_positive_fails_before_anything_is_sent(engine):
    result = runner.invoke(cli.app, ["stop", "--timeout", "0"])
    assert result.exit_code == 1
    assert "--timeout must be > 0" in result.output
    assert engine.sent == []


def test_status_prints_the_retained_state_and_exits_0(engine):
    engine.bus.publish(topics.STATE, {"scene": "cave", "clip": "cave/a.wav"}, retain=True)
    engine.bus.publish(topics.ONLINE, topics.ONLINE_ON, retain=True)
    assert cli.show_status(engine.bus) == 0


def test_status_exits_1_when_the_engine_is_offline(engine):
    engine.bus.publish(topics.STATE, {"scene": "cave"}, retain=True)
    engine.bus.publish(topics.ONLINE, topics.ONLINE_OFF, retain=True)
    assert cli.show_status(engine.bus) == 1


def test_status_exits_1_when_no_state_arrives(engine):
    engine.bus.publish(topics.ONLINE, topics.ONLINE_ON, retain=True)
    assert cli.show_status(engine.bus, timeout_s=0.2) == 1


def test_send_command_returns_none_when_nobody_answers():
    bus = InMemoryBus()
    assert cli.send_command(bus, "stop", {}, timeout_s=0.2) is None

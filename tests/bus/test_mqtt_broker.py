"""MqttBus against a real broker (mosquitto from docker compose).

Skipped when 127.0.0.1:1883 does not accept a connection; run them with:

    docker compose up -d mosquitto && uv run pytest -m broker

`RPGHOME_REQUIRE_BROKER=1` turns that skip into a failure, so a run meant to use the broker
cannot quietly pass without it.

They use the real topics (the engine process publishes there), except the retention test,
which uses `rpghome/test/state` so it leaves no fake state behind on the engine's topic.
"""

import json
import logging
import os
import queue
import socket
import subprocess
import sys
import time
import uuid

import numpy as np
import pytest
import soundfile as sf
from typer.testing import CliRunner

from rpghome.bus import topics
from rpghome.bus.base import Bus
from rpghome.bus.mqtt import MqttBus
from rpghome.cli import main as cli
from rpghome_engine.core import Engine
from rpghome_engine.devices import NullOutput
from rpghome_engine.link import EngineLink

pytestmark = pytest.mark.broker

HOST, PORT = "127.0.0.1", 1883
RATE = 48000


def broker_reachable() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


def broker_required() -> bool:
    """RPGHOME_REQUIRE_BROKER=1: a broker is expected, so a missing one is a failure."""
    return os.environ.get("RPGHOME_REQUIRE_BROKER", "").strip() not in ("", "0")


@pytest.fixture
def broker():
    if broker_reachable():
        return
    where = f"no broker on {HOST}:{PORT} - docker compose up -d mosquitto"
    if broker_required():
        pytest.fail(f"RPGHOME_REQUIRE_BROKER=1, but {where}")
    pytest.skip(where)


def new_bus(**kw) -> MqttBus:
    bus = MqttBus(HOST, PORT, client_id=f"rpghome-test-{uuid.uuid4().hex[:8]}", **kw)
    assert isinstance(bus, Bus)
    assert bus.wait_connected(10), f"cannot connect to the broker at {HOST}:{PORT}"
    return bus


def wait_until(what, predicate, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def engine_process(env, **extra_env):
    """The real process, no sound card, this broker, this (temporary) state folder."""
    return subprocess.Popen(
        [sys.executable, "-m", "rpghome_engine"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "RPGHOME_AUDIO_DEVICE": "null", **extra_env},
    )


def test_an_mqtt_bus_is_a_bus(broker):
    bus = new_bus()
    try:
        assert bus.connected
    finally:
        bus.close()


def test_a_late_subscriber_gets_the_retained_message_at_once(broker):
    topic = "rpghome/test/state"  # not the engine's topic: nothing to clean up afterwards
    marker = uuid.uuid4().hex
    pub = new_bus()
    try:
        pub.publish(topic, {"marker": marker, "scene": "retained"}, retain=True)
        time.sleep(0.3)  # the broker has it now
    finally:
        pub.close()

    late = new_bus()  # a subscriber that starts after the message was published
    messages = queue.Queue()
    try:
        late.subscribe(topic, lambda _t, payload: messages.put(payload))
        got = messages.get(timeout=10)
        assert got == {"marker": marker, "scene": "retained"}
    finally:
        late.close()


def test_a_message_that_is_not_a_json_object_is_dropped(broker, caplog):
    topic = f"rpghome/test/cmd/{uuid.uuid4().hex}"
    pub, sub = new_bus(), new_bus()
    seen = queue.Queue()
    try:
        with caplog.at_level(logging.WARNING, logger="rpghome.bus"):
            sub.subscribe(topic, lambda _t, payload: seen.put(payload))
            time.sleep(0.3)  # the subscription is in place
            # past MqttBus.publish, exactly as a sender that is not us would do it
            pub._client.publish(topic, b"{not json", qos=1)
            pub._client.publish(topic, b'["nope"]', qos=1)  # valid JSON, but not an object
            pub._client.publish(topic, b'{"ok": 1}', qos=1)
            assert seen.get(timeout=10) == {"ok": 1}  # the good message still arrives
        assert "dropped a message" in caplog.text
        assert "Expecting property name" in caplog.text  # what json says about `{not json`
        assert "not a JSON object" in caplog.text
    finally:
        pub.close()
        sub.close()


def test_a_subscriber_with_on_invalid_gets_the_bytes_it_cannot_read(broker):
    topic = f"rpghome/test/cmd/{uuid.uuid4().hex}"
    pub, sub = new_bus(), new_bus()
    seen, bad = queue.Queue(), queue.Queue()
    try:
        sub.subscribe(topic, lambda _t, payload: seen.put(payload), lambda *b: bad.put(b))
        time.sleep(0.3)  # the subscription is in place
        pub._client.publish(topic, b"{not json", qos=1)  # past MqttBus, as another client does it
        pub._client.publish(topic, b'["nope"]', qos=1)
        pub._client.publish(topic, b'{"ok": 1}', qos=1)
        assert seen.get(timeout=10) == {"ok": 1}  # the good message still arrives
        where, raw, reason = bad.get(timeout=10)
        assert where == topic and raw == b"{not json" and "Expecting property name" in reason
        assert bad.get(timeout=10)[2] == "not a JSON object, got a list"
    finally:
        pub.close()
        sub.close()


def test_a_command_over_the_broker_reaches_a_real_engine(broker, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    tone = np.arange(round(3 * RATE)) / RATE
    samples = (0.25 * np.sin(2 * np.pi * 440 * tone)).astype(np.float32)
    sf.write(str(data / "t.wav"), samples, RATE, "FLOAT")

    engine = Engine(data, seed=1)
    output = NullOutput(engine)
    output.start()
    bus = new_bus(will=(topics.ONLINE, topics.ONLINE_OFF), keepalive_s=5)
    link = EngineLink(engine, bus)
    bus.add_connect_hook(link.announce)
    link.start()
    client = new_bus()
    events: list[dict] = []
    try:
        bed = {
            "clips": [{"path": "t.wav"}],
            "hold_s": [2, 2],
            "crossfade_s": 1,
            "loop_crossfade_s": 0.5,
        }
        scene = {"name": "broker-test", "bed": bed}
        event = cli.send_command(client, "scene", {"scene": scene}, timeout_s=10)
        assert event is not None and event["type"] == "ok", event
        wait_until("the scene to play", lambda: engine.status()["scene"] == "broker-test")

        # the state the command left behind, read as any client reads it: no forced publish
        state = cli.read_retained(client, [topics.STATE, topics.ONLINE], timeout_s=10)
        assert state[topics.STATE]["scene"] == "broker-test"
        assert state[topics.STATE]["clip"] == "t.wav"
        assert state[topics.ONLINE] == topics.ONLINE_ON

        bad = cli.send_command(client, "overlay", {"name": "fire"}, timeout_s=10)
        assert bad["type"] == "error" and "not supported" in bad["error"]

        client.subscribe(topics.EVENTS, lambda _t, payload: events.append(payload))
        time.sleep(0.3)  # the subscription is in place
        client._client.publish(topics.cmd("scene"), b"{not json", qos=1)  # bytes, not a command
        wait_until(
            "the engine to answer a message it cannot read",
            lambda: any("invalid JSON" in e.get("error", "") for e in events),
        )
        answer = next(e for e in events if "invalid JSON" in e.get("error", ""))
        assert answer["type"] == "error" and answer["command"] == "scene", answer
        assert engine.status()["scene"] == "broker-test"  # the engine is still playing it

        ok = cli.send_command(
            client, "set", {"name": "master_gain_db", "value": -6.0}, timeout_s=10
        )
        assert ok["type"] == "ok", ok  # and it answers what it can read
    finally:
        engine.stop(0.05)
        time.sleep(0.2)  # the state that ends the run says so
        link.close()
        output.close()
        client.close()


@pytest.fixture
def engine_link(broker, tmp_path):
    """A real engine (no sound card) on this broker, and a client bus that sends it commands.

    Yields `(engine, link, client, events)`: `events` is what the client saw on
    `rpghome/events`, in the order the broker gave them."""
    data = tmp_path / "data"
    data.mkdir()
    tone = np.arange(round(3 * RATE)) / RATE
    samples = (0.25 * np.sin(2 * np.pi * 440 * tone)).astype(np.float32)
    sf.write(str(data / "t.wav"), samples, RATE, "FLOAT")

    engine = Engine(data, seed=1)
    output = NullOutput(engine)
    output.start()
    bus = new_bus(will=(topics.ONLINE, topics.ONLINE_OFF), keepalive_s=5)
    link = EngineLink(engine, bus)
    bus.add_connect_hook(link.announce)
    link.start()
    client = new_bus()
    events: list[dict] = []
    client.subscribe(topics.EVENTS, lambda _t, payload: events.append(payload))
    time.sleep(0.3)  # the subscription is in place
    try:
        yield engine, link, client, events
    finally:
        engine.stop(0.05)
        time.sleep(0.2)  # the state that ends the run says so
        link.close()
        output.close()
        client.close()


def test_a_repeated_command_id_is_answered_and_not_dropped(engine_link):
    """QoS 1 is "at least once", so the same `(topic, id)` can arrive twice.

    The bus hands both over; the engine answers the redelivery with the event it gave before
    and applies the command once, and it refuses the same id carrying another payload."""
    engine, _link, client, events = engine_link

    def send(payload: dict) -> None:  # past MqttBus, exactly as another client does it
        client._client.publish(topics.cmd("set"), json.dumps(payload).encode(), qos=1)

    send({"name": "master_gain_db", "value": -6.0, "id": "1"})
    wait_until("the answer to id 1", lambda: any(e.get("id") == "1" for e in events))
    wait_until("master_gain_db -6", lambda: engine.status()["levels"]["master_gain_db"] == -6.0)

    send({"name": "master_gain_db", "value": -6.0, "id": "1"})  # the same message again
    send({"name": "master_gain_db", "value": -3.0, "id": "1"})  # the same id, another payload
    wait_until("three answers to id 1", lambda: len([e for e in events if e.get("id") == "1"]) == 3)

    answers = [e for e in events if e.get("id") == "1"]
    assert [a["type"] for a in answers] == ["ok", "ok", "error"], answers
    assert all(a["command"] == "set" for a in answers), answers
    assert "already used for another set command" in answers[2]["error"], answers[2]
    assert engine.status()["levels"]["master_gain_db"] == -6.0  # -3 was never applied


def test_the_broker_marks_the_engine_offline_when_it_is_killed(broker, env):
    """kill -9: the engine says nothing, the broker publishes its last will."""
    seen: list[dict] = []
    watcher = new_bus()
    watcher.subscribe(topics.ONLINE, lambda _t, payload: seen.append(payload))
    proc = engine_process(env)
    try:
        wait_until(
            "online: true from the engine", lambda: any(p.get("online") is True for p in seen), 30
        )
        seen.clear()
        proc.kill()  # kill -9: no DISCONNECT, so the will fires (keepalive 5 s -> ~8 s)
        wait_until(
            "online: false from the broker's last will",
            lambda: any(p.get("online") is False for p in seen),
            40,
        )
    finally:
        proc.kill()
        proc.wait(timeout=15)
        watcher.close()
    assert proc.returncode < 0  # killed by a signal, not by itself


def test_cli_status_reads_the_engine_over_the_broker(broker, env):
    seen: list[dict] = []
    watcher = new_bus()
    watcher.subscribe(topics.ONLINE, lambda _t, payload: seen.append(payload))
    proc = engine_process(env)
    try:
        wait_until(
            "online: true from the engine", lambda: any(p.get("online") is True for p in seen), 30
        )
        result = CliRunner().invoke(cli.app, ["status"])
        assert result.exit_code == 0, result.output
        assert "engine online: True" in result.output
        assert "seconds_rendered" in result.output
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        watcher.close()
    assert proc.returncode == 0  # SIGTERM: online: false published, a clean exit

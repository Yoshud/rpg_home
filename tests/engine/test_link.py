"""EngineLink: a real Engine (no sound card, the null device) driven only over a bus.

Everything a client does: publish on rpghome/cmd/<command>, read the events and the
retained state. Nothing calls the engine directly.
"""

import threading
import time
import uuid

import pytest

from rpghome.bus import topics
from rpghome.bus.memory import InMemoryBus
from rpghome_engine import clips
from rpghome_engine.core import STOPPING, Engine
from rpghome_engine.devices import NullOutput
from rpghome_engine.link import BOOT_NS, EngineLink, client_state

from .conftest import write_tone

RATE = 48000


def scene_payload(name="test", clips=("t.wav",), **bed):
    bed_data = {
        "clips": [{"path": c} for c in clips],
        "hold_s": [2, 2],
        "crossfade_s": 1,
        "loop_crossfade_s": 0.5,
    }  # fmt: skip
    return {"name": name, "bed": bed_data | bed}


def wait_for(what, predicate, timeout=6.0):
    """Poll a predicate: commands are applied by the control thread, not by the caller."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class Rig:
    """Engine + null device + InMemoryBus + link, with the events and states it published."""

    def __init__(self, data, *, control=True, **link_kw):
        """`control=False`: no device and no control thread, like an engine whose audio
        output failed to start - commands are queued and never applied."""
        self.engine = Engine(data, seed=1)
        self.output = NullOutput(self.engine) if control else None
        if self.output is not None:
            self.output.start()
        self.bus = InMemoryBus(will=(topics.ONLINE, topics.ONLINE_OFF))
        self.events: list[dict] = []
        self.states: list[dict] = []
        self.online: list[dict] = []
        self.bus.subscribe(topics.EVENTS, lambda _t, p: self.events.append(p))
        self.bus.subscribe(topics.STATE, lambda _t, p: self.states.append(p))
        self.bus.subscribe(topics.ONLINE, lambda _t, p: self.online.append(p))
        self.link = EngineLink(self.engine, self.bus, **link_kw)
        self.link.start()

    def close(self):
        self.link.close()
        if self.output is not None:
            self.output.close()

    def send(self, command, payload, timeout=6.0, msg_id=None):
        """Publish a command and return the event that answers it.

        A payload that is not an object gets no `id` (a test hands the engine junk), so the
        answer is matched by the command name among the events that came after the publish.
        Pass `msg_id` to send a specific one - e.g. an id a client uses twice."""
        msg_id = msg_id or f"{command}-{uuid.uuid4().hex[:8]}"
        message = {**payload, "id": msg_id} if isinstance(payload, dict) else payload
        after = len(self.events)
        self.bus.publish(topics.cmd(command), message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events[after:]:
                if event.get("command") == command and event.get("id", msg_id) == msg_id:
                    return event
            time.sleep(0.02)
        raise AssertionError(f"no event answered {command} ({msg_id}): {self.events[after:]}")

    def answer(self, command, **payload):
        event = self.send(command, payload)
        assert event["type"] == "ok", event
        return event

    def status(self):
        return self.engine.status()


@pytest.fixture
def rig(data):
    write_tone(data / "t.wav", 440, 3)
    rig = Rig(data, state_period_s=0.1, state_max_age_s=5.0)
    try:
        yield rig
    finally:
        rig.close()


@pytest.fixture
def taken(rig, monkeypatch):
    """What the engine itself took: `set` calls, in the order it took them."""
    calls: list[tuple[str, float]] = []
    real = rig.engine.set

    def counted(name: str, value: float) -> None:
        calls.append((name, value))
        real(name, value)

    monkeypatch.setattr(rig.engine, "set", counted)
    return calls


def test_the_engine_announces_itself_online(rig):
    assert rig.online[0] == topics.ONLINE_ON
    assert rig.bus.retained[topics.ONLINE] == topics.ONLINE_ON
    assert any(t == topics.ONLINE and r for t, _p, r in rig.bus.published)


def test_scene_stop_and_set_reach_the_engine_and_change_status(rig):
    rig.answer("scene", scene=scene_payload(), transition_s=1)
    wait_for("the scene to play", lambda: rig.status()["scene"] == "test")
    assert wait_for("a clip", lambda: rig.status()["clip"] == "t.wav")

    rig.answer("set", name="master_gain_db", value=-6.0)
    wait_for("master_gain_db", lambda: rig.status()["levels"]["master_gain_db"] == -6.0)

    rig.answer("set", name="bed_gain_db", value=-3.5)
    wait_for("bed_gain_db", lambda: rig.status()["levels"]["bed_gain_db"] == -3.5)

    rig.answer("stop", fade_s=0.1)
    wait_for("silence", lambda: rig.status()["scene"] is None)

    assert [e["command"] for e in rig.events] == ["scene", "set", "set", "stop"]
    assert all(e["type"] == "ok" for e in rig.events)


def test_a_scene_with_a_new_name_replaces_the_old_one(rig):
    rig.answer("scene", scene=scene_payload("first"), transition_s=0.2)
    wait_for("first", lambda: rig.status()["scene"] == "first")
    rig.answer("scene", scene=scene_payload("second"), transition_s=0.2)
    wait_for("second", lambda: rig.status()["scene"] == "second")
    wait_for("the old scene to fade out", lambda: rig.status()["fading_out"] == [])


BAD = [
    ("not JSON at all", "scene", "not an object", "invalid JSON"),
    ("a list, not an object", "scene", ["scene"], "invalid JSON"),
    ("unknown command", "dance", {}, "unknown command 'dance'"),
    ("missing field", "set", {"name": "master_gain_db"}, "value: Field required"),
    ("a typo in a field", "stop", {"fade": 2}, "fade: Extra inputs are not permitted"),
    ("a scene with no name", "scene", {"scene": {"bed": None}}, "name: Field required"),
    (
        "a scene with no such clip",
        "scene",
        {"scene": scene_payload(clips=("gone.wav",))},
        "clip not found",
    ),
    ("overlay", "overlay", {"name": "fire"}, "overlay is not supported by this engine yet"),
    (
        "fx",
        "fx",
        {"name": "fireball", "distance_m": 2500},
        "fx is not supported by this engine yet",
    ),
    (
        "a level that is not a number",
        "set",
        {"name": "master_gain_db", "value": "loud"},
        "Input should be a valid number",
    ),
    ("a gain out of range", "set", {"name": "bed_gain_db", "value": 900}, "must be within"),
    ("a density out of range", "set", {"name": "density", "value": 0}, "density must be within"),
    ("bytes a broker could not parse", "scene", b"{not json", "invalid JSON"),
]


@pytest.mark.parametrize(("what", "command", "payload", "message"), BAD)
def test_a_bad_command_answers_an_error_and_the_engine_keeps_working(
    rig, what, command, payload, message
):
    event = rig.send(command, payload)
    assert event["type"] == "error", event
    assert event["command"] == command
    assert message in event["error"], event["error"]
    if isinstance(payload, dict):
        assert event.get("id")  # the id is echoed, so the sender recognises its own message

    # the engine is still itself: the next valid command works
    rig.answer("scene", scene=scene_payload(), transition_s=0.2)
    wait_for("the scene to play", lambda: rig.status()["scene"] == "test")


def test_a_redelivery_of_the_same_command_is_answered_again_but_applied_once(rig, taken):
    payload = {"name": "master_gain_db", "value": -6.0}
    first = rig.send("set", payload, msg_id="dup")
    assert first["type"] == "ok", first

    for _ in range(2):  # QoS 1 promises "at least once": the same message can come twice
        again = rig.send("set", payload, msg_id="dup")
        assert again == first, again  # the sender gets its answer again, the same one
    assert [e for e in rig.events if e.get("id") == "dup"] == [first] * 3
    assert taken == [("master_gain_db", -6.0)]  # the engine took it once
    assert rig.status()["levels"]["master_gain_db"] == -6.0


def test_the_same_id_with_another_payload_is_refused(rig, taken):
    """A client that reuses an id: the second message is its mistake, not the engine's."""
    first = rig.send("set", {"name": "master_gain_db", "value": -6.0}, msg_id="1")
    assert first["type"] == "ok", first

    second = rig.send("set", {"name": "master_gain_db", "value": -3.0}, msg_id="1")
    assert second["type"] == "error", second
    assert second["command"] == "set" and second["id"] == "1", second
    assert (
        second["error"] == "id '1' was already used for another set command; ids must be unique"
    ), second["error"]
    assert taken == [("master_gain_db", -6.0)]  # -3 was never applied
    assert rig.status()["levels"]["master_gain_db"] == -6.0

    # the id keeps meaning its first command: that message is still answered as it was
    assert rig.send("set", {"name": "master_gain_db", "value": -6.0}, msg_id="1") == first


def test_the_same_id_on_another_command_is_another_command(rig):
    a = rig.send("set", {"name": "master_gain_db", "value": -6.0}, msg_id="same")
    b = rig.send("stop", {"fade_s": 0.05}, msg_id="same")
    assert a["type"] == "ok" and b["type"] == "ok", (a, b)


def test_a_command_without_an_id_is_taken_every_time_it_arrives(rig, taken):
    for value in (-6.0, -3.0):  # nothing to recognise them by, so both are taken
        rig.bus.publish(topics.cmd("set"), {"name": "master_gain_db", "value": value})
    wait_for("two answers", lambda: len([e for e in rig.events if e["command"] == "set"]) == 2)
    assert [e["type"] for e in rig.events] == ["ok", "ok"], rig.events
    assert all("id" not in e for e in rig.events), rig.events
    assert taken == [("master_gain_db", -6.0), ("master_gain_db", -3.0)]
    wait_for("the last value", lambda: rig.status()["levels"]["master_gain_db"] == -3.0)


def test_the_answer_to_a_command_already_carries_the_state_it_changed(data):
    """The real periods (state every 2 s): no waiting for the next state cycle.

    `Engine.scene` only queues the command for the control thread, so the link has to wait
    for that cycle before it publishes - otherwise the retained state a client reads right
    after its own command is the state from before it.
    """
    write_tone(data / "t.wav", 440, 3)
    rig = Rig(data)  # state_period_s=2.0, state_max_age_s=10.0: the defaults
    try:
        started = time.monotonic()
        rig.answer("scene", scene=scene_payload(), transition_s=0.2)
        state = rig.bus.retained[topics.STATE]  # published before the answer, so it is there now
        assert state["scene"] == "test", state
        assert state["clip"] == "t.wav", state
        assert time.monotonic() - started < 0.5, state  # the control cycle, not the 2 s period
    finally:
        rig.close()


def test_a_command_the_engine_never_applies_answers_an_error(data):
    """No control thread - the audio output failed to start, so nothing is ever applied.

    The command is queued and the link waits for the cycle that would apply it; when that
    never comes the answer is an error, never an `ok` for something that did not happen.
    """
    write_tone(data / "t.wav", 440, 3)
    rig = Rig(data, control=False)
    try:
        event = rig.send("set", {"name": "master_gain_db", "value": -6.0})
        assert event["type"] == "error", event
        assert event["command"] == "set" and event.get("id"), event
        assert "the engine did not apply the command within 1 s" in event["error"], event["error"]
        assert rig.status()["planning_cycles"] == 0  # no cycle ever ran
        assert rig.status()["levels"]["master_gain_db"] == 0.0  # and nothing was applied
    finally:
        rig.close()


def test_set_bed_gain_db_with_nothing_playing_answers_an_error(rig):
    event = rig.send("set", {"name": "master_gain_db", "value": -6.0})  # this one has a target
    assert event["type"] == "ok", event
    event = rig.send("set", {"name": "bed_gain_db", "value": -3.5})
    assert event["type"] == "error", event
    assert "bed_gain_db needs a scene with a bed that is playing" in event["error"], event

    rig.answer("scene", scene=scene_payload(), transition_s=0.2)  # now it has one
    wait_for("the scene to play", lambda: rig.status()["scene"] == "test")
    rig.answer("set", name="bed_gain_db", value=-3.5)


def test_set_density_needs_a_scene_and_scales_the_playing_one(rig):
    event = rig.send("set", {"name": "density", "value": 2.0})  # nothing is playing
    assert event["type"] == "error", event
    assert "density needs a scene that is playing" in event["error"], event

    rig.answer("scene", scene=scene_payload(), transition_s=0.2)  # now it has one
    wait_for("the scene to play", lambda: rig.status()["scene"] == "test")
    rig.answer("set", name="density", value=2.0)
    wait_for("the new density", lambda: rig.status()["levels"]["density"] == 2.0)


def test_a_command_during_the_shutdown_fade_is_refused(rig):
    rig.answer("scene", scene=scene_payload(), transition_s=0.2)
    wait_for("the scene to play", lambda: rig.status()["scene"] == "test")
    rig.link.begin_shutdown()  # the engine's closing fade has started
    rig.engine.stop(0.05)
    event = rig.send("stop", {"fade_s": 0.05})
    assert event["type"] == "error", event
    assert "engine is stopping" in event["error"], event
    assert rig.status()["seconds_rendered"] > 0  # the engine itself is fine, it is only leaving


def test_a_scene_still_loading_when_the_shutdown_starts_never_plays(data, monkeypatch):
    """SIGTERM while the clips are being read: the load finishes, the scene does not start."""
    write_tone(data / "t.wav", 440, 3)
    real_read = clips.read_clip

    def slow_read(path, rate):  # the load outlives the signal, like a folder of real clips
        time.sleep(0.3)
        return real_read(path, rate)

    monkeypatch.setattr(clips, "read_clip", slow_read)
    rig = Rig(data, state_period_s=0.1, state_max_age_s=5.0)
    try:
        rig.bus.publish(topics.cmd("scene"), {"scene": scene_payload(), "id": "slow"})
        time.sleep(0.1)  # the worker thread is in the middle of reading the clips
        assert rig.status()["scene"] is None
        rig.link.begin_shutdown()  # SIGTERM arrives here, and the load carries on
        wait_for(
            "the engine to answer the scene it loaded",
            lambda: any(e.get("id") == "slow" for e in rig.events),
        )
        answers = [e for e in rig.events if e.get("id") == "slow"]
        assert answers[0]["type"] == "error", answers
        assert STOPPING in answers[0]["error"], answers[0]
        assert rig.status()["scene"] is None  # what was loaded never reached the engine
    finally:
        rig.close()


def test_a_command_that_arrives_while_closing_still_gets_an_answer(rig):
    rig.link._stop.set()  # no worker to take it: it is still in the queue at close()
    time.sleep(0.3)
    rig.bus.publish(topics.cmd("scene"), {**scene_payload(), "id": "late-1"})
    rig.link.close()
    answers = [e for e in rig.events if e.get("id") == "late-1"]
    assert answers, rig.events  # nothing is swallowed on the way out
    assert answers[0]["type"] == "error" and "engine is stopping" in answers[0]["error"], answers


def test_state_versions_only_go_up(rig):
    rig.answer("scene", scene=scene_payload(), transition_s=0.2)
    rig.answer("set", name="master_gain_db", value=-3.0)
    versions = [s["version"] for s in rig.states]
    assert versions == sorted(versions) and len(set(versions)) == len(versions), versions

    other = [
        threading.Thread(target=rig.link.publish_state, kwargs={"force": True}) for _ in range(8)
    ]
    for thread in other:
        thread.start()
    for thread in other:
        thread.join()  # eight publishers at once: serialised, each with its own version
    versions = [s["version"] for s in rig.states]
    assert versions == sorted(versions) and len(set(versions)) == len(versions), versions


def test_the_state_carries_one_boot_for_the_whole_process(rig):
    rig.answer("set", name="master_gain_db", value=-3.0)
    other = EngineLink(rig.engine, rig.bus)  # another link, the same process: the same boot
    other.publish_state(force=True)
    assert {s["boot"] for s in rig.states} == {BOOT_NS}, rig.states
    assert isinstance(BOOT_NS, int) and 10**17 < BOOT_NS <= time.time_ns()  # ns since the epoch


def test_a_client_orders_states_by_boot_then_version(rig, monkeypatch):
    """After a restart `version` starts at 1 again; only `(boot, version)` says which is newer."""
    rig.answer("set", name="master_gain_db", value=-3.0)
    before = rig.states[-1]
    assert before["version"] > 1, before

    monkeypatch.setattr("rpghome_engine.link.BOOT_NS", BOOT_NS + 10**9)  # a new engine process
    EngineLink(rig.engine, rig.bus).publish_state(force=True)
    after = rig.states[-1]
    assert after["version"] == 1, after  # the version alone reads as "older than before"
    assert (after["boot"], after["version"]) > (before["boot"], before["version"]), (before, after)


def test_state_is_not_queued_behind_a_bus_that_cannot_publish(rig):
    rig.answer("scene", scene=scene_payload(), transition_s=0.2)
    wait_for("the scene in the state", lambda: rig.states[-1]["scene"] == "test")
    n = len(rig.states)
    rig.bus.kill()  # the client is gone: a state published now would go nowhere
    time.sleep(0.5)  # period 0.1 s: the state loop would have published five times
    assert len(rig.states) == n, rig.states[n:]
    rig.bus.publish("rpghome/cmd", {"id": "x"})  # `#` covers it, but it names no command
    time.sleep(0.3)
    assert not [e for e in rig.events if e.get("id") == "x"]


def test_events_are_not_retained_and_state_is(rig):
    rig.answer("scene", scene=scene_payload(), transition_s=0.2)
    flags = {topic: retain for topic, _p, retain in rig.bus.published}
    assert flags[topics.STATE] is True
    assert flags[topics.ONLINE] is True
    assert all(not retain for topic, _p, retain in rig.bus.published if topic == topics.EVENTS)


def test_a_late_subscriber_gets_the_last_state_at_once(rig):
    rig.answer("scene", scene=scene_payload(), transition_s=0.2)
    wait_for("the state to say test", lambda: rig.states and rig.states[-1]["scene"] == "test")
    late: list[dict] = []
    rig.bus.subscribe(topics.STATE, lambda _t, p: late.append(p))
    assert len(late) == 1, late  # retained: at once, without waiting for the next publish
    assert late[0]["scene"] == "test" and late[0]["clip"] == "t.wav"
    assert "timings_ms" not in late[0]  # a client gets what plays, not the engine's timings
    # the output health a listener would hear as crackles is part of the state
    assert [late[0][k] for k in ("underruns", "late_callbacks", "gaps", "nonfinite_blocks")] == [
        0,
        0,
        0,
        0,
    ]


def test_state_is_published_when_it_changes_and_at_least_every_state_max_age_s(rig):
    rig.answer("scene", scene=scene_payload(), transition_s=0.2)
    wait_for("the scene in the state", lambda: rig.states[-1]["scene"] == "test")
    n = len(rig.states)
    time.sleep(0.5)  # period 0.1 s, max age 5 s: nothing changed, so nothing new
    assert len(rig.states) == n, rig.states[n:]

    rig.answer("set", name="master_gain_db", value=-4.0)
    wait_for(
        "a new state",
        lambda: len(rig.states) > n and rig.states[-1]["levels"]["master_gain_db"] == -4.0,
    )


def test_state_is_refreshed_on_its_own_even_when_nothing_changes(data):
    write_tone(data / "t.wav", 440, 3)
    rig = Rig(data, state_period_s=0.1, state_max_age_s=0.3)
    try:
        n = len(rig.states)
        time.sleep(0.8)  # nothing happened, but 0.3 s is well behind us
        assert len(rig.states) >= n + 2, rig.states[n:]
    finally:
        rig.close()


def test_kill_delivers_the_last_will_online_false(rig):
    assert rig.online[-1] == topics.ONLINE_ON
    rig.bus.kill()
    assert rig.online[-1] == topics.ONLINE_OFF
    assert rig.bus.retained[topics.ONLINE] == topics.ONLINE_OFF


def test_close_publishes_online_false_and_the_state_that_ends_the_run(rig):
    rig.answer("scene", scene=scene_payload(), transition_s=0.2)
    wait_for("the scene to play", lambda: rig.status()["scene"] == "test")
    rig.engine.stop(0.05)
    time.sleep(0.2)
    rig.link.close()
    assert rig.online[-1] == topics.ONLINE_OFF
    final = rig.states[-1]
    assert final["scene"] is None and final["seconds_rendered"] > 0
    assert rig.bus.closed is True
    assert client_state(rig.status())["planning_cycles"] > 0

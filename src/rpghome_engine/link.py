"""The bus side of the engine process: commands in, events and state out.

Threads: a bus hands commands over in its own network thread, and `_on_command` only puts
the message on a queue - a slow command (loading a scene's clips) never blocks that thread.
A worker thread parses it, calls the engine and publishes the answer, so a bad command is
an error event and nothing else about the engine changes. A state thread publishes
`engine.status()` every `state_period_s`, only when what plays changed or when the last
publish is older than `state_max_age_s` (counters still refresh).

An `ok` event means the engine applied the command: the worker waits for the control cycle
that applies it and publishes the state before answering, so a client that reads the retained
state after its own command already sees what plays. When that cycle never comes, the answer
is an error - a command is never reported as applied when it was not.

QoS 1 can deliver the same command twice, and a client can reuse an id. The link remembers
the payload and the event of each recent `(topic, id)` (see `rpghome.bus.recent`): a
redelivery is answered again with the earlier event and applied once, and the same id with
another payload is refused as the client's mistake.
"""

import logging
import queue
import threading
import time
from typing import Any

from rpghome.bus import topics
from rpghome.bus.base import Bus
from rpghome.bus.recent import Answered, RecentCommands, message_id
from rpghome.models.commands import (
    Command,
    CommandError,
    SceneCommand,
    SetCommand,
    StopCommand,
    error_event,
    ok_event,
    parse_command,
)
from rpghome_engine.clips import EngineError
from rpghome_engine.core import STOPPING, Engine

log = logging.getLogger("rpghome.engine")

#: what a client of `rpghome/state` gets out of `status()`: what plays, the levels, the
#: counters. The timings (timings_ms) stay out: they are for the engine's own reports.
STATE_KEYS = (
    "scene",
    "clip",
    "hold_s",
    "hold_elapsed_s",
    "levels",
    "fading_out",
    "seconds_rendered",
    "output_peak_dbfs",
    "output_peak_max_dbfs",
    "limiter_gain_db",
    "limiter_min_gain_db",
    "underruns",
    "late_callbacks",
    "gaps",
    "nonfinite_blocks",
    "starved_blocks",
    "late_events",
    "bad_messages",
    "planning_cycles",
)

STATE_PERIOD_S = 2.0
STATE_MAX_AGE_S = 10.0
APPLIED_WAIT_S = 1.0  # a command is answered once the control cycle has applied it
APPLIED_POLL_S = 0.01

#: When this process started, in ns since the epoch - the same number in every state this
#: process publishes. `version` starts at 1 again in every engine process, so a client orders
#: the states it is given by `(boot, version)` (see the README, "Bus").
BOOT_NS = time.time_ns()


def client_state(status: dict) -> dict:
    """`Engine.status()` as a client sees it."""
    return {key: status[key] for key in STATE_KEYS if key in status}


class EngineLink:
    def __init__(
        self,
        engine: Engine,
        bus: Bus,
        *,
        state_period_s: float = STATE_PERIOD_S,
        state_max_age_s: float = STATE_MAX_AGE_S,
    ):
        self.engine = engine
        self.bus = bus
        self._state_period_s = state_period_s
        self._state_max_age_s = state_max_age_s
        self._commands: queue.Queue[tuple[str, Any, str | None]] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._state_key: tuple | None = None
        self._state_at = 0.0
        self._state_lock = threading.Lock()  # the command thread and the state thread publish
        self._state_version = 0  # rises with every publish, so no client sees state go back
        self._recent = RecentCommands()  # (topic, id) -> the payload and the event it got
        self.closed = False
        self._closing = False  # the shutdown fade started: no command is taken any more

    # -- start / stop -----------------------------------------------------------------

    def start(self) -> None:
        self.bus.subscribe(topics.CMD_FILTER, self._on_command, self._on_invalid)
        self.announce()
        for name, target in (("commands", self._run_commands), ("state", self._run_state)):
            thread = threading.Thread(target=target, name=f"engine-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def announce(self) -> None:
        """retained `online: true` plus the state as it is now; an MqttBus runs it again
        after every (re)connect, which is also what publishes state after an outage."""
        self._publish(topics.ONLINE, topics.ONLINE_ON, retain=True)
        self.publish_state(force=True)

    def begin_shutdown(self) -> None:
        """The shutdown fade has begun: from here every command is refused, not applied."""
        self._closing = True

    def close(self) -> None:
        """online: false, the state that ends the run, then close the bus."""
        if self.closed:
            return
        self.closed = True
        self._closing = True
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._drain()  # whatever arrived while closing still gets an answer
        self._publish(topics.ONLINE, topics.ONLINE_OFF, retain=True)
        self.publish_state(force=True)
        self.bus.close()

    def _drain(self) -> None:
        while True:
            try:
                topic, payload, reason = self._commands.get_nowait()
            except queue.Empty:
                return
            self._handle(topic, payload, reason)

    # -- commands ---------------------------------------------------------------------

    def _on_command(self, topic: str, payload: dict) -> None:
        """Bus thread: only hand the message over."""
        if topics.command_of(topic) is None:
            log.warning("ignoring a message on %s: not a command topic", topic)
            return
        self._commands.put((topic, payload, None))

    def _on_invalid(self, topic: str, _raw: bytes, reason: str) -> None:
        """Bus thread: the engine answers a message it cannot read, too."""
        if topics.command_of(topic) is None:
            log.warning("ignoring a message on %s: not a command topic", topic)
            return
        self._commands.put((topic, _raw, reason))

    def _run_commands(self) -> None:
        while not self._stop.is_set():
            try:
                topic, payload, reason = self._commands.get(timeout=0.2)
            except queue.Empty:
                continue
            self._handle(topic, payload, reason)

    def is_stopping(self) -> bool:
        """The engine is already leaving: a slow command must not hand it more work."""
        return self._closing

    def _handle(self, topic: str, payload: Any, reason: str | None) -> None:
        command = topics.command_of(topic)  # the topic names it; checked when the message came in
        if command is None:
            return
        if reason is not None:
            self._refuse(topic, command, payload, f"invalid JSON: {reason}")
            return
        if self._closing:
            self._refuse(topic, command, payload, STOPPING)
            return
        earlier = self._recent.answer_of(topic, payload)
        if earlier is not None:  # this (topic, id) was answered before: see _repeat
            self._repeat(topic, command, payload, earlier)
            return
        try:
            cmd = parse_command(command, payload)
            self._apply(cmd)
        except (CommandError, EngineError) as exc:
            self._refuse(topic, command, payload, str(exc))
        except Exception as exc:  # a broken command never stops the engine
            log.exception("command %s failed", command)
            event = error_event(command, f"engine error: {exc}", message_id(payload))
            self._answer(topic, payload, event)
        else:
            if not self._wait_applied():
                self._refuse(
                    topic,
                    command,
                    payload,
                    f"the engine did not apply the command within {APPLIED_WAIT_S:g} s",
                )
                return
            self.publish_state(force=True)  # what the command changed, before the answer
            self._answer(topic, payload, ok_event(cmd.command, cmd.id))
            log.info("command %s done", command)

    def _repeat(self, topic: str, command: str, payload: Any, earlier: Answered) -> None:
        """A `(topic, id)` the engine has already answered: a redelivery, or an id used twice.

        The same payload again is the broker delivering one command twice: it is applied once,
        and the earlier event goes out again so the sender is not left without its answer. A
        different payload under the same id is the sender's mistake: it is refused, and the
        engine keeps playing what it played.
        """
        msg_id = message_id(payload)
        if earlier.payload == payload:
            log.info("id %s on %s again: answered again, not applied again", msg_id, topic)
            self._event(earlier.event)
            return
        message = (
            f"id {msg_id!r} was already used for another {command} command; ids must be unique"
        )
        log.warning("command %s refused: %s", command, message)
        self._event(error_event(command, message, msg_id))

    def _refuse(self, topic: str, command: str, payload: Any, message: str) -> None:
        log.warning("command %s refused: %s", command, message)
        self._answer(topic, payload, error_event(command, message, message_id(payload)))

    def _answer(self, topic: str, payload: Any, event: dict) -> None:
        """One event for one command, remembered against its `(topic, id)`."""
        self._recent.remember(topic, payload, event)
        self._event(event)

    def _apply(self, cmd: Command) -> None:
        match cmd:
            case SceneCommand():
                # `should_stop`: the clips are read here, in this thread, and the shutdown may
                # start while they are being read - the engine then refuses the scene instead
                # of starting it during its own fade-out.
                if cmd.transition_s is None:
                    self.engine.scene(cmd.scene, should_stop=self.is_stopping)
                else:
                    self.engine.scene(
                        cmd.scene, transition_s=cmd.transition_s, should_stop=self.is_stopping
                    )
            case StopCommand():
                if cmd.fade_s is None:
                    self.engine.stop()
                else:
                    self.engine.stop(fade_s=cmd.fade_s)
            case SetCommand():
                levels = self.engine.status()["levels"]  # None: the engine would only warn
                if cmd.name == "bed_gain_db" and levels["bed_gain_db"] is None:
                    raise EngineError("bed_gain_db needs a scene with a bed that is playing")
                if cmd.name == "density" and levels["density"] is None:
                    raise EngineError("density needs a scene that is playing")
                self.engine.set(cmd.name, cmd.value)
            case _:
                raise EngineError(f"{cmd.command} is not supported by this engine yet")

    def _wait_applied(self) -> bool:
        """Wait for the control cycle that applies what was queued.

        `Engine.scene/stop/set` only queue the command; the control thread applies it within
        its period. Waiting for the queue to empty *and* for a cycle to finish after that is
        what makes the state published next the truth about the command. Bounded, so a control
        thread that is stuck or never started costs an error answer, never an `ok` for a
        command that was not applied.
        """
        deadline = time.monotonic() + APPLIED_WAIT_S
        while self.engine.has_commands and time.monotonic() < deadline:
            time.sleep(APPLIED_POLL_S)
        cycles = self.engine.status()["planning_cycles"]  # rises at the end of a cycle
        while time.monotonic() < deadline:
            if self.engine.status()["planning_cycles"] != cycles:
                return True
            time.sleep(APPLIED_POLL_S)
        return False

    def _event(self, event: dict) -> None:
        self._publish(topics.EVENTS, event)

    # -- state --------------------------------------------------------------------------

    def publish_state(self, force: bool = False) -> None:
        """Publish `status()`, but only when a client would see something new.

        Both the command thread and the state thread publish it, so one is inside the lock
        while the other waits: the versions come out in order and no client ever sees an
        older state. A bus that is retrying a broker that is down gets nothing - state is a
        snapshot, and `announce()` publishes the current one when the connection is back.

        `version` counts the publishes of this process, so it starts at 1 again after a
        restart: `boot` says which process a state came from, and a client orders the states
        it is given by `(boot, version)`.
        """
        if not self.bus.connected:
            return
        with self._state_lock:
            status = self.engine.status()  # cheap: any number of readers may poll it
            key = self._state_key_of(status)
            now = time.monotonic()
            if (
                not force
                and key == self._state_key
                and now - self._state_at < self._state_max_age_s
            ):
                return
            self._state_key, self._state_at = key, now
            self._state_version += 1
            state = client_state(status) | {"boot": BOOT_NS, "version": self._state_version}
            self._publish(topics.STATE, state, retain=True)

    @staticmethod
    def _state_key_of(status: dict) -> tuple:
        """What plays now - not the counters, which always move."""
        return (
            status["scene"],
            status["clip"],
            status["hold_s"],
            tuple(sorted(status["levels"].items())),
            tuple(status["fading_out"]),
        )

    def _run_state(self) -> None:
        while not self._stop.wait(self._state_period_s):
            try:
                self.publish_state()
            except Exception:  # the state loop must survive
                log.exception("publishing the engine state failed")

    # -- publishing ----------------------------------------------------------------------

    def _publish(self, topic: str, payload: dict, retain: bool = False) -> None:
        try:
            self.bus.publish(topic, payload, retain=retain)
        except Exception:  # a bus that cannot publish must not stop the engine
            log.exception("publishing on %s failed", topic)

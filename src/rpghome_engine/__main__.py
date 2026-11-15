"""The engine process: `python -m rpghome_engine`.

Without arguments (how launchd runs it) it opens the output stream, plays silence and
waits for commands (none arrive until the bus exists) and for SIGTERM / SIGINT.
For manual and orchestrator tests:

    python -m rpghome_engine --scene scene.json [--seconds N] [--report report.json]
                             [--offline out.wav] [--seed S] [--transition S]

Exit codes: 0 after SIGTERM / SIGINT, after --seconds, and after a configuration error
(invalid settings, bad arguments, unreadable scene or clip), which is logged, so launchd
(KeepAlive SuccessfulExit=false) does not restart a job that cannot work.
Anything unexpected exits 1 and launchd restarts it.
"""

import argparse
import datetime
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from pathlib import Path

from rpghome.core.config import ConfigError, Settings, load_settings
from rpghome.core.logs import add_stderr, setup_logging
from rpghome.models.scene import Scene, SceneError, scene_from_json
from rpghome_engine.clips import EngineError
from rpghome_engine.core import Engine
from rpghome_engine.devices import (
    AudioOutputError,
    DeviceNotFoundError,
    NullOutput,
    OfflineDevice,
    SoundDeviceOutput,
)
from rpghome_engine.memory import MemoryPin, NoPin

log = logging.getLogger("rpghome.engine")

STOP_SIGNALS = {signal.SIGTERM, signal.SIGINT}
LOG_FILE = "engine.log"
STATUS_LOG_EVERY_S = 60.0
STOP_FADE_S = 0.5
DEFAULT_LATENCY_S = 0.05  # ambience tolerates more; effects need < 100 ms click to sound
AUDIO_FAILURE_FILE = "engine-audio-failure.json"


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ConfigError(f"invalid arguments: {message}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = _Parser(prog="python -m rpghome_engine", description="RPG HOME audio engine")
    p.add_argument("--scene", type=Path, help="scene JSON (clip paths relative to the data folder)")
    p.add_argument("--seconds", type=float, help="play this long, then exit")
    p.add_argument("--report", type=Path, help="write the final status and measurements as JSON")
    p.add_argument("--offline", type=Path, metavar="OUT.wav", help="render to a WAV, no device")
    p.add_argument("--seed", type=int, help="random seed (default: random, logged)")
    p.add_argument("--transition", type=float, default=2.0, help="fade-in of --scene (s)")
    args = p.parse_args(argv)
    if args.offline is not None and args.seconds is None:
        raise ConfigError("--offline needs --seconds")
    if args.seconds is not None and not args.seconds > 0:
        raise ConfigError("--seconds must be > 0")
    return args


def read_scene(path: Path) -> Scene:
    try:
        return scene_from_json(path.read_bytes())
    except OSError as exc:
        raise ConfigError(f"cannot read scene file {path}: {exc.strerror or exc}") from None
    except SceneError as exc:
        raise ConfigError(f"invalid scene file {path}: {exc}") from None


def audio_latency(value: str | None) -> float | str:
    """RPGHOME_AUDIO_LATENCY: seconds, or PortAudio's "low" / "high"."""
    value = (value or "").strip().lower()
    if not value:
        return DEFAULT_LATENCY_S
    if value in ("low", "high"):
        return value
    try:
        seconds = float(value)
    except ValueError:
        seconds = -1.0
    if not 0 < seconds <= 2:
        raise ConfigError(
            f"RPGHOME_AUDIO_LATENCY must be seconds (0 < s <= 2), 'low' or 'high', got {value!r}"
        )
    return seconds


def open_output(engine: Engine):
    device = os.environ.get("RPGHOME_AUDIO_DEVICE", "").strip()
    latency = audio_latency(os.environ.get("RPGHOME_AUDIO_LATENCY"))
    if device in ("null", "null-stalled"):
        return NullOutput(engine, stalled=device == "null-stalled")
    try:
        return SoundDeviceOutput(
            engine, int(device) if device.isdigit() else device or None, latency
        )
    except DeviceNotFoundError as exc:
        raise ConfigError(str(exc)) from None


def record_audio_failure(state_dir: Path, reason: str) -> None:
    """The last audio failure, for status.sh and humans: launchd restarts us every 5 s."""
    path = state_dir / AUDIO_FAILURE_FILE
    data = {
        "time": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "reason": reason,
    }
    try:
        path.write_text(json.dumps(data) + "\n")
    except OSError as exc:
        log.warning("cannot write %s: %s", path, exc)


def clear_audio_failure(state_dir: Path) -> None:
    path = state_dir / AUDIO_FAILURE_FILE
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("cannot remove %s: %s", path, exc)
        return
    log.info("audio output plays again: removed %s", path)


def write_report(path: Path, report: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n")
    except OSError as exc:
        log.error("cannot write the report %s: %s", path, exc)
        return
    log.info("report written to %s", path)


def build_report(
    engine: Engine,
    args: argparse.Namespace,
    mode: str,
    wall_s: float,
    playing: dict | None,
    failure: str | None = None,
) -> dict:
    """playing: status() just before the closing fade-out (what was playing).

    The run is valid only if the output played: no failure, and the callback ran (an
    active stream that never calls back renders nothing and counts no underruns)."""
    status = engine.status()
    rendered = engine.mixer.clock / engine.rate
    callbacks = status["timings_ms"]["callback"]["count"]
    if failure is not None:
        reason = failure
    elif callbacks == 0:
        reason = "the audio callback never ran: nothing played, the counters mean nothing"
    else:
        reason = None
    # Played, but not cleanly: the listener may have heard crackles.
    degraded = [f"{k}={status[k]}" for k in ("underruns", "late_callbacks", "gaps") if status[k]]
    return {
        "valid": reason is None,
        "invalid_reason": reason,
        "degraded": bool(degraded),
        "degraded_reason": ", ".join(degraded) or None,
        "mode": mode,
        "seed": engine.seed,
        "scene_file": str(args.scene) if args.scene else None,
        "seconds_requested": args.seconds,
        "seconds_rendered": round(rendered, 3),
        "wall_s": round(wall_s, 3),
        "status": playing or status,
        "measurements": {
            "seconds_rendered": round(rendered, 3),
            "callbacks": callbacks,
            "underruns": status["underruns"],
            "late_callbacks": status["late_callbacks"],
            "gaps": status["gaps"],
            "max_gap_ms": status["max_gap_ms"],
            "slow_callbacks": status["slow_callbacks"],
            "min_headroom_ms": status["min_headroom_ms"],
            "nonfinite_blocks": status["nonfinite_blocks"],
            "starved_blocks": status["starved_blocks"],
            "late_events": status["late_events"],
            "planning_cycles": status["planning_cycles"],
            "control_ms": status["timings_ms"]["control"],
            "callback_ms": status["timings_ms"]["callback"],
            "callback_budget_ms": status["timings_ms"]["callback_budget"],
            "realtime_factor": round(rendered / wall_s, 1) if wall_s > 0 else None,
        },
        "memory": engine.memory.summary(),
        "bed_timeline": engine.timeline(),
        "spot_timeline": engine.spot_timeline(),
    }


class _StopSignals:
    """SIGTERM / SIGINT set an event that the main loop waits on (macOS has no
    sigtimedwait). Installed first thing in main(), so no signal is lost."""

    def __init__(self):
        self.event = threading.Event()
        self.received: int | None = None
        for sig in STOP_SIGNALS:
            signal.signal(sig, self._handle)

    def _handle(self, signum, frame) -> None:
        if self.received is None:
            self.received = signum
        self.event.set()

    @property
    def name(self) -> str:
        return signal.Signals(self.received).name if self.received else "?"


def pin_memory():
    """mlockall, else per-buffer mlock (see memory.py); never fatal."""
    try:
        memory = MemoryPin()
    except Exception as exc:
        log.warning("memory: cannot use mlock (%s); memory is not locked", exc)
        return NoPin()
    memory.pin_all()
    return memory


def run(args: argparse.Namespace, settings: Settings, stop: _StopSignals) -> int:
    scene = read_scene(args.scene) if args.scene else None
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    memory = pin_memory() if args.offline is None else NoPin()
    engine = Engine(settings.artifacts_dir, seed=seed, memory=memory)
    if scene is not None:
        try:
            engine.scene(scene, transition_s=args.transition)
        except EngineError as exc:
            raise ConfigError(f"scene {scene.name!r}: {exc}") from None
    if args.offline is None:
        log.info("memory: %s", memory.summary())
    started = time.monotonic()
    common = (
        f"pid={os.getpid()} seed={seed} data={settings.artifacts_dir} state={settings.state_dir}"
    )

    if args.offline is not None:
        log.info("engine started offline %s out=%s seconds=%g", common, args.offline, args.seconds)
        dev = OfflineDevice(engine)
        frames = round(args.seconds * engine.rate)
        args.offline.parent.mkdir(parents=True, exist_ok=True)
        dev.render_to_file(args.offline, frames, should_stop=stop.event.is_set)
        mode, playing, failure = "offline", None, None
    else:
        deadline = started + args.seconds if args.seconds else None
        next_status = started + STATUS_LOG_EVERY_S
        playing, failure, output = None, None, None
        try:
            output = open_output(engine)
            log.info("engine started %s device=%s", common, output.description)
            output.start()
            cleared = False
            while True:
                now = time.monotonic()
                timeout = 1.0 if deadline is None else max(0.0, min(1.0, deadline - now))
                if stop.event.wait(timeout):
                    log.info("received %s, stopping", stop.name)
                    break
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    log.info("played %g s, stopping", args.seconds)
                    break
                output.check()
                if not cleared and engine.mixer.clock > 0:
                    cleared = True  # output started and callbacks arrive
                    clear_audio_failure(settings.state_dir)
                if now >= next_status:
                    next_status += STATUS_LOG_EVERY_S
                    s = engine.status()
                    log.info(
                        "status: scene=%s clip=%s rendered=%.1f s underruns=%d "
                        "late_callbacks=%d gaps=%d max_gap=%.1f ms min_headroom=%s ms "
                        "starved=%d callback=%s control=%s",
                        s["scene"],
                        s["clip"],
                        s["seconds_rendered"],
                        s["underruns"],
                        s["late_callbacks"],
                        s["gaps"],
                        s["max_gap_ms"],
                        s["min_headroom_ms"],
                        s["starved_blocks"],
                        s["timings_ms"]["callback"],
                        s["timings_ms"]["control"],
                    )
            playing = engine.status()
            engine.stop(STOP_FADE_S)
            time.sleep(STOP_FADE_S + 0.1)
        except AudioOutputError as exc:
            failure = str(exc)
            log.error("no audio output: is the lid closed or the output device missing? (%s)", exc)
            record_audio_failure(settings.state_dir, failure)
        finally:
            if output is not None:
                output.close()
        mode = "device"
    wall = time.monotonic() - started
    if args.report:
        write_report(args.report, build_report(engine, args, mode, wall, playing, failure))
    s = engine.status()
    log.info(
        "engine stopped: %.1f s rendered, underruns=%d late_callbacks=%d gaps=%d starved=%d "
        "callback=%s control=%s",
        engine.mixer.clock / engine.rate,
        s["underruns"],
        s["late_callbacks"],
        s["gaps"],
        s["starved_blocks"],
        s["timings_ms"]["callback"],
        s["timings_ms"]["control"],
    )
    return 1 if failure else 0


def main(argv: list[str] | None = None) -> int:
    stop = _StopSignals()
    to_stderr = sys.stderr.isatty()
    try:
        settings = load_settings()
    except ConfigError as exc:
        setup_logging(None, LOG_FILE, True)  # no state folder to log to: stderr (engine.stdout.log)
        log.error("configuration error, not starting: invalid settings: %s", exc)
        return 0
    try:
        args, bad_args = parse_args(argv), None
    except ConfigError as exc:
        args, bad_args = None, exc
    try:
        to_stderr = to_stderr or bad_args is not None or args.offline is not None
        setup_logging(settings.log_dir, LOG_FILE, to_stderr)
    except ConfigError as exc:  # the state or log folder is not usable
        setup_logging(None, LOG_FILE, True)  # stderr only: engine.stdout.log under launchd
        log.error("configuration error, not starting: %s", exc)
        return 0
    if bad_args is not None:
        log.error("configuration error, not starting: %s", bad_args)
        return 0
    try:
        return run(args, settings, stop)
    except ConfigError as exc:
        add_stderr()
        log.error("configuration error, not starting: %s", exc)
        if args.report:
            write_report(args.report, {"valid": False, "error": str(exc)})
        return 0
    except Exception:
        log.exception("engine crashed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

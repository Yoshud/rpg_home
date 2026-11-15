"""The engine object: thread-safe command API, the control cycle and status.

Threads:
  audio    render_block(): Mixer.render, called by the device callback (or a fake device)
  control  control_cycle() a few times per second: applies commands and asks each
           element for its material in the next HORIZON_S seconds, which it posts to
           the mixer as buffers with a start sample
  callers  scene() / stop() / set() / status() from any thread; scene() loads the clips
           in the caller's thread, so a bad file is an error there and nothing changes
"""

import collections
import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from rpghome.models.scene import MAX_GAIN_DB, MIN_GAIN_DB, Scene, SceneError, scene_from_data
from rpghome_engine.bed import BedPlayer
from rpghome_engine.clips import EngineError, LoopClip, db_to_gain, load_loop_clip
from rpghome_engine.memory import MemoryPin, NoPin
from rpghome_engine.mixer import Mixer
from rpghome_engine.stats import Timings

log = logging.getLogger("rpghome.engine")

RATE = 48000
CHANNELS = 2
BLOCK = 512
CONTROL_PERIOD_S = 0.2
HORIZON_S = 5.0
LEAD_BLOCKS = 2  # a command takes effect this many blocks after the current clock
LATE_HEADROOM_S = 0.002  # a block that reaches the DAC sooner than this after now is late
MIN_FADE_S = 0.01  # even a "cut" is a 10 ms fade, never a click

LEVELS = ("master_gain_db", "bed_gain_db")


@dataclass
class _LoadedScene:
    scene: Scene
    bed_clips: list[LoopClip] = field(default_factory=list)


@dataclass
class _Run:
    """One scene instance, from its start until its fade-out ends."""

    sid: int
    scene: Scene
    start: int
    bed: BedPlayer | None
    bed_gain_db: float
    released: bool = False
    logged: int = 0  # bed history entries already logged


def _db(gain: float) -> float:
    return round(20 * math.log10(max(gain, 1e-9)), 2)


def _dbfs(peak: float) -> float | None:
    return _db(peak) if peak > 0 else None


class Engine:
    def __init__(
        self,
        data_dir: Path | str,
        *,
        seed: int | None = None,
        rate: int = RATE,
        blocksize: int = BLOCK,
        horizon_s: float = HORIZON_S,
        control_period_s: float = CONTROL_PERIOD_S,
        memory: "MemoryPin | NoPin | None" = None,
    ):
        self.data_dir = Path(data_dir)
        self.rate = rate
        self.blocksize = blocksize
        self.horizon = round(horizon_s * rate)
        self.control_period_s = control_period_s
        self.seed = seed
        self._rng = random.Random(seed)
        self.memory = memory or NoPin()
        self.mixer = Mixer(rate, CHANNELS, max_block=max(4096, blocksize), pin=self.memory.pin)
        self._commands: collections.deque = collections.deque()
        self._wake = threading.Event()
        self._lock = threading.Lock()  # control state vs status(); never taken by audio
        self._runs: dict[int, _Run] = {}
        self._current: _Run | None = None
        self._next_sid = 1
        self._master_gain_db = 0.0
        self._timeline: collections.deque = collections.deque(maxlen=10000)
        self.callback_times = Timings()
        self.control_times = Timings()
        self.underruns = 0  # device underflows, counted by the device callback
        # Real-time health, measured by the engine itself (CoreAudio does not report a
        # late callback as an underflow through PortAudio).
        self.period_s = blocksize / rate
        # PortAudio on CoreAudio calls back in bursts (3-4 blocks per host buffer at 50 ms
        # latency), so wall-clock spacing says nothing; the DAC timeline does.
        self.late_callbacks = 0  # headroom to the DAC below LATE_HEADROOM_S (or negative)
        self.gaps = 0  # the DAC time jumped by more than the frames rendered: samples lost
        self.max_gap_s = 0.0  # largest loss (DAC jump beyond the frames rendered)
        self.min_headroom_s: float | None = None  # min(outputBufferDacTime - currentTime)
        self.slow_callbacks = 0  # callbacks whose own work took longer than a block period
        self._dac_ref = 0.0  # DAC time of the last callback where it changed
        self._frames_since_ref = 0  # frames rendered from that callback on
        self.cycles = 0
        self._bad_logged = 0

    # -- command API (any thread) ---------------------------------------------------

    def scene(self, scene: Scene | dict, transition_s: float = 10.0) -> None:
        """Switch to `scene` with an equal-power crossfade of transition_s.

        Loads the clips first; raises EngineError (and changes nothing) if one is
        missing or unreadable.
        """
        if isinstance(scene, dict):
            try:
                scene = scene_from_data(scene)
            except SceneError as exc:
                raise EngineError(f"invalid scene: {exc}") from None
        transition_s = self._seconds(transition_s, "transition_s")
        loaded = self._load(scene)
        self._command(("scene", loaded, transition_s))

    def stop(self, fade_s: float = 2.0) -> None:
        """Fade everything out over fade_s; then the engine plays silence."""
        self._command(("stop", self._seconds(fade_s, "fade_s")))

    def set(self, name: str, value: float) -> None:
        """Set a level: master_gain_db, or bed_gain_db (the bed of the current scene;
        a new scene starts at its own bed gain_db)."""
        if name not in LEVELS:
            raise EngineError(f"unknown level {name!r}; known: {', '.join(LEVELS)}")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise EngineError(f"{name} must be a number, got {value!r}")
        if not MIN_GAIN_DB <= value <= MAX_GAIN_DB:
            raise EngineError(f"{name} must be within {MIN_GAIN_DB:g}..{MAX_GAIN_DB:g} dB")
        self._command(("set", name, float(value)))

    def status(self) -> dict[str, Any]:
        with self._lock:
            clock = self.mixer.clock
            run = self._current
            out: dict[str, Any] = {
                "scene": run.scene.name if run else None,
                "clip": None,
                "hold_s": None,
                "hold_elapsed_s": None,
                "time_s": round(clock / self.rate, 3),
                "levels": {
                    "master_gain_db": self._master_gain_db,
                    "bed_gain_db": run.bed_gain_db if run and run.scene.bed else None,
                    "scene_gain_db": run.scene.gain_db if run else None,
                },
                "fading_out": sorted(r.scene.name for r in self._runs.values() if r.released),
            }
            if run and run.bed:
                cur = run.bed.current(clock)
                out["clip"] = cur["clip"]
                out["hold_s"] = None if cur["hold"] is None else round(cur["hold"] / self.rate, 3)
                out["hold_elapsed_s"] = round(max(0, clock - cur["start"]) / self.rate, 3)
        m = self.mixer
        # Meters are only read here, never reset: any number of readers can poll.
        out.update(
            {
                "seconds_rendered": round(m.clock / self.rate, 3),
                "output_peak_dbfs": _dbfs(m.peak.recent()),
                "output_peak_max_dbfs": _dbfs(m.peak.total),
                "limiter_gain_db": _db(m.limiter_gain.recent()),
                "limiter_min_gain_db": _db(m.limiter_gain.total),
                "underruns": self.underruns,
                "late_callbacks": self.late_callbacks,
                "gaps": self.gaps,
                "max_gap_ms": round(self.max_gap_s * 1e3, 3),
                "slow_callbacks": self.slow_callbacks,
                "min_headroom_ms": (
                    None if self.min_headroom_s is None else round(self.min_headroom_s * 1e3, 3)
                ),
                "nonfinite_blocks": m.limiter.nonfinite_blocks,
                "starved_blocks": m.starved_blocks,
                "late_events": m.late_events,
                "bad_messages": m.bad_messages,
                "planning_cycles": self.cycles,
                "timings_ms": {
                    "control": self.control_times.summary_ms(),
                    "callback": self.callback_times.summary_ms(),
                    "callback_budget": round(self.blocksize / self.rate * 1e3, 3),
                },
            }
        )
        return out

    def timeline(self) -> list[dict]:
        """Every bed clip started so far: scene, clip, start_s, hold_s, crossfade_s."""
        with self._lock:
            self._collect_timeline()
            return list(self._timeline)

    # -- audio thread -----------------------------------------------------------------

    def render_block_rt(self, out: np.ndarray, time_info=None) -> None:
        """render_block for a real-time device, which also follows the DAC timeline
        (PortAudio's outputBufferDacTime and currentTime, when it gives them):

        - late: the block reaches the DAC less than LATE_HEADROOM_S after now (or never);
        - gap: from the last callback where the DAC time changed, it moved further than
          the frames rendered since then plus one block, so samples were lost. Counting
          from the last change works whether PortAudio advances the DAC time inside a
          burst of callbacks or repeats it, and clock drift cannot add up.

        Only float arithmetic: nothing allocated beyond Python floats, no locks."""
        t = time.perf_counter()
        if self.memory.audio_thread is None:
            self.memory.audio_thread = threading.get_ident()
        n = len(out)
        if time_info is not None:
            cur, dac = time_info.currentTime, time_info.outputBufferDacTime
            if cur > 0 and dac > 0:
                headroom = dac - cur
                if self.min_headroom_s is None or headroom < self.min_headroom_s:
                    self.min_headroom_s = headroom
                if headroom < LATE_HEADROOM_S:
                    self.late_callbacks += 1
                if dac != self._dac_ref:
                    if self._dac_ref:
                        lost = (dac - self._dac_ref) - self._frames_since_ref / self.rate
                        if lost > 0.5 * self.period_s:  # more than one block of jump
                            self.gaps += 1
                        if lost > self.max_gap_s:
                            self.max_gap_s = lost
                    self._dac_ref, self._frames_since_ref = dac, 0
                self._frames_since_ref += n
        self.mixer.render(out)
        d = time.perf_counter() - t
        self.callback_times.add(d)
        if d > self.period_s:
            self.slow_callbacks += 1

    def render_block(self, out: np.ndarray) -> None:
        t = time.perf_counter()
        self.mixer.render(out)
        self.callback_times.add(time.perf_counter() - t)

    # -- control thread ---------------------------------------------------------------

    def wake(self) -> None:
        self._wake.set()

    def wait_for_command(self, timeout: float) -> None:
        self._wake.wait(timeout)
        self._wake.clear()

    @property
    def has_commands(self) -> bool:
        return bool(self._commands)

    def control_cycle(self) -> None:
        t = time.perf_counter()
        # Free what the audio thread let go of here, not there (and munlock it).
        self.mixer.graveyard.clear()
        self.memory.collect()
        with self._lock:
            while self._commands:
                self._apply(self._commands.popleft())
            clock = self.mixer.clock
            until = clock + self.horizon
            for sid, run in list(self._runs.items()):
                if run.bed is not None:
                    events = run.bed.plan(until, clock)
                    if events:
                        self.mixer.post("events", sid, "bed", events)
                    if run.bed.done:
                        run.bed = None
                if run.released and run.bed is None:
                    del self._runs[sid]  # the mixer drops its bus when the fade ends
            self._collect_timeline()
            self.cycles += 1
            if self.mixer.bad_messages != self._bad_logged:
                self._bad_logged = self.mixer.bad_messages
                log.error("audio thread dropped bad messages (total %d)", self._bad_logged)
        self.control_times.add(time.perf_counter() - t)

    # -- internals ----------------------------------------------------------------------

    def _command(self, cmd: tuple) -> None:
        self._commands.append(cmd)
        self._wake.set()

    @staticmethod
    def _seconds(value: float, what: str) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise EngineError(f"{what} must be a number, got {value!r}")
        if not 0 <= value < 3600:
            raise EngineError(f"{what} must be within 0..3600 s, got {value}")
        return max(float(value), MIN_FADE_S)

    def _load(self, scene: Scene) -> _LoadedScene:
        loaded = _LoadedScene(scene)
        if scene.bed is not None:
            for ref in scene.bed.clips:
                loaded.bed_clips.append(
                    load_loop_clip(
                        self.data_dir,
                        ref,
                        scene.bed.loop_crossfade_s,
                        self.rate,
                        pin=self.memory.pin,
                    )
                )
        return loaded

    def _apply(self, cmd: tuple) -> None:
        kind = cmd[0]
        at = self.mixer.clock + LEAD_BLOCKS * self.blocksize
        if kind == "scene":
            _, loaded, transition_s = cmd
            n = round(transition_s * self.rate)
            self._release_all(at, n)
            self._start(loaded, at, n)
            log.info("scene %r (transition %.1f s)", loaded.scene.name, transition_s)
        elif kind == "stop":
            n = round(cmd[1] * self.rate)
            self._release_all(at, n)
            self._current = None
            log.info("stop (fade %.1f s)", cmd[1])
        elif kind == "set":
            _, name, value = cmd
            if name == "master_gain_db":
                self._master_gain_db = value
                self.mixer.post("master", db_to_gain(value))
            elif self._current is None or self._current.scene.bed is None:
                # Not kept for later: a new scene starts at its own bed gain_db.
                what = "no scene" if self._current is None else "the scene has no bed"
                log.warning("set %s = %.1f dB ignored: %s", name, value, what)
                return
            else:
                self._current.bed_gain_db = value
                self.mixer.post("layer_gain", self._current.sid, "bed", db_to_gain(value))
            log.info("set %s = %.1f dB", name, value)

    def _release_all(self, at: int, n: int) -> None:
        for run in self._runs.values():
            if run.released:
                continue
            run.released = True
            if run.bed is not None:
                cut = run.bed.release(at, at + n)
                if cut is not None:
                    self.mixer.post("truncate", run.sid, "bed", cut)
            self.mixer.post("fade", run.sid, at, n, "out")

    def _start(self, loaded: _LoadedScene, at: int, n: int) -> None:
        scene = loaded.scene
        sid, self._next_sid = self._next_sid, self._next_sid + 1
        rng = random.Random(self._rng.getrandbits(64))
        bed = None
        layers = {}
        if scene.bed is not None:
            bed = BedPlayer(scene.bed, loaded.bed_clips, rng, at, self.rate, pin=self.memory.pin)
            layers["bed"] = db_to_gain(scene.bed.gain_db)
        run = _Run(sid, scene, at, bed, scene.bed.gain_db if scene.bed else 0.0)
        self._runs[sid] = run
        self._current = run
        self.mixer.post("add_scene", sid, at, db_to_gain(scene.gain_db), layers)
        self.mixer.post("fade", sid, at, n, "in")

    def _collect_timeline(self) -> None:
        """Log the clips that have started (planned ones may still be cancelled)."""
        clock = self.mixer.clock
        for run in self._runs.values():
            if run.bed is None:
                continue
            for entry in run.bed.history[run.logged :]:
                if entry["start"] > clock:
                    break
                item = {
                    "scene": run.scene.name,
                    "clip": entry["clip"],
                    "start_s": entry["start"] / self.rate,
                    "hold_s": None if entry["hold"] is None else entry["hold"] / self.rate,
                    "crossfade_s": entry["crossfade"] / self.rate,
                }
                self._timeline.append(item)
                hold = "endless" if item["hold_s"] is None else f"{item['hold_s']:.1f} s"
                log.info(
                    "bed %r: clip %s at %.1f s, hold %s, crossfade %.1f s",
                    item["scene"],
                    item["clip"],
                    item["start_s"],
                    hold,
                    item["crossfade_s"],
                )
                run.logged += 1

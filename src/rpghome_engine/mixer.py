"""Audio-thread side: sums prepared buffers, applies layer, scene and master gains, and
limits. It owns all of its state; the control thread talks to it only through messages
on a deque (append / popleft are atomic), so the audio thread never takes a lock.

Messages (tuples), applied at the start of the next block. A message that does not fit
(unknown kind, unknown layer, wrong shape) is counted in `bad_messages` and dropped: the
audio thread never raises on a message.
  ("add_scene", sid, start, scene_gain, {layer: gain})
  ("events", sid, layer, [(start, buffer), ...])
  ("fade", sid, start, length, "in" | "out")   equal-power; "out" starts from the current value
  ("scene_gain", sid, gain) / ("layer_gain", sid, layer, gain) / ("master", gain)
  ("truncate", sid, layer, at)   cut the layer's buffers at sample `at` (it is re-planned)
  ("cancel", sid, layer, at)     drop the layer's buffers that start at or after `at`
  ("covered", sid, layer, until) the layer is planned up to `until`, even where it has no
                                 buffers (spots are silent between events)
"""

import collections
import math

import numpy as np

from rpghome_engine.limiter import Limiter

RAMP_S = 0.05  # gain changes glide linearly over this time (no zipper noise)
_SCENE_MESSAGES = ("events", "fade", "scene_gain", "layer_gain", "truncate", "cancel", "covered")


class Ramp:
    """A linear gain that glides to its target over a fixed time."""

    def __init__(self, value: float, ramp_samples: int):
        self.value = value
        self.target = value
        self.step = 0.0
        self.ramp_samples = max(1, ramp_samples)

    def set(self, target: float) -> None:
        self.target = float(target)
        self.step = (self.target - self.value) / self.ramp_samples

    def block(self, n: int, arange: np.ndarray, out: np.ndarray) -> float | np.ndarray:
        """The gain for the next n samples: a float when constant, else out[:n]."""
        if self.value == self.target:
            return self.value
        g = out[:n]
        np.multiply(arange[1 : n + 1], self.step, out=g)
        g += self.value
        if self.step > 0:
            np.minimum(g, self.target, out=g)
        else:
            np.maximum(g, self.target, out=g)
        self.value = float(g[-1])
        return g


class Fade:
    """Scene envelope: equal-power in (0 -> 1) or out (v0 -> 0) over [start, start + length)."""

    def __init__(self, start: int, length: int, kind: str, v0: float = 1.0):
        if kind not in ("in", "out"):
            raise ValueError(f"unknown fade {kind!r}")
        self.start, self.length, self.kind, self.v0 = int(start), max(1, int(length)), kind, v0

    @property
    def end(self) -> int:
        return self.start + self.length

    def value_at(self, t: float) -> float:
        x = min(1.0, max(0.0, (t - self.start) / self.length))
        if self.kind == "in":
            return math.sin(x * math.pi / 2)
        return 0.0 if x >= 1.0 else self.v0 * math.cos(x * math.pi / 2)

    def block(self, t0: int, n: int, arange: np.ndarray, out: np.ndarray) -> float | np.ndarray:
        if t0 >= self.end or t0 + n <= self.start:
            return self.value_at(t0 + 0.5)
        x = out[:n]
        np.add(arange[:n], t0 + 0.5 - self.start, out=x)
        x /= self.length
        np.clip(x, 0.0, 1.0, out=x)
        x *= np.pi / 2
        if self.kind == "in":
            return np.sin(x, out=x)
        np.cos(x, out=x)
        x[max(0, self.end - t0) :] = 0.0  # past the end: silence, not cos(pi/2) = 6e-17
        x *= self.v0
        return x


class LayerBus:
    def __init__(self, gain: float, start: int, ramp: int):
        self.gain = Ramp(gain, ramp)
        self.events: collections.deque = collections.deque()
        self.covered = start  # end of the last buffer received


class SceneBus:
    def __init__(self, start: int, gain: float, layers: dict[str, float], ramp: int):
        self.gain = Ramp(gain, ramp)
        self.fade: Fade | None = None
        self.layers = {name: LayerBus(g, start, ramp) for name, g in layers.items()}

    def envelope_at(self, t: int) -> float:
        return 1.0 if self.fade is None else self.fade.value_at(t)

    def finished(self, t: int) -> bool:
        return self.fade is not None and self.fade.kind == "out" and t >= self.fade.end


class Meter:
    """A value per block, kept as a running extreme since the start and over the last
    `slots` slots of `slot` samples. Only the audio thread writes; readers only read, so
    any number of them can poll without disturbing each other."""

    def __init__(self, slot: int, slots: int, reset: float, better):
        self._slot, self._reset, self._better = slot, reset, better
        self._ring = np.full(slots, reset, dtype=np.float64)
        self._current = -1
        self.total = reset

    def add(self, clock: int, value: float) -> None:
        i = (clock // self._slot) % len(self._ring)
        if i != self._current:
            self._current = i
            self._ring[i] = self._reset
        self._ring[i] = self._better(self._ring[i], value)
        self.total = self._better(self.total, value)

    def recent(self) -> float:
        return float(self._ring.max() if self._better is max else self._ring.min())


class Mixer:
    METER_SLOT_S = 0.25
    METER_SLOTS = 8  # "recent" = the last 1.75-2 s

    def __init__(self, rate: int, channels: int = 2, max_block: int = 4096, pin=None):
        self._pin = pin or (lambda a: a)  # keeps buffers in RAM (memory.MemoryPin.pin)
        self.rate = rate
        self.channels = channels
        self.inbox: collections.deque = collections.deque()
        self.clock = 0  # samples rendered so far
        self.scenes: dict[int, SceneBus] = {}
        self.master = Ramp(1.0, round(RAMP_S * rate))
        self.limiter = Limiter(rate=rate, channels=channels, max_block=max_block, pin=pin)
        slot = round(self.METER_SLOT_S * rate)
        self.peak = Meter(slot, self.METER_SLOTS, 0.0, max)  # output sample peak
        self.limiter_gain = Meter(slot, self.METER_SLOTS, 1.0, min)  # lowest limiter gain
        self.late_events = 0  # buffers that arrived after their start (head skipped)
        self.starved_blocks = 0  # blocks where a layer had no material planned
        self.bad_messages = 0  # messages dropped because they did not fit
        # What the audio thread lets go of (played buffers, finished scenes, messages):
        # the control thread clears it, so buffers are freed (and munlock'ed) there.
        self.graveyard: collections.deque = collections.deque()
        self._alloc(max_block)

    def _alloc(self, n: int) -> None:
        self._max = n
        self._mix = self._pin(np.zeros((n, self.channels), dtype=np.float32))
        self._tmp = self._pin(np.zeros((n, self.channels), dtype=np.float32))
        self._abs = self._pin(np.zeros((n, self.channels), dtype=np.float32))
        self._arange = self._pin(np.arange(n + 1, dtype=np.float64))
        self._env = self._pin(np.zeros(n, dtype=np.float64))
        self._sgain = self._pin(np.zeros(n, dtype=np.float64))
        self._lgain = self._pin(np.zeros(n, dtype=np.float64))
        self._mgain = self._pin(np.zeros(n, dtype=np.float64))
        self._g64 = self._pin(np.zeros(n, dtype=np.float64))
        self._g32 = self._pin(np.zeros(n, dtype=np.float32))

    # -- messages --------------------------------------------------------------------

    def post(self, *msg) -> None:
        """Called by the control thread."""
        self.inbox.append(msg)

    def _drain(self) -> None:
        while self.inbox:
            msg = self.inbox.popleft()
            try:
                self._apply(msg)
            except Exception:
                self.bad_messages += 1
            # The message may hold the only reference to a buffer (a scene already gone,
            # a bad message): let the control thread free it.
            self.graveyard.append(msg)

    def _apply(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "add_scene":
            _, sid, start, gain, layers = msg
            self.scenes[sid] = SceneBus(start, gain, layers, round(RAMP_S * self.rate))
            return
        if kind == "master":
            self.master.set(msg[1])
            return
        sb = self.scenes.get(msg[1])
        if sb is None:
            if kind not in _SCENE_MESSAGES:
                raise ValueError(f"unknown message {kind!r}")
            return  # the scene already faded out and was dropped
        if kind == "events":
            lb = sb.layers[msg[2]]
            for start, data in msg[3]:
                if data.ndim not in (1, 2) or (data.ndim == 2 and data.shape[1] != self.channels):
                    raise ValueError("buffer of the wrong shape")
                if start < self.clock:
                    self.late_events += 1
                lb.events.append((start, data))
                lb.covered = max(lb.covered, start + len(data))
        elif kind == "fade":
            _, _, start, length, how = msg
            sb.fade = Fade(start, length, how, v0=sb.envelope_at(start))
        elif kind == "scene_gain":
            sb.gain.set(msg[2])
        elif kind == "layer_gain":
            sb.layers[msg[2]].gain.set(msg[3])
        elif kind == "truncate":
            _, _, layer, at = msg
            lb = sb.layers[layer]
            kept: collections.deque = collections.deque()
            for start, data in lb.events:
                if start < at:
                    kept.append((start, data[: at - start]))
            self.graveyard.append(lb.events)  # the cut-off buffers die in the control thread
            lb.events = kept
            lb.covered = min(lb.covered, at)
        elif kind == "cancel":
            _, _, layer, at = msg
            lb = sb.layers[layer]
            # Buffers are in start order: pop from the end, nothing is built here, and
            # the dropped buffers die in the control thread.
            while lb.events and lb.events[-1][0] >= at:
                self.graveyard.append(lb.events.pop())
        elif kind == "covered":
            lb = sb.layers[msg[2]]
            lb.covered = max(lb.covered, int(msg[3]))
        else:
            raise ValueError(f"unknown message {kind!r}")

    # -- the block ---------------------------------------------------------------------

    def _product(self, n: int, factors) -> float | np.ndarray:
        """env * scene gain * layer gain, in that order, into a scratch buffer."""
        acc = 1.0
        first = True
        g = self._g64[:n]
        for f in factors:
            if first:
                acc, first = f, False
            elif isinstance(acc, np.ndarray):
                np.multiply(acc, f, out=g)
                acc = g
            elif isinstance(f, np.ndarray):
                np.multiply(f, acc, out=g)
                acc = g
            else:
                acc = acc * f
        return acc

    def _scale(self, buf: np.ndarray, gain: float | np.ndarray) -> None:
        """buf (n, channels) *= gain, without broadcasting or mixed dtypes."""
        if not isinstance(gain, np.ndarray):
            buf *= gain
            return
        g32 = self._g32[: len(buf)]
        np.copyto(g32, gain, casting="same_kind")
        for c in range(self.channels):
            np.multiply(buf[:, c], g32, out=buf[:, c])

    def render(self, out: np.ndarray) -> None:
        """Fill out (n, channels) float32 with the next n samples."""
        n = len(out)
        if n > self._max:
            # Never regrow here (allocation, mlock): render it in pieces.
            for i in range(0, n, self._max):
                self.render(out[i : i + self._max])
            return
        self._drain()
        t0, t1 = self.clock, self.clock + n
        mix, tmp, ar = self._mix[:n], self._tmp[:n], self._arange
        mix.fill(0.0)
        starved = False
        for sid, sb in list(self.scenes.items()):
            env = 1.0 if sb.fade is None else sb.fade.block(t0, n, ar, self._env)
            sgain = sb.gain.block(n, ar, self._sgain)
            need_until = t1 if sb.fade is None or sb.fade.kind == "in" else min(t1, sb.fade.end)
            for lb in sb.layers.values():
                lgain = lb.gain.block(n, ar, self._lgain)
                if lb.covered < need_until:
                    starved = True
                if not lb.events:
                    continue
                tmp.fill(0.0)
                for start, data in lb.events:
                    if start >= t1:
                        break
                    end = start + len(data)
                    if end <= t0:
                        continue
                    a, b = max(start, t0), min(end, t1)
                    src = data[a - start : b - start]
                    dst = tmp[a - t0 : b - t0]
                    if src.ndim == 1:
                        for c in range(self.channels):
                            np.add(dst[:, c], src, out=dst[:, c])
                    else:
                        dst += src
                while lb.events and lb.events[0][0] + len(lb.events[0][1]) <= t1:
                    self.graveyard.append(lb.events.popleft())
                self._scale(tmp, self._product(n, (env, sgain, lgain)))
                mix += tmp
            if sb.finished(t1):
                self.graveyard.append(self.scenes.pop(sid))
        if starved:
            self.starved_blocks += 1
        self._scale(mix, self.master.block(n, ar, self._mgain))
        self.limiter.process(mix, out)
        absout = self._abs[:n]
        np.abs(out, out=absout)
        self.peak.add(t0, float(absout.max(initial=0.0)))
        self.limiter_gain.add(t0, self.limiter.last_min_gain)
        self.clock = t1

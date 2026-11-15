"""Planner of the ambient bed (control thread).

A voice is one clip of the pool looping endlessly, shaped by an envelope: an
equal-power fade-in (none for the first voice, the scene fade covers it), a hold, and
an equal-power fade-out into the next voice. A pool of one clip has no holds: crossfading
a clip into itself would add correlated material (up to +3 dB), so it just loops.
`plan(until)` returns the bed's samples up to `until` as ready buffers; the randomness
(clip order, holds) comes from the given random.Random, so the result does not depend
on how the time is cut into chunks.
"""

import random
from dataclasses import dataclass

import numpy as np

from rpghome.models.scene import Bed
from rpghome_engine.clips import LoopClip, equal_power_in, equal_power_out


@dataclass
class Voice:
    clip: int  # index into the pool
    start: int  # sample of the fade-in start (engine clock)
    fade_in: int
    hold: int | None  # start-to-start until the next voice; None: loops forever
    fade_out_at: int | None = None
    fade_out: int = 0

    @property
    def end(self) -> float:
        return float("inf") if self.fade_out_at is None else self.fade_out_at + self.fade_out


class BedPlayer:
    def __init__(
        self,
        bed: Bed,
        clips: list[LoopClip],
        rng: random.Random,
        start: int,
        rate: int,
        pin=None,
    ):
        self._pin = pin or (lambda a: a)  # the audio thread reads these: keep them in RAM
        self.bed = bed
        self.clips = clips
        self.rng = rng
        self.rate = rate
        self.crossfade = round(bed.crossfade_s * rate)
        self.planned_until = start
        self.end: int | None = None  # set by release()
        self.single = len({c.path for c in clips}) == 1
        self.history: list[dict] = []  # one entry per voice, for status and tests
        self.voices: list[Voice] = []
        self._start_voice(start, fade_in=0, previous=None)

    @property
    def stereo(self) -> bool:
        return any(c.stereo for c in self.clips)

    def _draw_hold(self) -> int | None:
        if self.single:
            return None
        lo, hi = self.bed.hold_s
        return round(self.rng.uniform(lo, hi) * self.rate)

    def _pick(self, previous: int | None) -> int:
        """Any clip of the pool, never the one just played (compared by path)."""
        choices = list(range(len(self.clips)))
        if previous is not None:
            others = [i for i in choices if self.clips[i].path != self.clips[previous].path]
            choices = others or choices
        return self.rng.choice(choices)

    def _start_voice(self, at: int, fade_in: int, previous: int | None) -> None:
        voice = Voice(clip=self._pick(previous), start=at, fade_in=fade_in, hold=self._draw_hold())
        self.voices.append(voice)
        self.history.append(
            {
                "clip": self.clips[voice.clip].path,
                "start": at,
                "hold": voice.hold,
                "crossfade": fade_in,
            }
        )

    def release(self, at: int, end: int) -> int | None:
        """Stop planning new material from sample `at`: no clip change starts at or after
        it; the clips playing at `at` keep looping until `end` (the end of the scene's
        fade-out). Material planned past `at` is planned again, so this returns `at`
        when the buffers already handed out must be cut there (else None)."""
        if self.end is not None:
            self.end = min(self.end, end)
            return None
        self.end = end
        if self.planned_until <= at:
            return None
        self.voices = [v for v in self.voices if v.start < at]
        for v in self.voices:
            if v.fade_out_at is not None and v.fade_out_at >= at:
                v.fade_out_at, v.fade_out = None, 0
        self.history = [h for h in self.history if h["start"] < at]
        self.planned_until = at
        return at

    @property
    def done(self) -> bool:
        return self.end is not None and self.planned_until >= self.end

    def current(self, clock: int) -> dict:
        """History entry of the latest clip that has started by `clock` (the first one
        before the scene starts)."""
        for entry in reversed(self.history):
            if entry["start"] <= clock:
                return entry
        return self.history[0]

    def plan(self, until: int, clock: int) -> list[tuple[int, np.ndarray]]:
        """Material for [planned_until, until); voices that ended before `clock` are
        forgotten (later ones may be needed again after a release)."""
        if self.end is not None:
            until = min(until, self.end)
        a = self.planned_until
        if until <= a:
            return []
        if self.end is None and not self.single:
            # Schedule every clip change that starts before `until`.
            while True:
                last = self.voices[-1]
                switch = last.start + last.hold
                if switch >= until:
                    break
                last.fade_out_at, last.fade_out = switch, self.crossfade
                self._start_voice(switch, fade_in=self.crossfade, previous=last.clip)
        n = until - a
        shape = (n, 2) if self.stereo else (n,)
        out = self._pin(np.zeros(shape, dtype=np.float32))
        for v in self.voices:
            if v.start >= until or v.end <= a:
                continue
            lo, hi = max(a, v.start), min(until, v.end)
            hi = int(hi)
            seg = self.clips[v.clip].render(lo - v.start, hi - lo)
            env = self._envelope(v, lo, hi)
            if env is not None:
                seg *= env[:, None] if seg.ndim == 2 else env
            if seg.ndim == 1 and out.ndim == 2:
                seg = seg[:, None]
            out[lo - a : hi - a] += seg
        self.voices = [v for v in self.voices if v.end > clock]
        self.planned_until = until
        return [(a, out)]

    @staticmethod
    def _envelope(v: Voice, lo: int, hi: int) -> np.ndarray | None:
        in_fade = v.fade_in and lo < v.start + v.fade_in
        out_fade = v.fade_out_at is not None and hi > v.fade_out_at
        if not in_fade and not out_fade:
            return None
        t = np.arange(lo, hi) + 0.5
        env = np.ones(hi - lo, dtype=np.float32)
        if in_fade:
            env *= equal_power_in((t - v.start) / v.fade_in)
        if out_fade:
            env *= equal_power_out((t - v.fade_out_at) / max(v.fade_out, 1))
        return env

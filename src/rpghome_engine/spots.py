"""Spot layers (control thread): random one-shot sounds over the bed.

When a scene is set (in the caller's thread) each layer's clips are read, mixed to mono
(a spot is a point source, panned per event), and either cut into single drops (`slice`)
or trimmed of their silence; the scene's reverb impulse response is generated once.
While playing, `SpotsPlayer.plan(until)` draws the events up to `until` and renders each
one into a ready stereo buffer: gain, pitch shift, distance (level, low-pass, more wet),
equal-power pan and the reverb tail. The audio thread only sums these buffers.

Distance: a clip sounds from its perspective (close, medium, far = 1, 5, 20 m, or
metres; unknown = close). A layer's distance_m only moves clips further away; with
r = requested / clip distance (at least 1):
  direct level   -20 log10(r) dB, inverse distance: -6 dB per doubling
  low-pass       2nd-order Butterworth magnitude, cutoff 20 kHz / r^0.8 (>= 500 Hz),
                 none at r = 1: 11.5 kHz at r = 2, 5.5 kHz at 5, 1.8 kHz at 20
  wet / direct   the scene's wet_db + 10 log10(r) dB: +3 dB per doubling (the reverb
                 itself falls by 3 dB per doubling, the direct sound by 6)
"""

import collections
import logging
import math
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from rpghome.models.scene import ClipRef, Reverb, Spots
from rpghome_engine.clips import EngineError, db_to_gain, read_clip

log = logging.getLogger("rpghome.engine")

LOWPASS_TOP_HZ = 20000.0
LOWPASS_EXPONENT = 0.8
LOWPASS_FLOOR_HZ = 500.0
WET_DB_PER_DECADE = 10.0  # wet/direct rises 10 log10(r) dB: +3 dB per doubling
IR_LOWPASS_HZ = 2946.0  # the demo's one-pole (0.68 at 48 kHz)
IR_PREDELAY_S = 0.012
MIN_CLIP_S = 0.01  # shorter spot clips are an error when the scene is set
MIN_FFT = 64
# The 2nd-order low-pass's impulse response is below -120 dB of its peak after
# about 2 / fc seconds on each side; the FFT is padded by twice that, so neither side
# of the zero-phase ringing wraps around into the output.
LOWPASS_PAD_PERIODS = 4.0
SPECTRA_CACHE = 3  # reverb spectra kept, one per FFT size (complex64, the latest used)
LOWPASS_CACHE = 4  # low-pass responses kept, one per (FFT size, cutoff)


# -- preparing the sounds (when the scene is set) ------------------------------------


def _envelope_db(x: np.ndarray, hop: int) -> np.ndarray:
    n = len(x) // hop * hop
    rms = np.sqrt((x[:n].astype(np.float64).reshape(-1, hop) ** 2).mean(axis=1))
    return 20 * np.log10(rms + 1e-9)


def _fades(x: np.ndarray, rate: int, fin_s: float = 0.005, fout_s: float = 0.25) -> np.ndarray:
    x = x.astype(np.float32)  # a copy
    a, b = min(int(fin_s * rate), len(x) // 2), min(int(fout_s * rate), len(x) // 2)
    if a:
        x[:a] *= np.linspace(0, 1, a, dtype=np.float32)
    if b:
        x[-b:] *= np.linspace(1, 0, b, dtype=np.float32)
    return x


def slice_drops(x: np.ndarray, rate: int, max_len_s: float = 2.5) -> list[np.ndarray]:
    """Single drops of a mono clip (as the sound-gen spots demo): onsets where the 10 ms
    envelope rises to at least 20 dB over the clip's floor (20th percentile), at least
    0.4 s apart, at most 12; each slice runs to the next onset (at most max_len_s), is
    kept if longer than 0.3 s, with a 5 ms fade-in and a 250 ms fade-out."""
    hop = rate // 100
    env = _envelope_db(x, hop)
    if len(env) < 2:
        return []
    floor = np.percentile(env, 20)
    rise = env[1:] - env[:-1]
    peaks: list[int] = []
    for i in np.argsort(-rise, kind="stable"):
        if env[i + 1] > floor + 20 and all(abs(i - p) > 40 for p in peaks):
            peaks.append(int(i))
        if len(peaks) >= 12:
            break
    peaks.sort()
    out = []
    for k, p in enumerate(peaks):
        start = max(0, (p - 3) * hop)
        end = min(len(x), start + int(max_len_s * rate))
        if k + 1 < len(peaks):
            end = min(end, (peaks[k + 1] - 3) * hop)
        if end - start > 0.3 * rate:
            out.append(_fades(x[start:end], rate))
    return out


def trim(x: np.ndarray, rate: int, below_db: float = 40.0) -> np.ndarray:
    """The clip without its leading and trailing silence (more than below_db under its
    loudest 10 ms), with the same fades as a slice."""
    hop = rate // 100
    env = _envelope_db(x, hop)
    if not len(env):
        return _fades(x, rate)
    loud = np.flatnonzero(env > env.max() - below_db)
    return _fades(x[max(0, (loud[0] - 2) * hop) : min(len(x), (loud[-1] + 10) * hop)], rate)


@dataclass(frozen=True)
class SpotSound:
    """One sound of a layer's pool: a whole (trimmed) clip or one slice of it."""

    clip: ClipRef
    index: int | None  # slice number, None for a whole clip
    source: str  # the clip's source, other when it has none
    weight: float  # of the source
    data: np.ndarray  # mono float32 at the engine rate, clip gain applied

    @property
    def key(self) -> str:
        return self.clip.path if self.index is None else f"{self.clip.path}#{self.index}"


def load_spot_sounds(data_dir: Path, spots: Spots, rate: int) -> list[SpotSound]:
    per_clip = []
    for ref in spots.clips:
        path = data_dir / ref.path
        if not path.is_file():
            raise EngineError(f"clip not found: {ref.path} (in {data_dir})")
        x = read_clip(path, rate)
        if len(x) < MIN_CLIP_S * rate:
            raise EngineError(
                f"spots {spots.name!r}: clip {ref.path} is {len(x) / rate * 1e3:.1f} ms, "
                f"too short for a spot (at least {MIN_CLIP_S * 1e3:g} ms)"
            )
        if x.ndim == 2:
            x = x.mean(axis=1)
        x = x * np.float32(db_to_gain(ref.gain_db))
        if spots.slice:
            parts = slice_drops(x, rate)
            if not parts:
                log.warning("spots %r: no single drops found in %s, skipped", spots.name, ref.path)
            per_clip.append([(ref, i, p) for i, p in enumerate(parts)])
        else:
            per_clip.append([(ref, None, trim(x, rate))])
    if not any(per_clip):
        raise EngineError(f"spots {spots.name!r}: no single drops found in any clip")
    if not any(spots.weight(parts[0][0]) > 0 for parts in per_clip if parts):
        raise EngineError(f"spots {spots.name!r}: only clips of source weight 0 have sounds")
    sounds = []
    for parts in per_clip:
        for ref, i, data in parts:
            data = np.ascontiguousarray(data, dtype=np.float32)
            sounds.append(SpotSound(ref, i, ref.source or "other", spots.weight(ref), data))
    return sounds


def synthetic_ir(reverb: Reverb, rate: int) -> np.ndarray:
    """Stereo impulse response, shape (2, n), as the demo's cave_ir: noise decaying by
    60 dB over decay_s, darkened by a one-pole low-pass (~3 kHz), a 12 ms pre-delay,
    each channel normalised to unit energy."""
    rng = np.random.default_rng(reverb.seed)
    n = round(reverb.decay_s * rate)
    t = np.arange(n) / rate
    ir = rng.standard_normal((2, n)) * 10 ** (-3 * t / reverb.decay_s)
    # One-pole y[i] = a y[i-1] + (1 - a) x[i], applied in the frequency domain; the padding
    # holds its tail (a^256 is below 1e-40), so nothing wraps around.
    a = math.exp(-2 * math.pi * IR_LOWPASS_HZ / rate)
    m = _fft_size(n + 256)
    w = np.exp(-2j * np.pi * np.arange(m // 2 + 1) / m)
    ir = np.fft.irfft(np.fft.rfft(ir, m, axis=1) * ((1 - a) / (1 - a * w)), m, axis=1)[:, :n]
    ir[:, : round(IR_PREDELAY_S * rate)] = 0
    return ir / np.sqrt((ir**2).sum(axis=1, keepdims=True))


def _fft_size(n: int) -> int:
    """The smallest 2^k, 3 * 2^k or 5 * 2^k that is >= n and >= MIN_FFT (fast sizes for
    numpy's FFT)."""
    n = max(n, MIN_FFT)
    best = 1 << max(0, (n - 1).bit_length())
    for f in (3, 5):
        k = max(0, math.ceil(math.log2(n / f)))
        while f << k < n:
            k += 1
        best = min(best, f << k)
    return best


# -- distance ------------------------------------------------------------------------


@dataclass(frozen=True)
class Distance:
    clip_m: float  # the recording's own distance
    distance_m: float  # where it is heard (never closer than clip_m)
    attenuation_db: float  # of the direct sound
    lowpass_hz: float | None  # None: no filter
    wet_extra_db: float  # added to the scene's wet/direct ratio


def distance(clip_m: float, requested_m: float | None) -> Distance:
    d = clip_m if requested_m is None else max(clip_m, requested_m)
    r = d / clip_m
    if r <= 1.0:
        return Distance(clip_m, clip_m, 0.0, None, 0.0)
    cutoff = max(LOWPASS_FLOOR_HZ, LOWPASS_TOP_HZ / r**LOWPASS_EXPONENT)
    return Distance(clip_m, d, -20 * math.log10(r), cutoff, WET_DB_PER_DECADE * math.log10(r))


# -- the player (control thread) -----------------------------------------------------


class SpotsPlayer:
    """Plans one spot layer. Randomness comes from the given random.Random only, drawn
    per event in a fixed order, so a render does not depend on how time is cut into
    planning cycles. density divides the intervals drawn after it is set."""

    def __init__(
        self,
        spots: Spots,
        sounds: list[SpotSound],
        room: "Room",
        rng: random.Random,
        start: int,
        rate: int,
    ):
        self.spots = spots
        self.sounds = sounds
        self.room = room
        self.rng = rng
        self.rate = rate
        self.density = 1.0
        self.planned_until = start
        self.released = False
        self.history: collections.deque = collections.deque()  # planned, not yet logged
        self._last_start = start
        self._next_raw_s = rng.uniform(*spots.interval_s)  # before density
        self._previous: int | None = None
        weights = {sd.source: sd.weight for sd in sounds if sd.weight > 0}
        self._shares = {src: w / sum(weights.values()) for src, w in weights.items()}
        self._credit = dict.fromkeys(self._shares, 0.0)
        self._distances = [distance(s.clip.distance_m, spots.distance_m) for s in sounds]

    def release(self, at: int) -> bool:
        """No event starts at or after `at`; returns True when some already handed out
        did (the mixer must cancel them)."""
        self.released = True
        cancelled = self.planned_until > at and any(e["start"] >= at for e in self.history)
        self.history = collections.deque(e for e in self.history if e["start"] < at)
        return cancelled

    def plan(self, until: int) -> list[tuple[int, np.ndarray]]:
        """Events starting in [planned_until, until), each as (start, stereo buffer)."""
        out = []
        while not self.released:
            interval = round(self._next_raw_s / self.density * self.rate)
            # A higher density never moves the next event before what is already planned.
            t = max(self._last_start + interval, self.planned_until)
            if t >= until:
                break
            out.append((t, self._event(t)))
            self._last_start = t
            lo, hi = self.spots.interval_s
            self._next_raw_s = min(
                hi, max(lo, self.rng.expovariate(1 / self.spots.mean_interval_s))
            )
        self.planned_until = max(self.planned_until, until)
        return out

    def _pick(self) -> int:
        """A source, then one of its sounds; never the sound just played (when the pool
        has another one).

        Each source has its share of the weights. A source whose only sound was just
        played cannot come next; its share is then credited and added to its chance at
        the next pick where it can come (taken from the sources with no credit), so the
        long-run ratio stays at the weights: 3:1 for MOSS a, b and Epidemic c at 3:1,
        where plain renormalisation gives 4:1.
        """
        groups: dict[str, list[int]] = {}
        for i, sound in enumerate(self.sounds):
            if i != self._previous and sound.weight > 0:
                groups.setdefault(sound.source, []).append(i)
        if not groups:
            return self._previous
        for source, share in self._shares.items():
            if source not in groups:
                self._credit[source] += share
        sources = sorted(groups)
        owed = [s for s in sources if self._credit[s] > 0]
        free = [s for s in sources if self._credit[s] <= 0]
        probs = dict.fromkeys(sources, 0.0)
        for s in owed:
            probs[s] = self._shares[s] + self._credit[s]
        # The rest goes to the sources with no credit, by weight (when nothing is owed,
        # that is plain renormalisation over the sources that can come).
        rest = 1.0 - sum(probs.values())
        if free and rest > 0:
            free_total = sum(self._shares[s] for s in free)
            for s in free:
                probs[s] = self._shares[s] * rest / free_total
        for s in sources:
            self._credit[s] = 0.0
        source = self.rng.choices(sources, [probs[s] for s in sources])[0]
        return self.rng.choice(groups[source])

    def _event(self, t: int) -> np.ndarray:
        sp = self.spots
        i = self._pick()
        self._previous = i
        gain_db = self.rng.uniform(*sp.gain_db)
        semitones = self.rng.uniform(-sp.pitch_semitones, sp.pitch_semitones)
        pan = self.rng.uniform(-sp.pan, sp.pan)
        sound, dist = self.sounds[i], self._distances[i]
        wet_db = None if self.room.ir is None else self.room.wet_db + dist.wet_extra_db
        self.history.append(
            {
                "start": t,
                "layer": sp.name,
                "clip": sound.key,
                "source": sound.clip.source,
                "gain_db": round(gain_db, 3),
                "pan": round(pan, 4),
                "pitch_semitones": round(semitones, 4),
                "clip_distance_m": dist.clip_m,
                "distance_m": dist.distance_m,
                "attenuation_db": round(dist.attenuation_db, 3),
                "lowpass_hz": None if dist.lowpass_hz is None else round(dist.lowpass_hz, 1),
                "wet_db": None if wet_db is None else round(wet_db, 3),
            }
        )
        return self.room.render(
            sound.data, gain_db + dist.attenuation_db, semitones, pan, dist.lowpass_hz, wet_db
        )


class Room:
    """The scene's acoustics shared by its spot layers: the reverb impulse response (None:
    spots play dry) and the FFT-size caches of its spectrum and of the low-pass filters.
    pin(array) keeps each event buffer in RAM (memory.MemoryPin.pin), as the bed's
    planned buffers: the audio thread reads them. The sounds themselves are read only
    here, in the control thread, so they are not pinned."""

    def __init__(self, reverb: Reverb | None, rate: int, pin=None):
        self._pin = pin or (lambda a: a)
        self.rate = rate
        self.ir = None if reverb is None else synthetic_ir(reverb, rate)
        self.wet_db = 0.0 if reverb is None else reverb.wet_db
        self._ir_spectra: collections.OrderedDict = collections.OrderedDict()
        self._lowpass: collections.OrderedDict = collections.OrderedDict()

    @property
    def cache_bytes(self) -> int:
        caches = (*self._ir_spectra.values(), *self._lowpass.values())
        return sum(a.nbytes for a in caches)

    def render(
        self,
        x: np.ndarray,
        gain_db: float,
        semitones: float,
        pan: float,
        lowpass_hz: float | None,
        wet_db: float | None,
    ) -> np.ndarray:
        """One event: mono x at gain_db, pitch-shifted, low-passed, panned (equal power),
        plus the wet tail at wet_db under the direct sound; shape (n, 2) float32."""
        if semitones:
            # Resampling: higher is shorter. Linear interpolation is enough for a few
            # semitones of drops and stones.
            pos = np.arange(0, len(x) - 1, 2 ** (semitones / 12))
            x = np.interp(pos, np.arange(len(x)), x)
        g = db_to_gain(gain_db)
        angle = (pan + 1) * math.pi / 4
        left, right = g * math.cos(angle), g * math.sin(angle)
        if lowpass_hz is None and wet_db is None:
            y = np.ascontiguousarray(np.stack([x * left, x * right], axis=1), dtype=np.float32)
            return self._pin(y)
        tail = 0 if self.ir is None else self.ir.shape[1]
        n_out = len(x) + tail
        pad = 0 if lowpass_hz is None else math.ceil(LOWPASS_PAD_PERIODS * self.rate / lowpass_hz)
        n = _fft_size(n_out + pad)
        spec = np.fft.rfft(x, n)
        if lowpass_hz is not None:
            spec *= self._lowpass_response(n, lowpass_hz)
        if wet_db is None:
            ys = (spec * left, spec * right)
        else:
            # The reverb is diffuse: the mono source into both IR channels, each at
            # 1/sqrt(2), so the wet energy is wet_db under the direct energy.
            h = self._ir_spectrum(n) * (g * db_to_gain(wet_db) / math.sqrt(2))
            ys = (spec * (h[0] + left), spec * (h[1] + right))
        y = np.empty((n_out, 2), dtype=np.float32)
        for c, yc in enumerate(ys):
            y[:, c] = np.fft.irfft(yc, n)[:n_out]
        return self._pin(y)

    def _ir_spectrum(self, n: int) -> np.ndarray:
        return _cached(
            self._ir_spectra,
            n,
            SPECTRA_CACHE,
            lambda: np.fft.rfft(self.ir, n, axis=1).astype(np.complex64),
        )

    def _lowpass_response(self, n: int, cutoff: float) -> np.ndarray:
        """Zero-phase 2nd-order Butterworth magnitude, 1 / sqrt(1 + (f / fc)^4)."""

        def make():
            f = np.fft.rfftfreq(n, 1 / self.rate)
            return (1 / np.sqrt(1 + (f / cutoff) ** 4)).astype(np.float32)

        return _cached(self._lowpass, (n, cutoff), LOWPASS_CACHE, make)


def _cached(cache: collections.OrderedDict, key, size: int, make) -> np.ndarray:
    """A small LRU: the value for key, made and kept if missing; the oldest is dropped."""
    value = cache.get(key)
    if value is None:
        value = cache[key] = make()
        while len(cache) > size:
            cache.popitem(last=False)
    else:
        cache.move_to_end(key)
    return value

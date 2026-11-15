"""Clips in RAM: read with soundfile, resampled to the engine rate, gain applied, and
prepared as a seamless loop (version C of the sound-gen loop demo)."""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from rpghome.core.errors import RpgHomeError
from rpghome.models.scene import ClipRef


class EngineError(RpgHomeError):
    """A command the engine cannot carry out (the engine keeps playing what it played)."""


def db_to_gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def equal_power_in(x: np.ndarray) -> np.ndarray:
    """Fade-in curve for x in 0..1 (sin); its square plus fade-out's square is 1."""
    return np.sin(np.clip(x, 0.0, 1.0) * (np.pi / 2))


def equal_power_out(x: np.ndarray) -> np.ndarray:
    return np.cos(np.clip(x, 0.0, 1.0) * (np.pi / 2))


def resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Band-limited resampling in the frequency domain (whole clip at once, at load time).

    The clip is padded with its own reflection first, so the circular FFT does not
    smear the end into the start.
    """
    if src_rate == dst_rate:
        return x
    n = len(x)
    pad = min(n - 1, src_rate // 10) if n > 1 else 0
    padded = np.concatenate([x[pad:0:-1], x, x[-2 : -pad - 2 : -1]]) if pad else x
    m = len(padded)
    m_out = round(m * dst_rate / src_rate)
    spec = np.fft.rfft(padded, axis=0)
    keep = min(len(spec), m_out // 2 + 1)
    out_spec = np.zeros((m_out // 2 + 1, *spec.shape[1:]), dtype=spec.dtype)
    out_spec[:keep] = spec[:keep]
    y = np.fft.irfft(out_spec, n=m_out, axis=0) * (m_out / m)
    start = round(pad * dst_rate / src_rate)
    return y[start : start + round(n * dst_rate / src_rate)].astype(np.float32)


def read_clip(path: Path, rate: int) -> np.ndarray:
    """float32 samples at `rate`: shape (n,) for mono, (n, 2) for stereo; more channels are
    mixed down to mono."""
    try:
        data, src_rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as exc:  # soundfile raises several types (LibsndfileError, OSError...)
        raise EngineError(f"cannot read clip {path}: {exc}") from None
    if data.shape[0] == 0:
        raise EngineError(f"clip {path} has no samples")
    bad = int(np.count_nonzero(~np.isfinite(data)))
    if bad:  # checked before resampling, which would smear one inf over the whole clip
        raise EngineError(f"clip {path} has {bad} non-finite samples (NaN or inf)")
    if data.shape[1] == 1:
        data = data[:, 0]
    elif data.shape[1] > 2:
        data = data.mean(axis=1)
    data = resample(data, src_rate, rate)
    return np.ascontiguousarray(data, dtype=np.float32)


@dataclass(frozen=True)
class LoopClip:
    """A clip ready for endless looping with an equal-power crossfade of `fade` samples.

    The first pass plays the clip from its start (`head` holds the first `fade` samples);
    after that the signal is periodic with `period` = len(clip) - fade, and one period is
    `cycle`: the head fading in (sin) under the tail fading out (cos), then the rest.
    Gain is already applied.
    """

    path: str
    head: np.ndarray
    cycle: np.ndarray
    fade: int

    @property
    def period(self) -> int:
        return len(self.cycle)

    @property
    def stereo(self) -> bool:
        return self.cycle.ndim == 2

    def render(self, t0: int, n: int) -> np.ndarray:
        """Samples t0..t0+n of the endless loop (t = 0 is the start of the first pass)."""
        p = self.period
        idx = np.arange(t0, t0 + n) % p
        out = self.cycle[idx]
        if t0 < self.fade:
            k = min(n, self.fade - t0)
            out[:k] = self.head[t0 : t0 + k]
        return out


def load_loop_clip(
    data_dir: Path, ref: ClipRef, loop_fade_s: float, rate: int, pin=None
) -> LoopClip:
    """pin(array) keeps a buffer in RAM (memory.MemoryPin.pin)."""
    pin = pin or (lambda a: a)
    path = data_dir / ref.path
    if not path.is_file():
        raise EngineError(f"clip not found: {ref.path} (in {data_dir})")
    x = read_clip(path, rate) * np.float32(db_to_gain(ref.gain_db))
    fade = round(loop_fade_s * rate)
    if 2 * fade > len(x):
        raise EngineError(
            f"clip {ref.path} is {len(x) / rate:.2f} s, too short for a "
            f"{loop_fade_s:g} s loop crossfade (needs at least twice that)"
        )
    period = len(x) - fade
    cycle = x[:period].copy()
    if fade:
        ramp = (np.arange(fade) + 0.5) / fade
        fin, fout = equal_power_in(ramp), equal_power_out(ramp)
        if x.ndim == 2:
            fin, fout = fin[:, None], fout[:, None]
        cycle[:fade] = x[:fade] * fin + x[period:] * fout
    cycle = pin(np.ascontiguousarray(cycle, dtype=np.float32))
    return LoopClip(path=ref.path, head=pin(x[:fade].copy()), cycle=cycle, fade=fade)

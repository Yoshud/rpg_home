"""The previous limiter (per-block concatenates and a Python release loop), kept as a
reference: the allocation-free Limiter must give the same output within a tiny tolerance."""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


class Limiter:
    def __init__(
        self,
        ceiling_db: float = -1.0,
        rate: int = 48000,
        lookahead_s: float = 0.0025,
        release_s: float = 0.15,
        channels: int = 2,
        chunk: int = 16,
    ):
        self.ceiling = float(10 ** (ceiling_db / 20))
        self.window = max(2, round(lookahead_s * rate))
        self.delay = self.window - 1
        self.chunk = chunk
        # Per chunk, the gain recovers by this fraction of the way to 1 (one-pole release).
        self.release_coef = 1.0 - np.exp(-chunk / (release_s * rate))
        w = self.window
        self._x_hist = np.zeros((w - 1, channels), dtype=np.float32)
        self._need_hist = np.ones(w - 1, dtype=np.float32)
        self._held_hist = np.ones(w - 1, dtype=np.float32)
        self._held_last = 1.0
        self._idle = True  # all history gains are 1: the limiter is a delay
        self.min_gain = 1.0  # lowest gain applied since the caller last reset it

    def process(self, x: np.ndarray, out: np.ndarray) -> None:
        """Limit block x (n, channels) into out (same shape); output is delayed."""
        n = len(x)
        w1 = self.window - 1
        peak = np.abs(x).max(axis=1)
        if self._idle and float(peak.max(initial=0.0)) <= self.ceiling:
            self._delay_only(x, out)
            return
        need = np.minimum(1.0, self.ceiling / np.maximum(peak, 1e-12)).astype(np.float32)
        need_all = np.concatenate([self._need_hist, need])
        # Trailing min over the window: m[i] = min(need over the last `window` samples).
        m = sliding_window_view(need_all, self.window).min(axis=1)
        held = self._release(m)
        held_all = np.concatenate([self._held_hist, held])
        c = np.concatenate([[0.0], np.cumsum(held_all, dtype=np.float64)])
        gain = ((c[self.window :] - c[: -self.window]) / self.window).astype(np.float32)
        x_all = np.concatenate([self._x_hist, x])
        np.multiply(x_all[:n], gain[:, None], out=out)
        np.clip(out, -self.ceiling, self.ceiling, out=out)
        self.min_gain = min(self.min_gain, float(gain.min()))
        self._x_hist = x_all[n:].copy()
        self._need_hist = need_all[n:].copy()
        self._held_hist = held_all[len(held_all) - w1 :].copy()
        self._idle = bool(self._held_last >= 1.0 and self._held_hist.min() >= 1.0)
        if self._idle:
            self._need_hist.fill(1.0)

    def _delay_only(self, x: np.ndarray, out: np.ndarray) -> None:
        n, d = len(x), self.delay
        if n >= d:
            out[:d] = self._x_hist
            out[d:] = x[: n - d]
            self._x_hist[:] = x[n - d :]
        else:
            x_all = np.concatenate([self._x_hist, x])
            out[:] = x_all[:n]
            self._x_hist = x_all[n:].copy()
        # Keep the needed-gain history right for a peak in the next block.
        self._need_hist.fill(1.0)

    def _release(self, m: np.ndarray) -> np.ndarray:
        """Per chunk of samples: never above m, and recovering towards 1 at most at the
        release rate. Staying at or below m keeps the ceiling guarantee."""
        n = len(m)
        k = self.chunk
        held = np.empty_like(m)
        g = self._held_last
        a = self.release_coef
        for i in range(0, n, k):
            seg = m[i : i + k]
            g = min(float(seg.min()), g + (1.0 - g) * a)
            if g > 0.99999:
                g = 1.0
            np.minimum(seg, g, out=held[i : i + k])
        self._held_last = g
        return held

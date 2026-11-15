"""Lookahead peak limiter (audio thread). No sample leaves above the ceiling.

For every sample the needed gain is g = min(1, ceiling / peak). The applied gain is
that, min-filtered over the lookahead window, held with a release, then smoothed by a
moving average of the same window; the signal is delayed by window - 1 samples. Every
value under the moving average is at most the needed gain of the sample it lands on,
so the average is too: the ceiling holds by construction (a final clamp to the float32
ceiling, rounded down, only guards against rounding). When nothing is near the ceiling
the limiter is a plain delay.

Why our own: neither numpy nor scipy has a limiter. pedalboard has one, but it is GPLv3,
and it is JUCE's limiter: two compressors and a hard clip at 0 dBFS, with no lookahead,
so it cannot guarantee what this one does, that no sample goes above -1 dBFS.

All work buffers are allocated up front (again only for a block larger than any
before). Each block uses `out=` on contiguous or same-dtype slices only: broadcasting
and mixed-dtype ufuncs make numpy allocate buffers, so they are avoided.
"""

import math

import numpy as np


def float32_at_most(value: float) -> np.float32:
    """The largest float32 that is <= value."""
    c = np.float32(value)
    while float(c) > value:
        c = np.nextafter(c, np.float32(0))
    return c


class Limiter:
    def __init__(
        self,
        ceiling_db: float = -1.0,
        rate: int = 48000,
        lookahead_s: float = 0.0025,
        release_s: float = 0.15,
        channels: int = 2,
        chunk: int = 16,
        max_block: int = 4096,
        pin=None,
    ):
        self._pin = pin or (lambda a: a)  # keeps buffers in RAM (memory.MemoryPin.pin)
        self.ceiling = float32_at_most(10 ** (ceiling_db / 20))
        self._neg_ceiling = -self.ceiling
        self.window = max(2, round(lookahead_s * rate))
        self.delay = self.window - 1
        self.chunk = chunk
        self.channels = channels
        # Per chunk, 1 - gain decays by this factor (one-pole release towards gain 1).
        self._q = float(np.exp(-chunk / (release_s * rate)))
        self._held_last = 1.0
        self._idle = True  # all history gains are 1: the limiter is a delay
        self.last_min_gain = 1.0  # lowest gain applied in the last block
        self.min_gain = 1.0  # lowest gain applied since the start
        self.nonfinite_blocks = 0  # blocks that had NaN or inf samples (silenced)
        self._alloc(max_block)

    def _alloc(self, n: int) -> None:
        """Work buffers for blocks of up to n samples (called once, from __init__)."""
        w1, k, ch = self.window - 1, self.chunk, self.channels
        chunks = -(-n // k)
        self._max = n
        self._xw = self._pin(np.zeros((w1 + n, ch), dtype=np.float32))  # history, block
        self._need = self._pin(np.ones(w1 + n, dtype=np.float32))
        self._held = self._pin(np.ones(w1 + chunks * k, dtype=np.float32))  # padded tail
        self._held64 = self._pin(np.empty(w1 + n, dtype=np.float64))
        self._csum = self._pin(np.zeros(w1 + n + 1, dtype=np.float64))
        self._abs = self._pin(np.empty((n, ch), dtype=np.float32))
        self._peak = self._pin(np.empty(n, dtype=np.float32))
        self._ping = self._pin(np.empty(w1 + chunks * k, dtype=np.float32))
        self._pong = self._pin(np.empty(w1 + chunks * k, dtype=np.float32))
        self._m = self._pin(np.full(chunks * k, np.inf, dtype=np.float32))
        self._gs = self._pin(np.empty(chunks * k, dtype=np.float32))
        self._cmin = self._pin(np.empty(chunks, dtype=np.float32))
        self._r = self._pin(np.empty(chunks, dtype=np.float64))
        self._mask = self._pin(np.empty(chunks, dtype=bool))
        self._gch = self._pin(np.empty(chunks, dtype=np.float32))
        self._g64 = self._pin(np.empty(n, dtype=np.float64))
        self._g32 = self._pin(np.empty(n, dtype=np.float32))
        j = np.arange(chunks, dtype=np.float64)
        self._qpow = self._pin(self._q**j)
        self._qinv = self._pin(self._q**-j)

    def process(self, x: np.ndarray, out: np.ndarray) -> None:
        """Limit block x (n, channels) into out (same shape); output is delayed."""
        n = len(x)
        if n > self._max:
            # Never regrow in the audio thread (allocation, mlock): limit it in pieces.
            for i in range(0, n, self._max):
                self.process(x[i : i + self._max], out[i : i + self._max])
            return
        w, w1 = self.window, self.window - 1
        xw = self._xw
        block = xw[w1 : w1 + n]
        block[:] = x
        absx, peak = self._abs[:n], self._peak[:n]
        np.abs(block, out=absx)
        peak[:] = absx[:, 0]
        for c in range(1, self.channels):
            np.maximum(peak, absx[:, c], out=peak)
        top = float(peak.max(initial=0.0))
        if not math.isfinite(top):
            # NaN or inf (max propagates both): silence those samples before the detector,
            # so they never reach the gain state. Rare, so this path may allocate.
            self.nonfinite_blocks += 1
            np.nan_to_num(block, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
            np.abs(block, out=absx)
            peak[:] = absx[:, 0]
            for c in range(1, self.channels):
                np.maximum(peak, absx[:, c], out=peak)
            top = float(peak.max(initial=0.0))
        if self._idle and top <= self.ceiling:
            out[:] = xw[:n]
            self.last_min_gain = 1.0
            xw[:w1] = xw[n : n + w1]  # need and held histories stay all ones
            return

        need = self._need
        nv = need[w1 : w1 + n]
        np.maximum(peak, 1e-12, out=nv)
        np.divide(self.ceiling, nv, out=nv)
        np.minimum(nv, 1.0, out=nv)
        # Trailing min over the window: m[i] = min(need over the last `window` samples).
        self._sliding_min(need[: w1 + n], w, self._m[:n])
        held = self._release(n)
        h64, cs = self._held64[: w1 + n], self._csum
        np.copyto(h64, held[: w1 + n])
        np.cumsum(h64, out=cs[1 : w1 + n + 1])
        g64, g32 = self._g64[:n], self._g32[:n]
        np.subtract(cs[w : w + n], cs[:n], out=g64)
        g64 /= w
        np.copyto(g32, g64, casting="same_kind")
        for c in range(self.channels):
            np.multiply(xw[:n, c], g32, out=out[:, c])
        # fmin / fmax return the number when one side is NaN: the clamp itself is NaN-safe.
        np.fmin(out, self.ceiling, out=out)
        np.fmax(out, self._neg_ceiling, out=out)
        self.last_min_gain = float(g32.min())
        self.min_gain = min(self.min_gain, self.last_min_gain)

        xw[:w1] = xw[n : n + w1]
        need[:w1] = need[n : n + w1]
        held[:w1] = held[n : n + w1]
        self._idle = bool(self._held_last >= 1.0 and held[:w1].min() >= 1.0)
        if self._idle:
            need[:w1].fill(1.0)

    def _sliding_min(self, a: np.ndarray, width: int, out: np.ndarray, step: int = 1) -> None:
        """out[i] = min(a[i * step : i * step + width]), by doubling: after each pass
        b[i] is the min of a[i : i + span]; two overlapping spans cover the full width."""
        length, src, span = len(a), a, 1
        bufs = (self._ping, self._pong)
        turn = 0
        while 2 * span <= width:
            valid = length - span
            dst = bufs[turn]
            np.minimum(src[:valid], src[span:length], out=dst[:valid])
            src, length, span, turn = dst, valid, span * 2, 1 - turn
        dst = bufs[turn]
        last = (len(out) - 1) * step + 1
        np.minimum(src[:last], src[width - span : width - span + last], out=dst[:last])
        np.copyto(out, dst[:last:step])

    def _release(self, n: int) -> np.ndarray:
        """Writes the held gain of the block into self._held[w - 1:] and returns the buffer.

        Per chunk of samples the gain g is never above the chunk's min of m and recovers
        towards 1 at most at the release rate; staying at or below m keeps the ceiling
        guarantee. With r = 1 - g the recursion r_j = max(1 - min_j, q * r_(j-1)) has the
        closed form r_j = q^j * max(q * r_(-1), max_(i<=j) (1 - min_i) / q^i), which is
        one maximum.accumulate.
        """
        k = self.chunk
        chunks = -(-n // k)
        span = chunks * k
        if span > n:
            self._m[n:span] = np.inf
        m = self._m[:span]
        cmin, r = self._cmin[:chunks], self._r[:chunks]
        mask, g = self._mask[:chunks], self._gch[:chunks]
        self._sliding_min(m, k, cmin, step=k)
        np.copyto(r, cmin)
        np.subtract(1.0, r, out=r)
        r *= self._qinv[:chunks]
        np.maximum.accumulate(r, out=r)
        np.maximum(r, (1.0 - self._held_last) * self._q, out=r)
        r *= self._qpow[:chunks]
        np.less(r, 1e-5, out=mask)  # close enough to gain 1: snap to it
        np.copyto(r, 0.0, where=mask)
        last = 1.0 - float(r[chunks - 1])
        self._held_last = last if math.isfinite(last) else 0.0  # never latch NaN
        np.subtract(1.0, r, out=r)
        np.copyto(g, r, casting="same_kind")
        gs = self._gs[:span]
        np.copyto(gs.reshape(chunks, k), g.reshape(chunks, 1))
        w1 = self.window - 1
        held = self._held
        np.minimum(m, gs, out=held[w1 : w1 + span])
        return held

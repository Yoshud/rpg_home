"""Timing measurements: a fixed log-scale histogram, filled from the audio thread.

add() is a few float operations and one list item increment. summary_ms() walks the
fixed number of bins (about 20 microseconds), so a frequent status() poller holding
the GIL never delays the callback by more than that, however long the run.
Percentiles are those of the whole run, to the resolution of a bin (about 6 %).
"""

import math

BINS_PER_DECADE = 40
MIN_S = 1e-7  # 0.1 microsecond
DECADES = 7  # up to 1 s; longer values go into the last bin (max is exact)


class Timings:
    def __init__(self):
        self._bins = [0] * (BINS_PER_DECADE * DECADES)
        self.count = 0
        self.max = 0.0

    def add(self, seconds: float) -> None:
        if seconds > MIN_S:
            i = int(math.log10(seconds / MIN_S) * BINS_PER_DECADE)
            if i >= len(self._bins):
                i = len(self._bins) - 1
        else:
            i = 0
        self._bins[i] += 1
        self.count += 1
        if seconds > self.max:
            self.max = seconds

    def _percentile(self, q: float) -> float:
        """Geometric centre of the bin holding the q-th value (never above max)."""
        rank = q * (self.count - 1)
        seen = 0
        for i, n in enumerate(self._bins):
            seen += n
            if seen > rank:
                centre = MIN_S * 10 ** ((i + 0.5) / BINS_PER_DECADE)
                return min(centre, self.max)
        return self.max

    def summary_ms(self) -> dict:
        """p50 and p95 over the whole run (bin resolution), exact max, in milliseconds."""
        if not self.count:
            return {"count": 0, "p50": None, "p95": None, "max": None}
        return {
            "count": self.count,
            "p50": round(self._percentile(0.50) * 1e3, 4),
            "p95": round(self._percentile(0.95) * 1e3, 4),
            "max": round(self.max * 1e3, 4),
        }

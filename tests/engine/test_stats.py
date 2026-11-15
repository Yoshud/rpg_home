import time

import numpy as np
import pytest

from rpghome_engine.stats import Timings


def test_percentiles_match_numpy_to_a_bin_and_cost_the_same_at_any_length():
    rng = np.random.default_rng(2)
    values = rng.lognormal(np.log(2e-5), 0.5, 300_000)  # callback-like: ~20 us
    t = Timings()
    for v in values[:1000]:
        t.add(float(v))
    t0 = time.perf_counter()
    t.summary_ms()
    short = time.perf_counter() - t0
    for v in values[1000:]:
        t.add(float(v))
    t0 = time.perf_counter()
    s = t.summary_ms()
    long = time.perf_counter() - t0
    p50, p95 = np.percentile(values, [50, 95]) * 1e3
    assert s["count"] == 300_000
    assert s["p50"] == pytest.approx(p50, rel=0.06)
    assert s["p95"] == pytest.approx(p95, rel=0.06)
    assert s["max"] == pytest.approx(values.max() * 1e3, rel=1e-3)
    assert long < 0.002 and long < 20 * max(short, 1e-5)  # a fixed walk over the bins


def test_extremes_and_empty():
    t = Timings()
    assert t.summary_ms() == {"count": 0, "p50": None, "p95": None, "max": None}
    t.add(0.0)
    t.add(5.0)  # longer than the last bin
    s = t.summary_ms()
    assert s["max"] == 5000.0 and s["p95"] <= s["max"]

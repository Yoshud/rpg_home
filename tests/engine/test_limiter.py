import numpy as np

from rpghome_engine.limiter import Limiter


def run(lim, x, sizes):
    out, pos, i = [], 0, 0
    while pos < len(x):
        n = sizes[i % len(sizes)]
        block = x[pos : pos + n]
        y = np.empty_like(block)
        lim.process(block, y)
        out.append(y)
        pos, i = pos + n, i + 1
    return np.concatenate(out)


def test_spikes_never_pass_the_ceiling_with_any_block_size():
    rng = np.random.default_rng(3)
    x = rng.normal(0, 0.2, (48000 * 3, 2)).astype(np.float32)
    spikes = rng.integers(0, len(x), 300)
    x[spikes] *= 30  # single-sample peaks up to ~+25 dBFS
    x[50000:52000] = 4.0  # and a sustained overload
    lim = Limiter()
    y = run(lim, x, [512, 1, 7, 300, 4096, 129])
    assert np.abs(y).max() <= lim.ceiling
    assert lim.min_gain < 0.25


def test_below_the_ceiling_it_is_a_pure_delay():
    rng = np.random.default_rng(4)
    x = rng.uniform(-0.85, 0.85, (20000, 2)).astype(np.float32)
    lim = Limiter()
    y = run(lim, x, [512, 3, 1000])
    d = lim.delay
    np.testing.assert_array_equal(y[d:], x[:-d])
    assert np.all(y[:d] == 0)


def test_gain_recovers_after_a_peak():
    x = np.full((48000 * 2, 2), 0.5, dtype=np.float32)
    x[1000] = 5.0
    lim = Limiter(release_s=0.1)
    y = run(lim, x, [512])
    assert np.abs(y).max() <= lim.ceiling
    np.testing.assert_allclose(y[-1000:], 0.5, rtol=1e-4)


def test_ceiling_holds_strictly_in_float32():
    lim = Limiter()
    assert lim.ceiling.dtype == np.float32
    assert float(lim.ceiling) <= 10 ** (-1 / 20)
    assert float(np.nextafter(lim.ceiling, np.float32(1))) > 10 ** (-1 / 20)
    x = np.full((4096, 2), 3.0, dtype=np.float32)
    y = run(lim, x, [512])
    assert float(np.abs(y).max()) <= 10 ** (-1 / 20)
    assert 20 * np.log10(float(np.abs(y).max())) <= -1.0


def test_matches_the_previous_implementation():
    """The allocation-free limiter gives the output of the old one (concatenates and a
    Python release loop) within float rounding, over quiet, loud and mixed material."""
    from .limiter_reference import Limiter as Reference

    rng = np.random.default_rng(5)
    x = rng.normal(0, 0.3, (48000 * 4, 2)).astype(np.float32)
    x[48000:96000] *= 4  # a long overload
    x[rng.integers(0, len(x), 200)] *= 10  # and spikes
    sizes = [512, 1, 7, 300, 4096, 129, 512]
    new, old = Limiter(), Reference()
    y_new, y_old = run(new, x, sizes), run(old, x, sizes)
    np.testing.assert_allclose(y_new, y_old, rtol=0, atol=2e-5)
    assert abs(new.min_gain - old.min_gain) < 1e-5


def test_nan_and_inf_never_poison_the_state():
    lim = Limiter()
    clean = np.full((512, 2), 0.5, dtype=np.float32)
    y = np.empty_like(clean)
    lim.process(np.full((512, 2), 2.0, np.float32), y)  # engaged
    bad = clean.copy()
    bad[10, 0], bad[20, 1], bad[30, 0] = np.nan, np.inf, -np.inf
    lim.process(bad, y)
    assert np.isfinite(y).all() and np.abs(y).max() <= lim.ceiling
    assert lim.nonfinite_blocks == 1
    for _ in range(400):  # ~4 s of clean audio: the gain recovers fully
        lim.process(clean, y)
        assert np.isfinite(y).all()
    np.testing.assert_allclose(y, 0.5, rtol=1e-4)
    lim.process(np.full((512, 2), np.nan, np.float32), y)  # a whole NaN block
    assert np.isfinite(y).all()

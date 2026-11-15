"""The audio-thread mixer on its own: bad messages and allocations per block."""

import tracemalloc

import numpy as np

from rpghome_engine.mixer import Mixer


def test_bad_messages_are_counted_and_dropped():
    m = Mixer(48000)
    tone = (0.25 * np.sin(np.arange(48000) * 0.05)).astype(np.float32)
    m.post("add_scene", 1, 0, 1.0, {"bed": 1.0})
    m.post("events", 1, "bed", [(0, tone)])
    bad = [
        (),  # empty
        ("bogus", 1),  # unknown kind
        ("layer_gain", 1, "spots", 0.5),  # no such layer
        ("events", 1, "bed", [(0, np.zeros((10, 3), np.float32))]),  # 3 channels
        ("fade", 1, 0, 100, "sideways"),  # unknown fade
        ("master",),  # missing value
        ("add_scene", 2),  # missing fields
    ]
    for msg in bad:
        m.post(*msg)
    out = np.zeros((512, 2), np.float32)
    for _ in range(10):
        m.render(out)  # never raises
    assert m.bad_messages == len(bad)
    assert np.abs(out).max() > 0.2  # the good messages still play
    m.post("layer_gain", 99, "bed", 0.5)  # a scene that already ended: ignored, not bad
    m.render(out)
    assert m.bad_messages == len(bad)


def _busy_mixer(n):
    """Mono and stereo buffers, a gain ramp, a fade and an engaged limiter."""
    m = Mixer(48000, max_block=n)
    rng = np.random.default_rng(1)
    loud = rng.uniform(-3, 3, 48000 * 4).astype(np.float32)
    stereo = rng.uniform(-1, 1, (48000 * 4, 2)).astype(np.float32)
    m.post("add_scene", 1, 0, 1.0, {"bed": 1.0, "other": 0.5})
    m.post("events", 1, "bed", [(0, loud[:30000]), (30000, loud[30000:])])
    m.post("events", 1, "other", [(100, stereo)])
    m.post("fade", 1, 0, 48000, "in")
    return m


def test_render_allocates_no_block_sized_arrays():
    """numpy reports its buffers to tracemalloc. A block of 2048 stereo float32 is 16 KB
    (8 KB per channel); what is left per block is small Python objects (views, floats)."""
    n = 2048
    m = _busy_mixer(n)
    out = np.zeros((n, 2), np.float32)
    m.render(out)
    tracemalloc.start()
    worst = 0
    try:
        for i in range(40):
            if i == 10:
                m.post("layer_gain", 1, "bed", 0.3)  # a ramp
                m.post("master", 2.0)
            tracemalloc.reset_peak()
            base = tracemalloc.get_traced_memory()[0]
            m.render(out)
            worst = max(worst, tracemalloc.get_traced_memory()[1] - base)
    finally:
        tracemalloc.stop()
    assert m.limiter.min_gain < 0.5  # the limiter was engaged
    assert worst < 6000, worst

"""Spot layers, distance and the scene reverb, rendered offline from synthetic clips."""

import gc
import itertools
import math
import random
import threading
import time

import numpy as np
import pytest
import soundfile as sf

from rpghome.models.scene import Bed, ClipRef, Reverb, Scene, Spots
from rpghome_engine.clips import EngineError
from rpghome_engine.core import CONTROL_PERIOD_S, HORIZON_S, Engine
from rpghome_engine.devices import OfflineDevice, SoundDeviceOutput
from rpghome_engine.mixer import Mixer
from rpghome_engine.spots import (
    LOWPASS_CACHE,
    SPECTRA_CACHE,
    Room,
    SpotSound,
    SpotsPlayer,
    _fades,
    _fft_size,
    distance,
)

from .conftest import LEAD, RATE, Rig, amp_at, write_noise, write_tone
from .test_devices import BurstFakeStream, _Status, _Time
from .test_memory import FakeLibc, covered, memory_pin

CEILING = 10 ** (-1 / 20)
LONG_S = 2 * 3600  # engine time of the long render


def write(path, x, rate=RATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), x.astype(np.float32), rate, "FLOAT")


def drops_clip(path, n=6, seed=0, spacing_s=1.0):
    """n decaying noise bursts spacing_s apart over a quiet noise floor (a "drips" clip)."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(-1e-3, 1e-3, round((n * spacing_s + 0.5) * RATE))
    t = np.arange(round(0.4 * RATE)) / RATE
    for k in range(n):
        a = round((0.3 + k * spacing_s) * RATE)
        x[a : a + len(t)] += 0.3 * rng.uniform(-1, 1, len(t)) * np.exp(-t / 0.05)
    write(path, x)


def stone_clip(path, seconds=1.5, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(round(seconds * RATE)) / RATE
    write(path, 0.3 * rng.uniform(-1, 1, len(t)) * np.exp(-t / 0.4))


def cave_scene(data, reverb=True, stones_distance_m=5.0):
    """examples/cave.json's shape with synthetic clips: the demo C settings."""
    bed = []
    for i in range(3):
        write_noise(data / f"bed{i}.wav", 20, amp=0.05, seed=10 + i)
        bed.append(ClipRef(path=f"bed{i}.wav"))
    drops, stones = [], []
    for source, k in itertools.product(("moss", "epidemic"), (1, 2)):
        drops_clip(data / f"{source}/drops{k}.wav", seed=k + 7 * len(source))
        drops.append(ClipRef(path=f"{source}/drops{k}.wav", source=source))
        stone_clip(data / f"{source}/stone{k}.wav", seed=k + 7 * len(source))
        stones.append(ClipRef(path=f"{source}/stone{k}.wav", source=source, perspective="close"))
    weights = {"moss": 3, "epidemic": 1}
    return Scene(
        name="cave",
        bed=Bed(clips=tuple(bed), hold_s=(150, 210), crossfade_s=35, loop_crossfade_s=4),
        spots=(
            Spots(
                name="drops",
                clips=tuple(drops),
                interval_s=(1.5, 12),
                mean_interval_s=4,
                gain_db=(-10, -2),
                pitch_semitones=2,
                source_weights=weights,
                slice=True,
            ),
            Spots(
                name="stones",
                clips=tuple(stones),
                interval_s=(15, 80),
                mean_interval_s=35,
                gain_db=(-6, 0),
                distance_m=stones_distance_m,
                source_weights=weights,
            ),
        ),
        reverb=Reverb() if reverb else None,
    )


def clamped_mean(mean, lo, hi):
    """E[min(hi, max(lo, X))] for X exponential with the given mean."""
    return lo + mean * (math.exp(-lo / mean) - math.exp(-hi / mean))


def run_long(data, density=1.0, seconds=LONG_S):
    engine = Engine(data, seed=11)
    engine.scene(cave_scene(data), 2)
    if density != 1.0:
        engine.set("density", density)
    peak = 0.0
    for block in OfflineDevice(engine).blocks(round(seconds * RATE)):
        peak = max(peak, float(np.abs(block).max()))
    return engine, peak


@pytest.fixture(scope="module")
def long_render(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    engine, peak = run_long(data)
    return data, engine, engine.spot_timeline(), peak


def by_layer(events):
    out = {}
    for e in events:
        out.setdefault(e["layer"], []).append(e)
    return out


def test_long_render_follows_the_settings(long_render):
    _, engine, events, _ = long_render
    layers = by_layer(events)
    settings = {"drops": (1.5, 12, 4, (-10, -2), 2), "stones": (15, 80, 35, (-6, 0), 0)}
    for name, (lo, hi, mean, (g0, g1), pitch) in settings.items():
        ev = layers[name]
        gaps = np.diff([e["start_s"] for e in ev])
        assert gaps.min() >= lo - 1e-6 and gaps.max() <= hi + 1e-6, name
        # Drawn, not constant: the clamps are reached.
        assert gaps.min() < lo + 0.05 * (hi - lo) and gaps.max() > hi - 0.1 * (hi - lo)
        expected = clamped_mean(mean, lo, hi)
        assert gaps.mean() == pytest.approx(expected, rel=0.04 if name == "drops" else 0.12)
        gains = [e["gain_db"] for e in ev]
        assert g0 <= min(gains) < g0 + 0.5 and g1 - 0.5 < max(gains) <= g1
        pitches = [e["pitch_semitones"] for e in ev]
        assert all(abs(p) <= pitch for p in pitches)
        if pitch:
            assert min(pitches) < -0.9 * pitch and max(pitches) > 0.9 * pitch
        pans = [e["pan"] for e in ev]
        assert all(abs(p) <= 0.8 for p in pans) and max(pans) > 0.75 and min(pans) < -0.75
        clips = [e["clip"] for e in ev]
        assert all(a != b for a, b in itertools.pairwise(clips)), name
        moss = sum(e["source"] == "moss" for e in ev) / len(ev)
        assert moss == pytest.approx(0.75, abs=0.03 if name == "drops" else 0.08), name
    assert len(layers["drops"]) > 1500 and len(layers["stones"]) > 180
    # Sliced: drops are single slices ("path#n"), stones whole clips.
    assert all("#" in e["clip"] for e in layers["drops"])
    assert not any("#" in e["clip"] for e in layers["stones"])
    assert len({e["clip"] for e in layers["drops"]}) == 24  # 4 clips of 6 drops
    s = engine.status()
    assert s["spot_events"] == len(events)
    assert (s["starved_blocks"], s["late_events"], s["bad_messages"]) == (0, 0, 0)
    print_stats(engine, layers)


def print_stats(engine, layers):
    for name, ev in layers.items():
        gaps = np.diff([e["start_s"] for e in ev])
        moss = sum(e["source"] == "moss" for e in ev)
        print(
            f"\n{name}: {len(ev)} events, interval min {gaps.min():.2f} max {gaps.max():.2f} "
            f"mean {gaps.mean():.2f} s, gain {min(e['gain_db'] for e in ev):.2f}.."
            f"{max(e['gain_db'] for e in ev):.2f} dB, pitch "
            f"{min(e['pitch_semitones'] for e in ev):.2f}.."
            f"{max(e['pitch_semitones'] for e in ev):.2f}"
            f", moss:epidemic {moss}:{len(ev) - moss} = {moss / max(1, len(ev) - moss):.2f}"
        )
    t = engine.status()["timings_ms"]
    print(f"control ms {t['control']}\ncallback ms {t['callback']}")


def test_limiter_ceiling_holds_with_spots(long_render):
    *_, peak = long_render
    assert peak <= CEILING
    rig = Rig(long_render[0], seed=2)
    rig.engine.set("master_gain_db", 24)
    rig.engine.scene(cave_scene(long_render[0]), 0)
    rig.render(120)
    assert np.abs(rig.out).max() <= np.float32(CEILING)
    assert rig.engine.status()["limiter_min_gain_db"] < -10  # it did work


def test_density_two_halves_the_mean_interval(long_render):
    data, _, events, _ = long_render
    engine, _ = run_long(data, density=2.0, seconds=LONG_S / 2)
    dense = by_layer(engine.spot_timeline())
    normal = by_layer(events)
    for name in ("drops", "stones"):
        ratio = (
            np.diff([e["start_s"] for e in dense[name]]).mean()
            / np.diff([e["start_s"] for e in normal[name]]).mean()
        )
        assert ratio == pytest.approx(0.5, rel=0.06 if name == "drops" else 0.15), name


def test_density_applies_to_intervals_planned_after_the_call(data):
    def run(density):
        rig = Rig(data, seed=4)
        rig.engine.scene(cave_scene(data, reverb=False), 0)
        rig.render(60)
        if density:
            rig.engine.set("density", density)
        rig.render(120)
        return rig, [
            (e["start_s"], e["clip"]) for e in by_layer(rig.engine.spot_timeline())["drops"]
        ]

    _, base = run(None)
    rig, dense = run(4)
    # Planned before the change: up to the horizon of the last cycle before it.
    planned = 60 + HORIZON_S - CONTROL_PERIOD_S
    assert [e for e in base if e[0] < 60] == [e for e in dense if e[0] < 60]
    early = [e for e in dense if e[0] < planned]
    assert all(e in base for e in early)  # planned events stay
    late_base = np.diff([t for t, _ in base if t > planned + 15]).mean()
    late_dense = np.diff([t for t, _ in dense if t > planned + 15]).mean()
    assert late_dense < late_base / 2.5
    assert rig.engine.status()["levels"]["density"] == 4


def test_density_is_validated_and_ignored_without_a_scene(rig, caplog):
    for bad in (0.05, 11, "x", True):
        with pytest.raises(EngineError, match="density"):
            rig.engine.set("density", bad)
    rig.engine.set("density", 2)
    rig.render(0.5)
    assert "density = 2 ignored" in caplog.text


def test_same_seed_same_render(data):
    scene = cave_scene(data)

    def render(seed):
        r = Rig(data, seed=seed)
        r.engine.scene(scene, 0.5)
        r.render(40)
        return r.out, r.engine.spot_timeline()

    x1, t1 = render(5)
    x2, t2 = render(5)
    x3, t3 = render(6)
    assert np.array_equal(x1, x2) and t1 == t2 and len(t1) > 5
    assert not np.array_equal(x1, x3) and t1 != t3


# -- distance and reverb: one event per render ----------------------------------------


def one_spot(data, clip, distance_m=None, reverb=True, perspective=None, at_s=5.0):
    """A scene whose only layer plays `clip` at at_s (centre, gain 0, no pitch)."""
    ref = ClipRef(path=clip, perspective=perspective)
    spots = Spots(name="one", clips=(ref,), interval_s=(at_s, at_s), pan=0, distance_m=distance_m)
    rig = Rig(data)
    rig.engine.scene(Scene(name="s", spots=(spots,), reverb=Reverb() if reverb else None), 0)
    rig.render(at_s + 4.5)  # before the next event, at 2 * at_s
    (event,) = rig.engine.spot_timeline()
    start = round(event["start_s"] * RATE)
    return rig.out[start:], event


def burst(data, name, freq=None, seconds=0.5):
    """A constant tone (or white noise) burst, faded in and out over 5 ms."""
    n = round(seconds * RATE)
    if freq is None:
        x = np.random.default_rng(1).uniform(-0.3, 0.3, n)
    else:
        x = 0.3 * np.sin(2 * np.pi * freq * np.arange(n) / RATE)
    f = round(0.005 * RATE)
    x[:f] *= np.linspace(0, 1, f)
    x[-f:] *= np.linspace(1, 0, f)
    write(data / name, x)
    return name, n


def energy(x):
    return float(np.sum(x.astype(np.float64) ** 2))


def hf_share(x, above_hz=4000):
    spec = np.abs(np.fft.rfft(x[:, 0].astype(np.float64))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / RATE)
    return spec[f > above_hz].sum() / spec.sum()


def test_distance_curve_is_documented_values():
    d = distance(1.0, 20.0)
    assert d.attenuation_db == pytest.approx(-20 * math.log10(20))  # -6 dB per doubling
    assert d.lowpass_hz == pytest.approx(20000 / 20**0.8)
    assert d.wet_extra_db == pytest.approx(10 * math.log10(20))
    assert distance(1.0, 2.0).attenuation_db == pytest.approx(-6.02, abs=0.01)
    # Only the ratio counts: 5 -> 20 m is the same change as 1 -> 4 m.
    keys = ("attenuation_db", "lowpass_hz", "wet_extra_db")
    assert [getattr(distance(5.0, 20.0), k) for k in keys] == [
        getattr(distance(1.0, 4.0), k) for k in keys
    ]
    near = distance(20.0, 1.0)  # never brought closer
    assert (near.distance_m, near.attenuation_db, near.lowpass_hz, near.wet_extra_db) == (
        20.0,
        0.0,
        None,
        0.0,
    )


def test_far_spot_is_quieter_by_the_curve(data):
    clip, n = burst(data, "low.wav", freq=150)
    near, e1 = one_spot(data, clip, distance_m=1, reverb=False)
    far, e20 = one_spot(data, clip, distance_m=20, reverb=False)
    level = 10 * math.log10(energy(far[:n]) / energy(near[:n]))
    assert level == pytest.approx(-20 * math.log10(20), abs=0.2)  # -26 dB; 150 Hz passes
    assert (e1["attenuation_db"], e1["lowpass_hz"]) == (0.0, None)
    assert e20["distance_m"] == 20 and e20["lowpass_hz"] == pytest.approx(1820.6, abs=0.1)


def test_far_spot_is_darker_and_wetter(data):
    clip, n = burst(data, "noise.wav")
    near, e1 = one_spot(data, clip, distance_m=1)
    far, e20 = one_spot(data, clip, distance_m=20)
    assert hf_share(far[:n]) < 0.2 * hf_share(near[:n])
    tail = n + round(0.05 * RATE)

    def wet_dry(x):
        return 10 * math.log10(energy(x[tail:]) / energy(x[:n]))

    assert wet_dry(far) > wet_dry(near) + 8
    assert e20["wet_db"] == pytest.approx(-10 + 10 * math.log10(20), abs=1e-3)
    assert e1["wet_db"] == -10


def test_far_clip_is_not_brought_closer(data):
    clip, _ = burst(data, "noise.wav")
    asked, e_near = one_spot(data, clip, distance_m=1, perspective="far")
    own, e_own = one_spot(data, clip, distance_m=None, perspective="far")
    assert np.array_equal(asked, own)
    assert (e_near["distance_m"], e_near["attenuation_db"], e_near["lowpass_hz"]) == (20, 0, None)
    assert e_near["wet_db"] == e_own["wet_db"] == -10


def test_spots_have_a_tail_and_the_bed_has_none(data):
    write_tone(data / "bed.wav", 300, 20, 0.25)
    clip, n = burst(data, "spot.wav", freq=3000)
    bed = Bed(clips=(ClipRef(path="bed.wav"),), loop_crossfade_s=0.5)
    spots = Spots(name="one", clips=(ClipRef(path=clip),), interval_s=(2, 2), pan=0)

    def render(scene):
        rig = Rig(data)
        rig.engine.scene(scene, 0)
        rig.render(8)
        return rig.out

    with_spots = render(Scene(name="s", bed=bed, spots=(spots,), reverb=Reverb(wet_db=0)))
    bed_alone = render(Scene(name="s", bed=bed))
    for t in np.arange(0.5, 7.5, 0.25):  # the bed is the same tone, never convolved
        assert amp_at(with_spots, 300, t) == pytest.approx(0.25, abs=0.002)
    start = 2 + LEAD / RATE
    after = start + n / RATE + 0.1
    assert amp_at(with_spots, 3000, after, 0.05) > 0.005  # the spot's reverb tail
    assert amp_at(bed_alone, 3000, after, 0.05) < 1e-4
    # The bed alone with a scene reverb is exactly the bed: the reverb is for spots.
    assert np.array_equal(render(Scene(name="s", bed=bed, reverb=Reverb())), bed_alone)


def test_dry_spots_without_reverb_end_with_the_clip(data):
    clip, n = burst(data, "noise.wav")
    x, event = one_spot(data, clip, reverb=False)
    assert energy(x[n + 200 :]) == 0 and event["wet_db"] is None  # + the limiter delay


def test_scene_switch_cancels_planned_spots(data):
    rig = Rig(data, seed=3)
    rig.engine.scene(cave_scene(data), 0)
    rig.render(30)
    switch = rig.now + LEAD / RATE
    rig.engine.scene(Scene(name="quiet"), 3)
    rig.render(15)
    starts = [e["start_s"] for e in rig.engine.spot_timeline()]
    assert starts and max(starts) < switch
    s = rig.engine.status()
    assert (s["starved_blocks"], s["late_events"], s["bad_messages"]) == (0, 0, 0)
    assert rig.engine.mixer.scenes.keys() == {2}
    assert np.abs(rig.out[-RATE:]).max() == 0


def test_clip_without_drops_is_skipped_and_an_empty_layer_is_an_error(data, caplog):
    write_tone(data / "hum.wav", 100, 3, 0.2)  # no onsets
    drops_clip(data / "d.wav")
    rig = Rig(data)
    ok = Spots(
        name="d",
        clips=(ClipRef(path="hum.wav"), ClipRef(path="d.wav")),
        interval_s=(1, 2),
        slice=True,
    )
    rig.engine.scene(Scene(name="s", spots=(ok,)), 0)
    assert "no single drops found in hum.wav" in caplog.text
    bad = Spots(name="d", clips=(ClipRef(path="hum.wav"),), interval_s=(1, 2), slice=True)
    with pytest.raises(EngineError, match="no single drops"):
        rig.engine.scene(Scene(name="s", spots=(bad,)), 0)
    with pytest.raises(EngineError, match="not found"):
        rig.engine.scene(
            Scene(
                name="s",
                spots=(Spots(name="x", clips=(ClipRef(path="nope.wav"),), interval_s=(1, 2)),),
            ),
            0,
        )


def test_spot_timeline_is_in_the_report_fields(long_render):
    _, _, events, _ = long_render
    e = events[0]
    assert set(e) == {
        "scene",
        "start_s",
        "layer",
        "clip",
        "source",
        "gain_db",
        "pan",
        "pitch_semitones",
        "clip_distance_m",
        "distance_m",
        "attenuation_db",
        "lowpass_hz",
        "wet_db",
    }
    stones = [x for x in events if x["layer"] == "stones"]
    assert {x["distance_m"] for x in stones} == {5.0}
    assert {x["lowpass_hz"] for x in stones} == {round(20000 / 5**0.8, 1)}


# -- review fixes ----------------------------------------------------------------------


def make_player(pool, weights=None, seed=1):
    """A player over fake sounds: pool is a list of sources, one entry per sound."""
    clips = tuple(ClipRef(path=f"{src}{i}.wav", source=src) for i, src in enumerate(pool))
    spots = Spots(
        name="p",
        clips=clips,
        interval_s=(1, 2),
        source_weights=weights or {"moss": 3, "epidemic": 1},
    )
    sounds = [
        SpotSound(c, None, c.source, spots.weight(c), np.zeros(480, np.float32)) for c in clips
    ]
    return SpotsPlayer(spots, sounds, Room(None, RATE), random.Random(seed), 0, RATE)


@pytest.mark.parametrize(
    ("pool", "ratio"),
    [
        (["moss", "moss", "epidemic"], 3.0),  # one Epidemic sound: it cannot follow itself
        (["moss", "moss", "epidemic", "epidemic"], 3.0),
        (["moss"] * 12 + ["epidemic"] * 24, 3.0),
        # One MOSS sound at 3:1: never twice in a row caps it at every other pick.
        (["moss", "epidemic", "epidemic"], 1.0),
    ],
)
def test_source_ratio_holds_with_a_single_sound_source(pool, ratio):
    player = make_player(pool)
    picks = []
    for _ in range(40000):
        i = player._pick()
        player._previous = i
        picks.append(i)
    assert all(a != b for a, b in itertools.pairwise(picks))
    moss = sum(pool[i] == "moss" for i in picks)
    assert moss / (len(picks) - moss) == pytest.approx(ratio, rel=0.04), pool


def test_one_sound_pool_repeats(data):
    player = make_player(["moss"])
    assert [player._pick() for _ in range(3)] == [0, 0, 0]


@pytest.mark.parametrize("samples", [1, 96, 479])  # 1 sample, 2 ms, just under 10 ms
def test_too_short_spot_clips_are_an_error_from_scene(rig, data, samples):
    write(data / "short.wav", np.full(samples, 0.1))
    for slice_ in (False, True):
        spots = Spots(
            name="s",
            clips=(ClipRef(path="short.wav"),),
            interval_s=(1, 2),
            pitch_semitones=2,
            slice=slice_,
        )
        with pytest.raises(EngineError, match="too short for a spot"):
            rig.engine.scene(Scene(name="s", spots=(spots,), reverb=Reverb()), 0)


def test_short_clips_and_sizes_do_not_crash():
    assert _fft_size(0) == _fft_size(1) == 64
    assert _fft_size(1000) == 1024 and _fft_size(1100) == 1280 and _fft_size(1281) == 1536
    assert len(_fades(np.ones(3, np.float32), RATE)) == 3
    room = Room(Reverb(), RATE)
    y = room.render(np.full(480, 0.1, np.float32), 0, 2.0, 0.3, 800.0, -10)
    assert y.shape[1] == 2 and np.isfinite(y).all()


def test_lowpass_does_not_wrap_around():
    """A click at the start and silence after it: the zero-phase low-pass rings on both
    sides of the click, and none of it may reach the silent end of the buffer."""
    x = np.zeros(4096, np.float32)  # a fast FFT size: no slack without the padding
    x[0] = 1.0
    y = Room(None, RATE).render(x, 0.0, 0.0, 0.0, 500.0, None)
    assert len(y) == 4096
    peak = np.abs(y).max()
    assert peak > 0.01
    assert np.abs(y[-480:]).max() < 1e-6 * peak  # the last 10 ms: silence


def test_room_caches_are_bounded():
    room = Room(Reverb(), RATE)
    rng = np.random.default_rng(0)
    for k in range(40):  # many lengths (FFT sizes) and cutoffs
        x = rng.uniform(-0.1, 0.1, 2000 + 9000 * k).astype(np.float32)
        room.render(x, 0, 0, 0, 500.0 + 100 * k, -10)
    assert len(room._ir_spectra) <= SPECTRA_CACHE and len(room._lowpass) <= LOWPASS_CACHE
    # Largest here: n = 2^19 (10.9 s): 3 IR spectra at 4.2 MB, 4 responses at 1 MB.
    assert room.cache_bytes < 20e6


def test_cave_room_cache_stays_small(long_render):
    _, engine, _, _ = long_render
    rooms = {id(p.room): p.room for r in engine._runs.values() for p in r.spots}
    assert rooms and all(r.cache_bytes < 15e6 for r in rooms.values())
    print(f"\ncave room caches: {[round(r.cache_bytes / 1e6, 1) for r in rooms.values()]} MB")


def test_control_time_per_cycle_with_spots_and_reverb(data):
    """The control cycle on the cave settings (both spot layers, reverb), 20 min of
    engine time. Only a generous bound is asserted; the numbers are printed."""
    engine = Engine(data, seed=3)
    engine.scene(cave_scene(data), 2)
    for _ in OfflineDevice(engine).blocks(1200 * RATE):
        pass
    t = engine.status()["timings_ms"]
    control, callback = t["control"], t["callback"]
    print(f"\ncontrol per cycle ms: {control}\ncallback ms: {callback}")
    assert control["count"] >= 1200 / CONTROL_PERIOD_S * 0.95  # 19 blocks per period
    period_ms = CONTROL_PERIOD_S * 1e3
    assert control["p95"] < period_ms / 4 and control["max"] < 5 * period_ms
    assert callback["p95"] < t["callback_budget"] / 10


def test_spot_event_buffers_are_pinned_and_released(data):
    libc = FakeLibc()
    m = memory_pin(libc)
    rig = Rig(data)
    rig.engine = Engine(data, seed=1, memory=m)
    rig.device = OfflineDevice(rig.engine)
    before = m.buffers  # the mixer's and limiter's own buffers
    rig.engine.scene(cave_scene(data), 0)
    seen = 0
    for _ in range(100):  # 20 s, looking at the planned buffers every 0.2 s
        rig.render(0.2)
        layers = rig.engine.mixer.scenes[rig.engine._current.sid].layers
        events = [d for name in ("spots:drops", "spots:stones") for _, d in layers[name].events]
        assert all(covered(libc, d) for d in events)  # what the callback reads
        seen += len(events)
    assert seen > 10
    del events, layers
    rig.engine.stop(0.1)
    rig.render(3)
    rig.engine.control_cycle()  # frees what the audio thread let go of
    gc.collect()
    rig.engine.control_cycle()
    assert rig.engine.mixer.scenes == {}
    assert m.buffers == before  # every event buffer (and bed clip) unpinned when freed
    assert any(c[0] == "munlock" for c in libc.calls)


def test_the_audio_thread_never_pins_or_unpins_spot_buffers(data):
    class ThreadLibc(FakeLibc):
        def __init__(self):
            super().__init__()
            self.threads = set()

        def mlock(self, addr, length):
            self.threads.add(threading.get_ident())
            return super().mlock(addr, length)

        def munlock(self, addr, length):
            self.threads.add(threading.get_ident())
            return super().munlock(addr, length)

    libc = ThreadLibc()
    m = memory_pin(libc)
    engine = Engine(data, seed=1, memory=m, horizon_s=0.5, control_period_s=0.05)
    drops_clip(data / "d.wav", n=6, spacing_s=0.5)
    spots = Spots(name="d", clips=(ClipRef(path="d.wav"),), interval_s=(0.05, 0.1), slice=False)
    engine.scene(Scene(name="s", spots=(spots,), reverb=Reverb(decay_s=0.2)), 0)

    class LongBurst(BurstFakeStream):
        def _run(self):
            buf = np.zeros((512, 2), dtype=np.float32)
            dac = 1000.0
            while not self._quit.is_set():
                for _ in range(4):
                    self.callback(buf, 512, _Time(dac - 0.03, dac), _Status())
                    dac += 512 / 48000
                time.sleep(4 * 512 / 48000)

    out = SoundDeviceOutput(engine, "fake", stream_factory=LongBurst)
    out.start()
    try:
        time.sleep(1.0)  # an event every 50-100 ms: many planned, played, dropped
        engine.scene(Scene(name="quiet"), 0.1)  # cancels planned spots in the callback
        time.sleep(0.5)
    finally:
        out.close()
    assert engine.status()["spot_events"] > 5
    assert m.audio_thread is not None and m.audio_thread not in libc.threads
    assert any(c[0] == "munlock" for c in libc.calls)
    assert m.deferred_releases == 0


def test_cancel_builds_nothing_in_the_audio_thread():
    mixer = Mixer(RATE)
    mixer.post("add_scene", 1, 0, 1.0, {"spots:x": 1.0})
    bufs = [(s, np.ones((100, 2), np.float32)) for s in (0, 1000, 2000, 3000)]
    mixer.post("events", 1, "spots:x", bufs)
    mixer.render(np.zeros((16, 2), np.float32))
    lb = mixer.scenes[1].layers["spots:x"]
    events = lb.events
    mixer.graveyard.clear()
    mixer.post("cancel", 1, "spots:x", 1500)
    mixer.render(np.zeros((16, 2), np.float32))
    assert lb.events is events  # the same deque, popped in place
    assert [s for s, _ in lb.events] == [0, 1000]
    assert sorted(g[0] for g in mixer.graveyard if isinstance(g[0], int)) == [2000, 3000]

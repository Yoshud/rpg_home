"""Offline renders: what the listener hears, checked on the audio itself."""

import itertools
import logging
import re

import numpy as np
import pytest
import soundfile as sf

from rpghome.models.scene import Bed, ClipRef, Scene
from rpghome_engine.clips import EngineError

from .conftest import LEAD, RATE, Rig, amp_at, write_noise, write_tone

A = 0.25
CEILING = 10 ** (-1 / 20)


def assert_fade(x, start, length, f_out=None, f_in=None):
    """Equal-power curves over [start, start + length): f_out on cos, f_in on sin."""
    for frac in (0.25, 0.5, 0.75):
        t = start + frac * length
        if f_out is not None:
            expected = A * np.cos(frac * np.pi / 2)
            assert amp_at(x, f_out, t, 0.02) == pytest.approx(expected, abs=0.01), (f_out, t)
        if f_in is not None:
            expected = A * np.sin(frac * np.pi / 2)
            assert amp_at(x, f_in, t, 0.02) == pytest.approx(expected, abs=0.01), (f_in, t)


def tone_scene(data, freqs, name="s", seconds=20.0, **bed):
    clips = []
    for f in freqs:
        write_tone(data / f"t{f}.wav", f, seconds, A)
        clips.append(ClipRef(path=f"t{f}.wav"))
    bed.setdefault("loop_crossfade_s", 0.5)
    return Scene(name=name, bed=Bed(clips=tuple(clips), **bed))


def test_holds_and_crossfades_follow_the_settings(rig, data):
    """Each clip change: the old tone fades out (cos) and the new one in (sin) over
    exactly crossfade_s, and consecutive starts are hold_s apart."""
    xf = 2.0
    rig.engine.scene(tone_scene(data, [300, 500], hold_s=(4, 6), crossfade_s=xf), 0)
    rig.render(40)
    x = rig.out
    timeline = rig.engine.timeline()
    assert len(timeline) >= 7
    starts = [e["start_s"] for e in timeline]
    holds = np.diff(starts)
    assert all(4 <= h <= 6 for h in holds), holds
    assert len(set(np.round(holds, 2))) > 3  # drawn, not constant
    for prev, cur in itertools.pairwise(timeline):
        s = cur["start_s"]
        if s + xf + 0.2 > 40:
            break
        assert cur["crossfade_s"] == xf
        f_out, f_in = (int(e["clip"][1:4]) for e in (prev, cur))
        assert amp_at(x, f_out, s - 0.15) == pytest.approx(A, abs=0.005)
        assert amp_at(x, f_in, s - 0.15) < 0.003
        assert_fade(x, s, xf, f_out, f_in)
        assert amp_at(x, f_out, s + xf + 0.1) < 0.003
        assert amp_at(x, f_in, s + xf + 0.1) == pytest.approx(A, abs=0.005)


def test_crossfade_keeps_the_energy(rig, data):
    """Equal power: with uncorrelated noise the level stays flat through the crossfade."""
    for i in range(2):
        write_noise(data / f"n{i}.wav", 20, A, seed=i)
    bed = Bed(clips=(ClipRef(path="n0.wav"), ClipRef(path="n1.wav")), hold_s=(5, 5), crossfade_s=3)
    rig.engine.scene(Scene(name="noise", bed=bed), 0)
    rig.render(11)
    x = rig.out[:, 0]
    start = rig.engine.timeline()[1]["start_s"]
    rms = [
        np.sqrt(np.mean(x[round(t * RATE) : round((t + 0.25) * RATE)] ** 2))
        for t in np.arange(start - 1.5, start + 4.5, 0.25)
    ]
    db = 20 * np.log10(np.array(rms) / (A / np.sqrt(3)))
    assert np.all(np.abs(db) < 0.6), db.round(2)


def test_loop_crossfade_has_the_set_length(rig, data):
    """A 4 s clip (300 Hz, then 500 Hz) looped with a 1 s crossfade: at the seam the
    end (500 Hz) fades out under the start (300 Hz) over exactly 1 s."""
    t = np.arange(4 * RATE) / RATE
    x = np.where(t < 2, np.sin(2 * np.pi * 300 * t), np.sin(2 * np.pi * 500 * t)) * A
    sf.write(str(data / "two.wav"), x.astype(np.float32), RATE, "FLOAT")
    bed = Bed(
        clips=(ClipRef(path="two.wav"),), hold_s=(100, 100), crossfade_s=1, loop_crossfade_s=1
    )
    rig.engine.scene(Scene(name="loop", bed=bed), 0)
    rig.render(8)
    out = rig.out
    t0 = LEAD / RATE
    period = 3.0
    assert amp_at(out, 300, t0 + 1.5) == pytest.approx(A, abs=0.005)
    assert amp_at(out, 500, t0 + 2.5) == pytest.approx(A, abs=0.005)
    for seam in (t0 + period, t0 + 2 * period):
        assert_fade(out, seam, 1.0, f_out=500, f_in=300)
        assert amp_at(out, 500, seam + 1.2) < 0.003


def test_never_the_same_clip_twice_in_a_row(rig, data):
    freqs = [300, 500, 700]
    rig.engine.scene(tone_scene(data, freqs, hold_s=(1, 1.5), crossfade_s=0.3), 0)
    rig.render(90)
    x = rig.out
    timeline = rig.engine.timeline()
    assert len(timeline) > 50
    clips = [e["clip"] for e in timeline]
    assert all(a != b for a, b in itertools.pairwise(clips))
    assert len(set(clips)) == 3
    # The audio agrees: mid-hold, the timeline's clip is the only tone playing.
    for cur, nxt in itertools.pairwise(timeline):
        mid = (cur["start_s"] + cur["crossfade_s"] + nxt["start_s"]) / 2
        if mid > 89.9:
            break
        levels = {f: amp_at(x, f, mid, 0.1) for f in freqs}
        assert max(levels, key=levels.get) == int(cur["clip"][1:4])
        assert levels[int(cur["clip"][1:4])] == pytest.approx(A, abs=0.005)


def test_gains_are_applied(rig, data):
    write_tone(data / "t.wav", 400, 20, A)
    bed = Bed(
        clips=(ClipRef(path="t.wav", gain_db=-6),), hold_s=(100, 100), crossfade_s=1, gain_db=-3
    )
    rig.engine.scene(Scene(name="g", bed=bed, gain_db=-2), 0)
    rig.render(2)
    assert amp_at(rig.out, 400, 1.5) == pytest.approx(A * 10 ** (-11 / 20), rel=0.01)
    rig.engine.set("master_gain_db", -4)
    rig.engine.set("bed_gain_db", 0)
    rig.render(2)
    assert amp_at(rig.out, 400, 3.5) == pytest.approx(A * 10 ** (-12 / 20), rel=0.01)
    levels = rig.engine.status()["levels"]
    assert levels == {"master_gain_db": -4.0, "bed_gain_db": 0.0, "scene_gain_db": -2.0}


def test_limiter_never_passes_the_ceiling(rig, data):
    write_noise(data / "loud.wav", 10, 1.0)
    bed = Bed(clips=(ClipRef(path="loud.wav", gain_db=12),), hold_s=(100, 100), crossfade_s=1)
    rig.engine.scene(Scene(name="loud", bed=bed), 0)
    rig.render(6)
    rig.engine.set("master_gain_db", 20)
    rig.render(2)
    peak = np.abs(rig.out).max()
    assert peak <= CEILING
    assert peak > CEILING * 0.98  # it limits; it does not just turn everything down


def test_limiter_is_transparent_below_the_ceiling(rig, data):
    write_tone(data / "t.wav", 400, 20, 0.8)  # -1.9 dBFS: just under the ceiling
    rig.engine.scene(
        Scene(name="q", bed=Bed(clips=(ClipRef(path="t.wav"),), hold_s=(100, 100), crossfade_s=1)),
        0,
    )
    rig.render(3)
    t = np.arange(RATE) / RATE + 1.0
    out = rig.out[RATE : 2 * RATE, 0]
    delay = rig.engine.mixer.limiter.delay
    expected = 0.8 * np.sin(2 * np.pi * 400 * (t - (LEAD + delay) / RATE))
    np.testing.assert_allclose(out, expected, atol=1e-5)


def test_scene_switch_crossfades_and_the_old_scene_plans_nothing_new(rig, data):
    # Scene A would change clip 3 s after it starts, in the middle of the transition.
    a = tone_scene(data, [300, 400], "A", hold_s=(3, 3), crossfade_s=0.5)
    b = tone_scene(data, [600], "B", hold_s=(100, 100), crossfade_s=1)
    rig.engine.scene(a, 0)
    rig.render(2)
    f_a = int(rig.engine.status()["clip"][1:4])
    other = 700 - f_a
    switch = rig.now + LEAD / RATE
    rig.engine.scene(b, transition_s=4)
    rig.render(6)
    x = rig.out
    assert rig.engine.status()["scene"] == "B"
    assert_fade(x, switch, 4, f_out=f_a, f_in=600)
    assert amp_at(x, f_a, switch + 4.2) < 0.003
    assert amp_at(x, 600, switch + 4.2) == pytest.approx(A, abs=0.005)
    assert max(amp_at(x, other, t) for t in np.arange(0.2, 7.8, 0.1)) < 0.003
    assert [e["scene"] for e in rig.engine.timeline()] == ["A", "B"]


def test_stop_fades_out_to_silence(rig, data):
    rig.engine.scene(tone_scene(data, [440], hold_s=(100, 100), crossfade_s=1), 0)
    rig.render(2)
    at = rig.now + LEAD / RATE
    rig.engine.stop(fade_s=1.5)
    rig.render(3)
    x = rig.out
    assert_fade(x, at, 1.5, f_out=440)
    tail = x[round((at + 1.5) * RATE) + 200 :]
    assert np.all(tail == 0)
    s = rig.engine.status()
    assert s["scene"] is None and s["clip"] is None
    assert rig.engine.mixer.scenes == {}


def test_same_seed_same_render(data):
    scene = tone_scene(data, [300, 500, 700], hold_s=(1, 2), crossfade_s=0.5)

    def render(seed):
        r = Rig(data, seed=seed)
        r.engine.scene(scene, 0.5)
        r.render(10)
        r.engine.set("master_gain_db", -3)
        r.render(5)
        return r.out, [e["clip"] for e in r.engine.timeline()]

    x1, t1 = render(7)
    x2, t2 = render(7)
    x3, _ = render(8)
    assert np.array_equal(x1, x2) and t1 == t2
    assert not np.array_equal(x1, x3)


def test_missing_clip_is_an_error_and_playback_goes_on(rig, data):
    rig.engine.scene(tone_scene(data, [440], hold_s=(100, 100), crossfade_s=1), 0)
    rig.render(1)
    broken = Scene(
        name="broken", bed=Bed(clips=(ClipRef(path="t440.wav"), ClipRef(path="nope/missing.wav")))
    )
    with pytest.raises(EngineError, match=re.escape("nope/missing.wav")):
        rig.engine.scene(broken)
    (data / "bad.wav").write_text("not audio")
    with pytest.raises(EngineError, match=re.escape("bad.wav")):
        rig.engine.scene(Scene(name="bad", bed=Bed(clips=(ClipRef(path="bad.wav"),))))
    bad_data = {"name": "d", "bed": {"clips": [{"path": "t440.wav"}], "hold_s": [10, 5]}}
    with pytest.raises(EngineError, match=re.escape("invalid scene: bed.hold_s: Value error")):
        rig.engine.scene(bad_data)
    rig.render(1)
    assert rig.engine.status()["scene"] == "s"
    assert amp_at(rig.out, 440, 1.5) == pytest.approx(A, abs=0.005)


def test_clip_too_short_for_the_loop_crossfade(rig, data):
    write_tone(data / "short.wav", 440, 1.0)
    with pytest.raises(EngineError, match="too short"):
        rig.engine.scene(
            Scene(name="s", bed=Bed(clips=(ClipRef(path="short.wav"),), loop_crossfade_s=4))
        )


def test_other_rates_are_resampled_and_mono_goes_to_both_channels(rig, data):
    write_tone(data / "cd.wav", 441, 10, A, rate=44100)
    rig.engine.scene(
        Scene(name="cd", bed=Bed(clips=(ClipRef(path="cd.wav"),), hold_s=(50, 50), crossfade_s=1)),
        0,
    )
    rig.render(3)
    x = rig.out
    assert amp_at(x, 441, 2.0) == pytest.approx(A, abs=0.003)
    assert np.array_equal(x[:, 0], x[:, 1])


def test_stereo_clips_keep_their_channels(rig, data):
    t = np.arange(10 * RATE) / RATE
    st = np.stack([np.sin(2 * np.pi * 300 * t), np.sin(2 * np.pi * 500 * t)], axis=1) * A
    sf.write(str(data / "st.wav"), st.astype(np.float32), RATE, "FLOAT")
    rig.engine.scene(
        Scene(name="st", bed=Bed(clips=(ClipRef(path="st.wav"),), hold_s=(50, 50), crossfade_s=1)),
        0,
    )
    rig.render(3)
    right = rig.out[:, ::-1]
    assert amp_at(rig.out, 300, 2.0) == pytest.approx(A, abs=0.003)
    assert amp_at(right, 500, 2.0) == pytest.approx(A, abs=0.003)
    assert amp_at(right, 300, 2.0) < 0.003


def test_status_reports_the_hold_and_measurements(rig, data):
    rig.engine.scene(tone_scene(data, [300, 500], hold_s=(5, 6), crossfade_s=1), 0)
    rig.render(3)
    s = rig.engine.status()
    assert s["scene"] == "s"
    assert s["clip"] in ("t300.wav", "t500.wav")
    assert 5 <= s["hold_s"] <= 6
    assert s["hold_elapsed_s"] == pytest.approx(3 - LEAD / RATE, abs=0.01)
    assert s["underruns"] == 0 and s["starved_blocks"] == 0 and s["late_events"] == 0
    assert s["planning_cycles"] >= 15
    for k in ("control", "callback"):
        t = s["timings_ms"][k]
        assert t["count"] > 0 and 0 <= t["p50"] <= t["p95"] <= t["max"]


def test_set_rejects_unknown_names_and_bad_values(rig):
    with pytest.raises(EngineError, match="unknown level"):
        rig.engine.set("volume", 0)
    with pytest.raises(EngineError, match="within"):
        rig.engine.set("master_gain_db", 100)
    with pytest.raises(EngineError, match="number"):
        rig.engine.set("master_gain_db", "loud")


def test_bed_gain_on_a_scene_without_bed_is_ignored_not_a_crash(rig, caplog):
    rig.engine.scene({"name": "quiet"}, 0)
    rig.render(0.5)
    with caplog.at_level(logging.WARNING, logger="rpghome.engine"):
        rig.engine.set("bed_gain_db", -6)
        rig.render(0.5)  # used to raise KeyError in the audio thread
    assert rig.engine.mixer.bad_messages == 0
    assert "set bed_gain_db = -6.0 dB ignored: the scene has no bed" in caplog.text
    assert rig.engine.status()["levels"]["bed_gain_db"] is None


def test_bed_gain_after_stop_is_ignored_and_logged(rig, data, caplog):
    rig.engine.scene(tone_scene(data, [440], hold_s=(100, 100), crossfade_s=1), 0)
    rig.render(1)
    rig.engine.stop(0.5)
    rig.render(1)
    with caplog.at_level(logging.INFO, logger="rpghome.engine"):
        rig.engine.set("bed_gain_db", -6)
        rig.render(0.2)
    assert "set bed_gain_db = -6.0 dB ignored: no scene" in caplog.text
    assert "set bed_gain_db = -6.0 dB\n" not in caplog.text + "\n"
    # The next scene starts at its own bed level, not the ignored one.
    rig.engine.scene(tone_scene(data, [440], hold_s=(100, 100), crossfade_s=1), 0)
    rig.render(1)
    assert amp_at(rig.out, 440, rig.now - 0.3) == pytest.approx(A, abs=0.005)


def test_one_clip_pool_loops_without_hold_crossfades(rig, data):
    """No crossfade of the clip into itself: one timeline entry, a flat level."""
    rig.engine.scene(tone_scene(data, [440], hold_s=(2, 3), crossfade_s=1.5), 0)
    rig.render(15)  # five holds' worth; the 20 s clip has no loop seam before 19.5 s
    timeline = rig.engine.timeline()
    assert len(timeline) == 1 and timeline[0]["hold_s"] is None
    levels = [amp_at(rig.out, 440, t, 0.05) for t in np.arange(0.2, 14.8, 0.1)]
    assert max(levels) == pytest.approx(A, abs=0.002)
    assert min(levels) == pytest.approx(A, abs=0.002)
    s = rig.engine.status()
    assert s["hold_s"] is None and s["hold_elapsed_s"] == pytest.approx(15 - LEAD / RATE, abs=0.01)


def test_status_reads_do_not_reset_the_meters(rig, data):
    """Any number of readers: reading status() changes nothing, and the 'recent'
    values follow the audio (last ~2 s) while the maxima cover the whole run."""
    write_noise(data / "loud.wav", 10, 1.0)
    bed = Bed(clips=(ClipRef(path="loud.wav", gain_db=6),), hold_s=(100, 100), crossfade_s=1)
    rig.engine.scene(Scene(name="loud", bed=bed), 0)
    rig.render(3)
    a, b = rig.engine.status(), rig.engine.status()
    keys = ("output_peak_dbfs", "output_peak_max_dbfs", "limiter_gain_db", "limiter_min_gain_db")
    assert [a[k] for k in keys] == [b[k] for k in keys]
    assert a["output_peak_dbfs"] == pytest.approx(-1.0, abs=0.01)
    assert a["limiter_gain_db"] < -6
    rig.engine.stop(0.1)
    for _ in range(30):
        rig.render(0.1)
        rig.engine.status()  # a poller, like the status log line or the bus
    s = rig.engine.status()
    assert s["output_peak_dbfs"] is None  # silence for the last 2 s
    assert s["limiter_gain_db"] > -0.1  # only the end of the release is left
    assert s["output_peak_max_dbfs"] == a["output_peak_max_dbfs"]
    assert s["limiter_min_gain_db"] <= a["limiter_min_gain_db"]
    assert s["seconds_rendered"] == pytest.approx(6.0)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_non_finite_clip_is_rejected_and_playback_goes_on(rig, data, bad):
    rig.engine.scene(tone_scene(data, [440], hold_s=(100, 100), crossfade_s=1), 0)
    rig.render(1)
    x = (0.25 * np.sin(np.arange(48000 * 5) * 0.05)).astype(np.float32)
    x[1234] = bad
    sf.write(str(data / "bad.wav"), x, RATE, "FLOAT")
    write_tone(data / "good.wav", 300, 5)
    pool = Bed(
        clips=(ClipRef(path="good.wav"), ClipRef(path="bad.wav")),
        hold_s=(2, 2),
        crossfade_s=1,
        loop_crossfade_s=0.5,
    )
    with pytest.raises(EngineError, match=r"bad\.wav has 1 non-finite samples \(NaN or inf\)"):
        rig.engine.scene(Scene(name="bad", bed=pool))
    rig.render(1)
    assert rig.engine.status()["scene"] == "s"
    assert np.isfinite(rig.out).all()
    assert amp_at(rig.out, 440, 1.5) == pytest.approx(A, abs=0.005)

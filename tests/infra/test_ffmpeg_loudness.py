import pytest

from rpghome.infra import ffmpeg

# A 1 kHz sine with peak amplitude A reads 20*log10(A) - 3.01 LUFS in mono (K-weighting is ~0 dB
# at 1 kHz); a second identical channel adds 3.01 LU.


def test_probe(needs_ffmpeg, sine, tmp_path):
    info = ffmpeg.probe(sine(tmp_path / "a.wav", 2.5, channels=2))
    assert info == ffmpeg.AudioInfo(duration_s=2.5, channels=2, sample_rate=48000)


def test_steady_sine(needs_ffmpeg, sine, tmp_path):
    loud = ffmpeg.loudness(sine(tmp_path / "a.wav", 5, 0.1))
    assert loud.integrated_lufs == pytest.approx(-23.0, abs=0.2)
    assert loud.bed_lufs == pytest.approx(-23.0, abs=0.2)
    assert loud.max_short_term_lufs == pytest.approx(-23.0, abs=0.2)
    assert loud.true_peak_dbtp == pytest.approx(-20.0, abs=0.2)
    stereo = ffmpeg.loudness(sine(tmp_path / "b.wav", 5, 0.1, channels=2))
    assert stereo.integrated_lufs == pytest.approx(-20.0, abs=0.2)


def test_bed_level_ignores_loud_events(needs_ffmpeg, sine, tmp_path):
    # 8 s quiet bed (-43 LUFS) with a 2 s loud burst (-9 LUFS) at the end.
    loud = ffmpeg.loudness(sine(tmp_path / "a.wav", 0, parts=[(8, 0.01), (2, 0.5)]))
    assert loud.bed_lufs == pytest.approx(-43.0, abs=0.3)
    # The best 3 s window holds 2 s of the burst: -9 + 10*log10(2/3).
    assert loud.max_short_term_lufs == pytest.approx(-10.8, abs=0.3)
    assert loud.true_peak_dbtp == pytest.approx(-6.0, abs=0.3)
    # Integrated loudness gates the quiet part out (relative gate, -10 LU).
    assert loud.integrated_lufs == pytest.approx(-9.0, abs=0.6)


def test_short_clip_gets_a_short_term_level(needs_ffmpeg, sine, tmp_path):
    # 1 s at -23 LUFS, read through a 3 s window: -23 - 10*log10(3) = -27.8.
    loud = ffmpeg.loudness(sine(tmp_path / "a.wav", 1, 0.1))
    assert loud.max_short_term_lufs == pytest.approx(-27.8, abs=0.3)


def test_silence(needs_ffmpeg, sine, tmp_path):
    loud = ffmpeg.loudness(sine(tmp_path / "a.wav", 2, 0.0))
    assert loud.bed_lufs is None
    assert loud.max_short_term_lufs is None
    assert loud.integrated_lufs is None
    assert loud.true_peak_dbtp is None


def test_errors(needs_ffmpeg, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_text("not audio")
    with pytest.raises(ffmpeg.FfmpegError):
        ffmpeg.loudness(bad)
    with pytest.raises(ffmpeg.FfmpegError):
        ffmpeg.probe(bad)


def test_percentile_matches_numpy_linear():
    # numpy.percentile([1, 2, 3, 4, 5], 20) == 1.8
    assert ffmpeg.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 20) == pytest.approx(1.8)
    assert ffmpeg.percentile([7.0], 20) == 7.0


def test_true_peak_of_quiet_signals(needs_ffmpeg, sine, tmp_path):
    # The 3-decimal linear metadata reads 0.000 here (None); the summary keeps the precision.
    # 16-bit samples: 0.001 -> 33 LSB (-59.9 dBFS), 0.000178 -> 6 LSB (-74.7 dBFS).
    quiet = ffmpeg.loudness(sine(tmp_path / "a.wav", 5, 0.001))
    assert quiet.true_peak_dbtp == pytest.approx(-59.9, abs=0.3)
    assert quiet.integrated_lufs == pytest.approx(-62.9, abs=0.3)
    very_quiet = ffmpeg.loudness(sine(tmp_path / "b.wav", 5, 0.000178))
    assert very_quiet.true_peak_dbtp == pytest.approx(-74.7, abs=0.4)


def test_bed_level_skips_warm_up_and_keeps_quiet_blocks(needs_ffmpeg, sine, tmp_path):
    # 1.5 s: 15 momentary values, the first 3 at -120.7 (window not filled yet). Without the
    # warm-up skip the 20th percentile would fall to about -50; with it, it is the sine's -23.
    # The clip is padded for the short-term window, but padded frames are not bed values.
    loud = ffmpeg.loudness(sine(tmp_path / "a.wav", 1.5, 0.1))
    assert loud.bed_lufs == pytest.approx(-23.0, abs=0.2)
    # Quiet blocks are not gated (loop_demo.py takes every value): 6 s at -83 LUFS and 4 s at
    # -23 LUFS give a bed level near -83, not -23.
    mixed = ffmpeg.loudness(sine(tmp_path / "b.wav", 0, parts=[(6, 0.0001), (4, 0.1)]))
    assert mixed.bed_lufs < -75

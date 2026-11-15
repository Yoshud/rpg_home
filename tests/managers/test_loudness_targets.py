from rpghome.managers import loudness
from rpghome.models.clip import Kind, Loudness


def _l(**kw):
    base = dict(
        integrated_lufs=-20.0, bed_lufs=-25.0, max_short_term_lufs=-15.0, true_peak_dbtp=-10.0
    )
    return Loudness(**{**base, **kw})


def test_each_kind_uses_its_measure():
    assert loudness.gain_db(Kind.BED, _l()) == -31.0 + 25.0
    assert loudness.gain_db(Kind.FX, _l()) == -22.0 + 15.0
    assert loudness.gain_db(Kind.SPOT, _l()) == -42.0 + 15.0
    assert loudness.gain_db(Kind.MUSIC, _l()) == -28.0 + 20.0


def test_true_peak_ceiling():
    # +11 dB would bring the bed to target, but the peak may rise only to -1 dBTP.
    assert loudness.gain_db(Kind.BED, _l(bed_lufs=-42.0, true_peak_dbtp=-5.0)) == 4.0


def test_unknown_level():
    assert loudness.gain_db(Kind.BED, None) is None
    assert loudness.gain_db(Kind.BED, _l(bed_lufs=None)) is None
    assert loudness.gain_db(Kind.FX, _l(true_peak_dbtp=None)) == -7.0

"""Target levels per clip kind and the gain that brings a clip there. Files are never modified:
the gain is stored in the catalog and applied at playback.

Where the values come from (sound-gen, ~/claude_code/tasks/sound-gen):
- bed, -31 LUFS bed level (20th percentile of momentary loudness): loop_demo.py version C, the
  ambience the user liked best. It matched the cave beds at their median bed level (-23.2) and
  then lowered them all by 8.0 dB so the loudest drip stays under -1 dBFS: -31.2.
- spot, -42 LUFS max short-term: spots_demo.py over that bed. It normalised each spot to a -8 dBFS
  peak and played it at -10..-2 dB (mean -6); measured on the sound-gen spot files that puts a
  spot at a median of -41.6 LUFS max short-term, about 10 LU under the bed.
- fx, -22 LUFS max short-term: blind.py, round 1. One-shots were played at -20 LUFS integrated, or
  lower when the true peak would pass -1 dBTP; measured on the MOSS one-shots that is a median of
  -21.8 LUFS max short-term.
- music, -28 LUFS integrated: a placeholder, there was no music test. Revisit with real music.
- ceiling, -1 dBTP for every kind (blind.py's CEIL_DBTP): the gain never lifts the true peak
  above it.
"""

from dataclasses import dataclass

from rpghome.models.clip import Kind, Loudness


@dataclass(frozen=True)
class Target:
    measure: str  # the Loudness field the target applies to
    lufs: float


TARGETS: dict[Kind, Target] = {
    Kind.BED: Target("bed_lufs", -31.0),
    Kind.SPOT: Target("max_short_term_lufs", -42.0),
    Kind.FX: Target("max_short_term_lufs", -22.0),
    Kind.MUSIC: Target("integrated_lufs", -28.0),
}
CEILING_DBTP = -1.0


def gain_db(kind: Kind, loudness: Loudness | None) -> float | None:
    """Gain to the kind's target, capped so the true peak stays at or under the ceiling.
    None when the clip has no level to measure (silence)."""
    if loudness is None:
        return None
    target = TARGETS[kind]
    level = getattr(loudness, target.measure)
    if level is None:
        return None
    gain = target.lufs - level
    if loudness.true_peak_dbtp is not None:
        gain = min(gain, CEILING_DBTP - loudness.true_peak_dbtp)
    return round(gain, 1) + 0.0  # + 0.0 turns -0.0 into 0.0

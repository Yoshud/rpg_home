"""ffmpeg / ffprobe: stream info and EBU R128 loudness of an audio file. Files are only read.

The loudness numbers follow sound-gen's `blind.py` (`measure`) and `loop_demo.py` (`bed_matched`):
one `ebur128=peak=true:metadata=1` pass prints momentary (M, 400 ms) and short-term (S, 3 s)
loudness every 100 ms, and a summary with the integrated loudness and the true peak.
"""

import json
import math
import re
import shutil
import statistics
import subprocess
from pathlib import Path

from rpghome.core.errors import RpgHomeError
from rpghome.models.clip import AudioInfo, Loudness

FALLBACK_DIR = Path("/opt/homebrew/bin")
# The first values are measured on a partly filled 400 ms window: loop_demo.py skips 4 of them.
WARM_UP_FRAMES = 4
# ffmpeg reports -120.7 LUFS for a window with no energy at all; a level at or under this is
# "no signal" (None). It is not a gate: quiet blocks still count, as in loop_demo.py.
NO_SIGNAL_LUFS = -120.0
# The short-term window is 3 s, so shorter clips are padded with silence to get any S value.
MIN_SECONDS = 3.5
BED_PERCENTILE = 20


class FfmpegError(RpgHomeError):
    pass


def tool(name: str) -> str:
    found = shutil.which(name) or (
        str(FALLBACK_DIR / name) if (FALLBACK_DIR / name).exists() else None
    )
    if not found:
        raise FfmpegError(f"{name} not found on PATH or in {FALLBACK_DIR}")
    return found


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode:
        tail = "\n".join(r.stderr.strip().splitlines()[-5:])
        raise FfmpegError(f"{Path(args[0]).name} failed on {args[-1]!r}:\n{tail}")
    return r


def probe(path: Path) -> AudioInfo:
    """Duration, channels and sample rate of the first audio stream."""
    r = _run(
        [
            tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels,sample_rate:format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    data = json.loads(r.stdout)
    if not data.get("streams"):
        raise FfmpegError(f"no audio stream in {path}")
    stream = data["streams"][0]
    return AudioInfo(
        duration_s=round(float(data["format"]["duration"]), 3),
        channels=int(stream["channels"]),
        sample_rate=int(stream["sample_rate"]),
    )


def _momentary_and_short_term(text: str, duration_s: float) -> tuple[list[float], list[float]]:
    """M and S per 100 ms frame from `ametadata=print`. M is kept only for frames inside the
    file, not the silence padded after it; S for all frames (the padding is what gives a
    short clip its 3 s window)."""
    momentary, short_term, t = [], [], 0.0
    for line in text.splitlines():
        if line.startswith("frame:"):
            m = re.search(r"pts_time:(\S+)", line)
            t = float(m.group(1)) if m else t
        elif line.startswith("lavfi.r128.M="):
            if t < duration_s - 1e-6:
                momentary.append(float(line.partition("=")[2]))
        elif line.startswith("lavfi.r128.S="):
            short_term.append(float(line.partition("=")[2]))  # float() parses "-inf" too
    return momentary, short_term


def _summary(stderr: str, label: str, unit: str) -> float | None:
    """A value from ebur128's summary at the end of stderr; None for -inf."""
    summary = stderr[stderr.rfind("Summary:") :]
    m = re.search(rf"{label}:\s+(-?[\d.]+|-inf|inf|nan)\s+{unit}", summary)
    if not m:
        raise FfmpegError(f"no {label} in the ebur128 summary")
    return _level(float(m.group(1)))


def _level(x: float) -> float | None:
    """Rounded to 0.1 dB; None for no signal (ffmpeg's floor is -120.7 for zero energy)."""
    return None if not math.isfinite(x) or x <= NO_SIGNAL_LUFS else round(x, 1) + 0.0


def loudness(path: Path) -> Loudness:
    """Measure a file, as loop_demo.py and blind.py did:
    - bed level: the 20th percentile of all momentary values after the 4 warm-up values;
    - fx/spot level: the maximum short-term loudness;
    - integrated loudness and true peak from ebur128's summary (true peak at 0.1 dB, from the
      full-precision value, not the 3-decimal linear metadata)."""
    duration = probe(path).duration_s
    pad = f"apad=whole_dur={MIN_SECONDS}," if duration < MIN_SECONDS else ""
    r = _run(
        [
            tool("ffmpeg"),
            "-nostats",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"{pad}ebur128=peak=true:metadata=1,ametadata=print:file=-",
            "-f",
            "null",
            "-",
        ]
    )
    momentary, short_term = _momentary_and_short_term(r.stdout, duration)
    momentary = momentary[WARM_UP_FRAMES:]
    integrated = _summary(r.stderr, "I", "LUFS")
    return Loudness(
        # ffmpeg prints -70.0 when every block is under the absolute gate: no level.
        integrated_lufs=integrated if integrated is not None and integrated > -70 else None,
        bed_lufs=_level(percentile(momentary, BED_PERCENTILE)) if momentary else None,
        max_short_term_lufs=_level(max(short_term)) if short_term else None,
        true_peak_dbtp=_summary(r.stderr, "Peak", "dBFS"),
    )


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile, the same as numpy's default (loop_demo.py used numpy)."""
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[round(p) - 1]

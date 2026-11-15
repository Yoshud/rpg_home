"""Synthetic test audio and a miniature sound-gen folder."""

import json
import math
import struct
import wave
from pathlib import Path

RATE = 48000


def write_sine(
    path: Path,
    seconds: float,
    amplitude: float = 0.1,
    freq: float = 1000.0,
    channels: int = 1,
    parts: list[tuple[float, float]] | None = None,
) -> Path:
    """16-bit WAV of a sine. `parts` = [(seconds, amplitude), ...] overrides seconds/amplitude."""
    parts = parts or [(seconds, amplitude)]
    frames = bytearray()
    n = 0
    for secs, amp in parts:
        for _ in range(int(secs * RATE)):
            v = round(amp * 32767 * math.sin(2 * math.pi * freq * n / RATE))
            frames += struct.pack("<h", v) * channels
            n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    return path


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


CAVE_PROMPT = "Cave ambience with dripping water and a distant waterfall."
CAVE_DETAILED = "Ambience of a large dark cave: water drops with a hollow echo."
SWORD_PROMPT = "Two swords clash in a fight."
EPI_CAVE = "58fa8b2d-0000-0000-0000-000000000001"
EPI_SWORD = "5e826354-0000-0000-0000-000000000002"
EPI_PEBBLES = "14e3dede-0000-0000-0000-000000000003"


def make_sound_gen(root: Path) -> Path:
    """A small sound-gen folder with the same file formats as the real one."""
    s, r = root / "samples", root / "results"
    moss = [
        ("cave_ambience", "short", CAVE_PROMPT),
        ("cave_ambience", "detailed", CAVE_DETAILED),
        ("sword_clash", "short", SWORD_PROMPT),
    ]
    rows = []
    for line, variant, prompt in moss:
        name = f"{line}__{variant}"
        rows.append(
            {
                "model": "moss",
                "name": name,
                "line": line,
                "variant": variant,
                "prompt": prompt,
                "seconds": 4.0,
                "seed": 42,
                "steps": 100,
                "cfg": 4.0,
            }
        )
        write_sine(s / "moss" / f"{name}.wav", 4 if line == "cave_ambience" else 2, 0.05)
    _jsonl(r / "moss.jsonl", rows)

    rows = []
    for cond, neg, amp in (("default", "", 0.02), ("neg", "music, hiss", 0.2)):
        name = f"cave_ambience__{cond}__s1"
        rows.append(
            {
                "model": "moss",
                "name": name,
                "line": "cave_ambience",
                "condition": cond,
                "prompt_field": "prompt",
                "prompt": CAVE_DETAILED,
                "negative": neg,
                "seed": 1,
                "steps": 100,
                "cfg": 4.0,
                "seconds": 5.0,
            }
        )
        write_sine(s / "moss_grid" / f"{name}.wav", 5, amp)
    _jsonl(r / "moss_grid.jsonl", rows)

    _jsonl(
        r / "spots_moss.jsonl",
        [
            {"name": "drop__s1", "prompt": "A single water drop falls in a cave.", "seed": 1},
            {"name": "rockfall__s1", "prompt": "A distant rock cracks in a cave.", "seed": 1},
        ],
    )
    write_sine(s / "spots_moss" / "drop__s1.wav", 1, 0.3)
    write_sine(s / "spots_moss" / "rockfall__s1.wav", 2, 0.3)

    index = {
        "cave_ambience": {
            "query": "cave",
            "pick": {
                "id": EPI_CAVE,
                "title": "Ambience, Underground, Cave, Water, Dripping",
                "seconds": 6.0,
                "tags": "underground",
            },
            "others": [],
        },
        "sword_clash": {
            "query": "sword",
            "pick": {
                "id": EPI_SWORD,
                "title": "Weapons, Sword, Rapier, Duel, Impact",
                "seconds": 2.0,
                "tags": "sword",
            },
            "others": [],
        },
    }
    (s / "epidemic").mkdir(parents=True, exist_ok=True)
    (s / "epidemic" / "index.json").write_text(json.dumps(index))
    write_sine(s / "epidemic" / "cave_ambience.wav", 6, 0.03, channels=2)
    write_sine(s / "epidemic" / "sword_clash.wav", 2, 0.5, channels=2)
    _jsonl(
        s / "spots_epidemic" / "index.jsonl",
        [{"name": "pebbles_a", "id": EPI_PEBBLES, "title": "Rocks, Impact, Stones Falling"}],
    )
    write_sine(s / "spots_epidemic" / "pebbles_a.wav", 1.5, 0.4, channels=2)

    # Round 1: cave Epidemic #1 best, MOSS short #2 middle, MOSS detailed #3 worst;
    # sword: MOSS short #4 best, Epidemic #5 worst ("-" is not used here).
    (r / "blind_key.json").write_text(
        json.dumps(
            {
                "1": {"item": "cave_ambience", "source": "epidemic", "epidemic_start_s": 10.0},
                "2": {"item": "cave_ambience", "source": "moss_short"},
                "3": {"item": "cave_ambience", "source": "moss_detailed"},
                "4": {"item": "sword_clash", "source": "moss_short"},
                "5": {"item": "sword_clash", "source": "epidemic", "epidemic_start_s": 0.0},
            }
        )
    )
    (r / "blind_votes.txt").write_text(
        "1 cave_ambience: najlepsze 1, najgorsze 3, uwagi: 1 jest super\n"
        "2 sword_clash: najlepsze 4, najgorsze 5, uwagi: 4 filmowe\n"
    )
    # Round 2: cave default s1 #1 = 5, neg s1 #2 unrated, Epidemic #3 = 2.
    (r / "blind_grid_key.json").write_text(
        json.dumps(
            {
                "1": {"item": "cave_ambience", "source": "default__s1"},
                "2": {"item": "cave_ambience", "source": "neg__s1"},
                "3": {"item": "cave_ambience", "source": "epidemic", "epidemic_start_s": 10.0},
            }
        )
    )
    (r / "blind_grid_votes.txt").write_text("cave_ambience: 1=5 2=- 3=2 | uwagi: piątki są mocne\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "sfx.json").write_text(
        json.dumps(
            [
                {"id": "cave_ambience", "use": "rpg"},
                {"id": "sword_clash", "use": "rpg"},
            ]
        )
    )
    return root

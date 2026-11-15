"""The clip library: import of the sound-gen test clips, search and pool summaries.

`import_sound_gen` copies the clips of ~/claude_code/tasks/sound-gen into the data folder (the
originals are only read), measures them, maps names, prompts, Epidemic titles and the user's
blind-test ratings to catalog fields, and stores them. Running it again changes nothing: ids come
from the source, unchanged files are not copied or measured again, a data-folder file that differs
from its recorded hash is never overwritten, and fields the user edited are kept (see `_merge`).

Everything that touches the catalog is async. Blocking work (reading sound-gen, hashing, copying,
ffmpeg, waiting for the import lock) runs in worker threads (`asyncio.to_thread`), so a server's
event loop is never blocked for long.
"""

import asyncio
import fcntl
import filecmp
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rpghome.core.errors import RpgHomeError
from rpghome.infra import ffmpeg
from rpghome.managers import loudness as levels
from rpghome.models import tags as vocabulary
from rpghome.models.clip import (
    USER_FIELDS,
    Clip,
    EpidemicDetails,
    ImportedFields,
    Kind,
    Loudness,
    MossDetails,
    Perspective,
    Source,
    Status,
    license_for,
    status_for,
)
from rpghome.repositories.catalog import Catalog

DEFAULT_SOUND_GEN = Path("~/claude_code/tasks/sound-gen")

# Where the copies go, relative to the data folder.
MOSS_DIRS = {
    "round1": "generated/moss/sound-gen/round1",
    "round2": "generated/moss/sound-gen/round2",
    "spots": "generated/moss/sound-gen/spots",
}
EPIDEMIC_DIRS = {"refs": "epidemic/sound-gen/refs", "spots": "epidemic/sound-gen/spots"}

POOLS = {
    "cave.bed": "cave ambience beds",
    "cave.drops": "cave water: single drops and slow drips (spots)",
    "cave.stones": "cave stones: pebbles and rockfalls (spots)",
    "tavern.bed": "tavern ambience beds",
    "storm.rain": "rain beds; the sound-gen storm clips also carry thunder",
    "storm.thunder": "separate thunder hits; empty: sound-gen has none, its storm clips mix rain "
    "and thunder in one bed",
    "swords.clash": "sword clashes (fx)",
}

# Line name (the part before `__` in the sound-gen file names) -> kind and pools.
LINES: dict[str, tuple[Kind, tuple[str, ...]]] = {
    "cave_ambience": (Kind.BED, ("cave.bed",)),
    "tavern_ambience": (Kind.BED, ("tavern.bed",)),
    "storm_rain": (Kind.BED, ("storm.rain",)),
    "forest_fire": (Kind.BED, ()),
    "sword_clash": (Kind.FX, ("swords.clash",)),
    "shiba_yelp": (Kind.FX, ()),
    "ravens_takeoff": (Kind.FX, ()),
    "laser_beam": (Kind.FX, ()),
    "door_creak": (Kind.FX, ()),
    "dragon_roar": (Kind.FX, ()),
    "drop": (Kind.SPOT, ("cave.drops",)),
    "drips": (Kind.SPOT, ("cave.drops",)),
    "pebbles": (Kind.SPOT, ("cave.stones",)),
    "rockfall": (Kind.SPOT, ("cave.stones",)),
}

# Words in prompts, titles and line names -> category tags (only where the mapping is obvious).
KEYWORDS: dict[str, tuple[str, ...]] = {}
for _words, _tags in [
    ("cave cavern caves underground", ("place:cave",)),
    ("tavern pub bar", ("place:tavern",)),
    ("forest trees", ("place:forest",)),
    ("dungeon", ("place:dungeon",)),
    ("courtyard", ("place:courtyard",)),
    ("valley mountain mountains", ("place:mountains",)),
    ("city london", ("place:city",)),
    ("water drop drops drip drips dripping puddle puddles waterfall flowing", ("source:water",)),
    ("rain rainfall", ("source:rain", "weather:rain")),
    ("thunder thunderstorm lightning", ("source:thunder", "weather:storm")),
    ("storm", ("weather:storm",)),
    ("wind", ("source:wind",)),
    ("fire flames burning bonfire", ("source:fire",)),
    ("stone stones rock rocks pebbles rockfall gravel debris", ("source:stone",)),
    ("sword swords longswords rapier swordfight blades", ("source:sword",)),
    ("crowd talking laughing", ("source:crowd",)),
    ("busy crowded", ("mood:busy",)),
    ("gentle calm", ("mood:calm",)),
    ("dog shiba", ("source:dog",)),
    ("fox", ("source:fox",)),
    ("ravens raven crow crows birds", ("source:bird",)),
    ("dragon", ("source:dragon",)),
    ("door doors hinges", ("source:door",)),
    ("laser lasers", ("source:laser",)),
]:
    for _w in _words.split():
        KEYWORDS[_w] = KEYWORDS.get(_w, ()) + _tags

# Recordings that already carry their own acoustics, and ones that are dry.
_WET_WORDS = {"echo", "echoing", "reverberant", "reverb", "cave", "cavern", "underground"}
# spots_demo.py: "All spots go through the same synthetic cave reverb, because Epidemic's stones
# are dry recordings."
_DRY_EPIDEMIC_SPOTS = {"pebbles", "rockfall"}

# The user's verdict after the spot demo (NEXT.md, 2026-11-11, Claude's English paraphrase).
SPOT_DEMO_NOTES = {
    ("moss", None): "Spot demo (NEXT.md, 2026-11-11, paraphrased): MOSS spots fit better overall "
    "and have more atmosphere.",
    ("moss", "rockfall"): "Spot demo (NEXT.md, 2026-11-11, paraphrased): the MOSS rockfall is far "
    "too close; spots need a distance setting (level, low-pass, more reverb).",
    ("epidemic", "drop"): "Spot demo (NEXT.md, 2026-11-11, paraphrased): Epidemic's drops are "
    "noticeably better, their realism adds a lot.",
    ("epidemic", "pebbles"): "Spot demo (NEXT.md, 2026-11-11, paraphrased): Epidemic's small "
    "pebbles are noticeably better, their realism adds a lot.",
}

# Round 1 had best/worst picks among three clips per item; mapped to ratings as:
ROUND1_BEST_RATING = 4
ROUND1_WORST_RATING = 2

# Pipeline defaults of MOSS-SoundEffect (NEXT.md, "pipeline's knobs"); run_spots.py used them.
MOSS_DEFAULT_STEPS = 100
MOSS_DEFAULT_CFG = 4.0


class LibraryError(RpgHomeError):
    pass


@dataclass
class _Item:
    """One clip as the import sees it, before it is copied and measured."""

    id: str
    line: str
    src: Path
    path: str
    kind: Kind
    title: str
    source: Source
    details: MossDetails | EpidemicDetails
    tags: set[str]
    pools: tuple[str, ...]
    perspective: Perspective | None = None
    dry: bool | None = None
    rating: int | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # data-folder files left alone, with reason
    skipped: list[str] = field(default_factory=list)  # unusable metadata rows, with reason


@dataclass(frozen=True)
class PoolSummary:
    name: str
    description: str
    clips: int
    kinds: tuple[str, ...]
    accepted: int


# -- reading sound-gen ------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _tags_from(text: str) -> set[str]:
    return {t for w in _words(text) for t in KEYWORDS.get(w, ())}


def _free_tags(names: list[str], covered: set[str]) -> set[str]:
    """Plain tags from names, leaving out those a category tag already says (`cave`)."""
    values = {t.partition(":")[2] for t in covered}
    return {s for s in (vocabulary.slug(n) for n in names) if s and s not in values}


def _dry(text: str) -> bool | None:
    words = set(_words(text))
    if "dry" in words:
        return True
    if words & _WET_WORDS:
        return False
    return None


def _perspective(text: str) -> Perspective | None:
    t = text.lower()
    if "close up" in t or "up close" in t or "close to" in t:
        return Perspective.CLOSE
    return None


def _pretty(line: str) -> str:
    return line.replace("_", " ").capitalize()


# The spot lines all come from tests/spots.json: "Spot one-shots for the cave loop demo".
LINE_TAGS = {line: ("place:cave",) for line in ("drop", "drips", "pebbles", "rockfall")}


def _line_tags(line: str, text: str, extra: list[str]) -> set[str]:
    tags = _tags_from(f"{line.replace('_', ' ')} {text}") | set(LINE_TAGS.get(line, ()))
    return tags | _free_tags(line.split("_") + extra, tags)


def _use_by_line(root: Path) -> dict[str, str]:
    path = root / "tests" / "sfx.json"
    if not path.exists():
        return {}
    return {t["id"]: t["use"] for t in json.loads(path.read_text()) if t.get("use")}


# Names from the sound-gen metadata become file and id parts: no separators, no "..".
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _safe(name: Any) -> str:
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name) or ".." in name:
        raise LibraryError(
            f"unsafe name {name!r}: only letters, digits, '_', '-' and '.', no '/' or '..'"
        )
    return name


def _line(line: str) -> tuple[Kind, tuple[str, ...]]:
    if line not in LINES:
        raise LibraryError(f"unknown sound-gen line {line!r}: add it to library.LINES")
    return LINES[line]


def _moss_item(
    batch: str, row: dict[str, Any], line: str, src: Path, title: str, condition: str
) -> _Item:
    kind, pools = _line(line)
    prompt = row["prompt"]
    return _Item(
        id=f"moss:sound-gen/{batch}/{row['name']}",
        line=line,
        src=src,
        path=f"{MOSS_DIRS[batch]}/{row['name']}{src.suffix}",
        kind=kind,
        title=title,
        source=Source.MOSS,
        details=MossDetails(
            prompt=prompt,
            seed=int(row["seed"]),
            steps=int(row.get("steps", MOSS_DEFAULT_STEPS)),
            cfg=float(row.get("cfg", MOSS_DEFAULT_CFG)),
            condition=condition,
            negative_prompt=row.get("negative", ""),
        ),
        tags=_line_tags(line, prompt, []) | {"batch:sound-gen"},
        pools=pools,
        perspective=_perspective(prompt),
        dry=_dry(prompt),
    )


def _epidemic_item(
    folder: str, name: str, line: str, src: Path, epidemic_id: str, title: str, tags: str
) -> _Item:
    kind, pools = _line(line)
    # Epidemic's category and subcategory (the first two title parts) and its tags, as free tags
    # unless a keyword already maps them ("Rocks" -> source:stone).
    segments = [s.strip() for s in title.split(",")][:2] + ([tags] if tags else [])
    extra = [s for s in segments if vocabulary.slug(s) not in KEYWORDS]
    item = _Item(
        id=f"epidemic:{epidemic_id}",
        line=line,
        src=src,
        path=f"{EPIDEMIC_DIRS[folder]}/{name}{src.suffix}",
        kind=kind,
        title=title,
        source=Source.EPIDEMIC,
        details=EpidemicDetails(id=epidemic_id, title=title),
        tags=_line_tags(line, title, extra) | {"batch:sound-gen"},
        pools=pools,
        perspective=_perspective(title),
        dry=_dry(title),
    )
    if folder == "spots" and line in _DRY_EPIDEMIC_SPOTS:
        item.dry = True
    return item


def _rows(path: Path, skipped: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """(where, row) per line of a JSONL file; lines that do not parse are skipped."""
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not a JSON object")
        except ValueError as exc:
            skipped.append(f"{path.name} line {n}: {exc}")
            continue
        out.append((f"{path.name} line {n}", row))
    return out


def _moss_round1(row: dict[str, Any], samples: Path) -> _Item:
    name, line, variant = _safe(row["name"]), _safe(row["line"]), _safe(row["variant"])
    title = f"{_pretty(line)}: MOSS round 1, {variant} prompt, seed {row['seed']}"
    item = _moss_item("round1", row, line, samples / "moss" / f"{name}.wav", title, variant)
    item.tags.add(vocabulary.slug(f"{variant}-prompt"))
    return item


def _moss_round2(row: dict[str, Any], samples: Path) -> _Item:
    name, line, cond = _safe(row["name"]), _safe(row["line"]), _safe(row["condition"])
    title = f"{_pretty(line)}: MOSS round 2, {cond}, seed {row['seed']}"
    return _moss_item("round2", row, line, samples / "moss_grid" / f"{name}.wav", title, cond)


def _moss_spot(row: dict[str, Any], samples: Path) -> _Item:
    name = _safe(row["name"])
    line = name.split("__")[0]
    title = f"{_pretty(line)}: MOSS spot, seed {row['seed']}"
    item = _moss_item("spots", row, line, samples / "spots_moss" / f"{name}.wav", title, "default")
    item.tags |= {"use:rpg", "spot-demo"}
    item.notes.append(SPOT_DEMO_NOTES["moss", None])
    if line == "rockfall":
        # The prompt says "distant", the user heard it far too close.
        item.perspective = Perspective.CLOSE
        item.notes.append(SPOT_DEMO_NOTES["moss", "rockfall"])
    return item


def _epidemic_ref(line: str, entry: dict[str, Any], samples: Path) -> _Item:
    line, pick = _safe(line), entry["pick"]
    src = samples / "epidemic" / f"{line}.wav"
    return _epidemic_item(
        "refs", line, line, src, _safe(pick["id"]), pick["title"], pick.get("tags", "")
    )


def _epidemic_spot(row: dict[str, Any], samples: Path) -> _Item:
    name = _safe(row["name"])
    line = name.split("_")[0]
    src = samples / "spots_epidemic" / f"{name}.wav"
    item = _epidemic_item("spots", name, line, src, _safe(row["id"]), row["title"], "")
    item.tags |= {"use:rpg", "spot-demo"}
    if ("epidemic", line) in SPOT_DEMO_NOTES:
        item.notes.append(SPOT_DEMO_NOTES["epidemic", line])
    return item


def _collect(root: Path, skipped: list[str]) -> dict[str, _Item]:
    """Every clip sound-gen describes. A row that cannot be used (bad or unsafe name, unknown
    line, missing field) is skipped with a message; the rest goes on."""
    samples, results = root / "samples", root / "results"
    use = _use_by_line(root)
    sources: list[tuple[str, dict[str, Any], Callable[..., _Item]]] = []
    sources += [(w, r, _moss_round1) for w, r in _rows(results / "moss.jsonl", skipped)]
    sources += [(w, r, _moss_round2) for w, r in _rows(results / "moss_grid.jsonl", skipped)]
    sources += [(w, r, _moss_spot) for w, r in _rows(results / "spots_moss.jsonl", skipped)]
    spots_index = samples / "spots_epidemic" / "index.jsonl"
    sources += [(w, r, _epidemic_spot) for w, r in _rows(spots_index, skipped)]

    by_id: dict[str, _Item] = {}

    def add(where: str, build: Callable[[], _Item]) -> None:
        try:
            item = build()
        except (LibraryError, vocabulary.TagError, KeyError, TypeError, ValueError) as exc:
            what = f"missing field {exc}" if isinstance(exc, KeyError) else str(exc)
            skipped.append(f"{where}: {what}, skipped")
            return
        if item.id in by_id:
            skipped.append(f"{where}: a second file for clip {item.id}, skipped")
            return
        by_id[item.id] = item

    for where, row, build in sources:
        add(where, lambda row=row, build=build: build(row, samples))
    index = json.loads((samples / "epidemic" / "index.json").read_text())
    for line, entry in index.items():
        add(
            f"index.json {line!r}",
            lambda line=line, entry=entry: _epidemic_ref(line, entry, samples),
        )

    for item in by_id.values():
        if item.line in use:
            item.tags.add(f"use:{vocabulary.slug(use[item.line])}")
    epidemic_refs = {
        item.line: item.id
        for item in by_id.values()
        if item.source is Source.EPIDEMIC and item.path.startswith(EPIDEMIC_DIRS["refs"] + "/")
    }
    _apply_ratings(root, by_id, epidemic_refs, skipped)
    return by_id


# -- ratings ----------------------------------------------------------------------------------

_ROUND1 = re.compile(
    r"^\s*\d+\s+(?P<item>\w+):\s*najlepsze\s+(?P<best>\S+?),\s*najgorsze\s+(?P<worst>\S+?),"
    r"\s*uwagi:\s*(?P<remark>.*)$"
)
_ROUND2 = re.compile(r"^(?P<item>\w+):\s*(?P<votes>.*?)\s*\|\s*uwagi:\s*(?P<remark>.*)$")


def _clip_id(item: str, source: str, round_: str, epidemic_refs: dict[str, str]) -> str:
    if source == "epidemic":
        return epidemic_refs.get(item, f"epidemic:<reference of {item}>")
    if round_ == "round1":
        return f"moss:sound-gen/round1/{item}__{source.removeprefix('moss_')}"
    return f"moss:sound-gen/round2/{item}__{source}"


def _played_from(info: dict[str, Any]) -> str:
    start = info.get("epidemic_start_s")
    return f" (played from {start} s)" if start else ""


def _apply_ratings(
    root: Path, items: dict[str, _Item], epidemic_refs: dict[str, str], skipped: list[str]
) -> None:
    """Round 2 ratings (1-5) win; round 1 best/worst picks fill in where round 2 has nothing.
    Every clip that was in a round gets a note with its blind number and the user's remark."""
    results = root / "results"
    round1: dict[str, int] = {}
    round2: dict[str, int] = {}

    key = json.loads((results / "blind_key.json").read_text())
    for text in (results / "blind_votes.txt").read_text().splitlines():
        m = _ROUND1.match(text)
        if not m:
            continue
        name, remark = m["item"], m["remark"].strip()
        picks = {m["best"]: "best", m["worst"]: "worst"}
        for n, info in sorted(key.items(), key=lambda kv: int(kv[0])):
            if info["item"] != name:
                continue
            clip_id = _clip_id(name, info["source"], "round1", epidemic_refs)
            pick = picks.get(n)
            if pick == "best":
                round1[clip_id] = ROUND1_BEST_RATING
                verdict = f"picked best of 3, rating {ROUND1_BEST_RATING}"
            elif pick == "worst":
                round1[clip_id] = ROUND1_WORST_RATING
                verdict = f"picked worst of 3, rating {ROUND1_WORST_RATING}"
            else:
                verdict = "neither best nor worst of 3"
            _note(items, clip_id, skipped).append(
                f"Round 1 blind test, #{n}{_played_from(info)}: {verdict}. "
                f"The user's remark on {name}: {remark}"
            )
        for n in picks:
            if n != "-" and key.get(n, {}).get("item") != name:
                raise LibraryError(f"round 1: #{n} is not a {name} clip in blind_key.json")

    key = json.loads((results / "blind_grid_key.json").read_text())
    for text in (results / "blind_grid_votes.txt").read_text().splitlines():
        m = _ROUND2.match(text)
        if not m:
            continue
        name, remark = m["item"], m["remark"].strip()
        votes = dict(re.findall(r"(\d+)=(\d|-)", m["votes"]))
        for n in votes:
            if key.get(n, {}).get("item") != name:
                raise LibraryError(f"round 2: #{n} is not a {name} clip in blind_grid_key.json")
        for n, info in sorted(key.items(), key=lambda kv: int(kv[0])):
            if info["item"] != name:
                continue
            clip_id = _clip_id(name, info["source"], "round2", epidemic_refs)
            value = votes.get(n, "-")
            if value != "-":
                round2[clip_id] = int(value)
            verdict = f"rated {value}/5" if value != "-" else "not rated"
            _note(items, clip_id, skipped).append(
                f"Round 2 blind test, #{n}{_played_from(info)}: {verdict}. "
                f"The user's remark on {name}: {remark}"
            )

    for clip_id, item in items.items():
        item.rating = round2.get(clip_id, round1.get(clip_id))
        if any(n.startswith("Round 1") for n in item.notes):
            item.tags.add("blind-round1")
        if any(n.startswith("Round 2") for n in item.notes):
            item.tags.add("blind-round2")


def _note(items: dict[str, _Item], clip_id: str, skipped: list[str]) -> list[str]:
    """The notes of a clip; for a clip that is not imported, a throwaway list and a message."""
    if clip_id not in items:
        skipped.append(f"a blind-test key refers to {clip_id}, which is not imported")
        return []
    return items[clip_id].notes


# -- copying, measuring, storing --------------------------------------------------------------

# Each source may only write inside its own subtree of the data folder.
_ROOTS = {Source.MOSS: "generated", Source.EPIDEMIC: "epidemic"}
LOCK_NAME = "catalog-import.lock"


def _destination(data_dir: Path, item: _Item) -> Path:
    """The absolute copy target, checked before anything is written: a relative path without
    "..", resolved inside the data folder's `generated/` (MOSS) or `epidemic/` (Epidemic)."""
    rel = Path(item.path)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise LibraryError(f"bad target path {item.path!r}")
    root = (data_dir / _ROOTS[item.source]).resolve()
    dest = (data_dir / rel).resolve()
    if not dest.is_relative_to(root) or dest == root:
        raise LibraryError(f"target {item.path!r} is outside {_ROOTS[item.source]}/")
    return dest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _copy(src: Path, dest: Path) -> str:
    """Copy through a uniquely named temp file next to dest (still *.wav, so the data folder's git
    ignores a leftover). Returns the SHA-256 of the copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.stem}.", suffix=".part.wav")
    os.close(fd)
    try:
        shutil.copy2(src, tmp)  # keeps the mtime, so the next run's shallow compare is enough
        digest = _sha256(Path(tmp))
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return digest


@dataclass
class _Planned:
    item: _Item
    sha256: str
    measured: tuple[ffmpeg.AudioInfo, Loudness] | None  # None: reuse the catalog's numbers


def _imported(item: _Item) -> ImportedFields:
    return ImportedFields(
        kind=item.kind,
        tags=tuple(sorted(item.tags)),
        pools=item.pools,
        perspective=item.perspective,
        dry=item.dry,
        loopable=item.kind is Kind.BED,
        rating=item.rating,
        notes="\n".join(item.notes),
        status=status_for(item.rating),
    )


def _new_clip(item: _Item, sha: str, info: ffmpeg.AudioInfo, loud: Loudness) -> Clip:
    imported = _imported(item)
    clip = Clip(
        id=item.id,
        path=item.path,
        title=item.title,
        source=item.source,
        details=item.details,
        license=license_for(item.source),
        loudness=loud,
        gain_db=levels.gain_db(item.kind, loud),
        duration_s=info.duration_s,
        channels=info.channels,
        sample_rate=info.sample_rate,
        sha256=sha,
        **{f: getattr(imported, f) for f in USER_FIELDS},
    )
    # Normalized values (tags), so an untouched clip compares equal to its import.
    return clip.model_copy(update={"imported": clip.user_fields()})


def _merge(old: Clip, new: Clip) -> Clip:
    """Re-import rule:
    - what comes from the file and the source (path, title, source details, license, stream
      info, loudness, hash) is taken from the new import;
    - a user-editable field (USER_FIELDS) the user did not edit (its catalog value still equals
      the value the last import set) gets the new import's value; an edited one is kept. With
      no record of the last import (`imported` None) every field counts as edited;
    - status follows the rating: when the user edited the rating, the status is kept too;
    - the gain follows the resulting kind."""
    assert new.imported is not None
    values = {}
    for f in USER_FIELDS:
        edited = old.imported is None or getattr(old, f) != getattr(old.imported, f)
        values[f] = getattr(old if edited else new, f)
    if old.imported is None or old.rating != old.imported.rating:
        values["status"] = old.status
    return old.changed(
        path=new.path,
        title=new.title,
        source=new.source,
        details=new.details,
        license=new.license,
        loudness=new.loudness,
        duration_s=new.duration_s,
        channels=new.channels,
        sample_rate=new.sample_rate,
        sha256=new.sha256,
        imported=new.imported,
        gain_db=levels.gain_db(values["kind"], new.loudness),
        **values,
    )


@asynccontextmanager
async def exclusive_lock(
    path: Path, progress: Callable[[str], None] | None = None
) -> AsyncIterator[None]:
    """An exclusive flock on `path` (created if needed), waiting for another holder in a
    worker thread, so the event loop keeps running."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if progress:
                progress(f"waiting for another import to finish ({path})")
            await asyncio.to_thread(fcntl.flock, f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


_Measure = Callable[[Path, str], tuple[ffmpeg.AudioInfo, Loudness]]


def _place(item: _Item, dest: Path, recorded: str | None, report: ImportReport) -> str | None:
    """Copy the file if needed and safe (blocking: hashes and copies). Returns the SHA-256 of
    the data-folder file, or None: leave it."""
    if dest.exists():
        current = _sha256(dest)
        if recorded is not None and current != recorded:
            if filecmp.cmp(item.src, dest, shallow=False):
                # Byte-identical to the current source: our own copy from an import that was
                # interrupted before it recorded the new hash. Adopt it (measured later).
                return current
            report.kept.append(f"{item.path}: changed in the data folder, kept")
            return None
        if filecmp.cmp(item.src, dest, shallow=True):
            return current
        if recorded is None:
            # Not a file this catalog imported, and not the source either: never overwrite it.
            report.kept.append(f"{item.path}: differs from the source and is not ours, kept")
            return None
        # The source changed, our copy did not: update it (below).
    sha = _copy(item.src, dest)
    report.copied.append(item.path)
    return sha


async def _plan(
    item: _Item, catalog: Catalog, report: ImportReport, measure_file: _Measure
) -> _Planned | None:
    """Copy the file if needed and safe, measure it if its content is new. None: leave it.
    The blocking work runs in worker threads."""
    try:
        dest = _destination(catalog.data_dir, item)
    except LibraryError as exc:
        report.skipped.append(f"{item.id}: {exc}, skipped")
        return None
    if not item.src.is_file():
        report.missing.append(str(item.src))
        return None
    old = await catalog.get(item.id)
    recorded = old.sha256 if old is not None and old.path == item.path else None
    existed = dest.exists()
    sha = await asyncio.to_thread(_place, item, dest, recorded, report)
    if sha is None:
        return None
    if old is not None and old.sha256 == sha and old.loudness is not None:
        return _Planned(item, sha, None)
    try:
        measured = await asyncio.to_thread(measure_file, dest, item.path)
    except ffmpeg.FfmpegError as exc:
        if not existed:
            dest.unlink(missing_ok=True)  # do not leave a copy the catalog does not know
            report.copied.remove(item.path)
        report.skipped.append(f"{item.path}: cannot read the audio, skipped: {exc}")
        return None
    return _Planned(item, sha, measured)


async def import_sound_gen(
    root: Path,
    catalog: Catalog,
    *,
    lock_path: Path,
    probe: Callable[[Path], ffmpeg.AudioInfo] = ffmpeg.probe,
    measure: Callable[[Path], Loudness] = ffmpeg.loudness,
    progress: Callable[[str], None] | None = None,
) -> ImportReport:
    """Import under an exclusive lock (`lock_path`, in the state folder), so two imports never
    copy, measure or write at the same time. `probe`, `measure` and `progress` are called in
    worker threads."""
    root = Path(root).expanduser()
    if not (root / "samples").is_dir() or not (root / "results").is_dir():
        raise LibraryError(f"{root} does not look like sound-gen (no samples/ or results/)")

    def measure_file(dest: Path, rel: str) -> tuple[ffmpeg.AudioInfo, Loudness]:
        if progress:
            progress(f"measuring {rel}")
        return probe(dest), measure(dest)

    async with exclusive_lock(lock_path, progress):
        report = ImportReport()
        items = await asyncio.to_thread(_collect, root, report.skipped)
        # Copy and measure first, outside the write transaction.
        planned = [
            p for i in sorted(items) if (p := await _plan(items[i], catalog, report, measure_file))
        ]
        # What the plan left unmeasured (its row changed between the plan and here) is measured
        # here, still outside the write transaction: the batch stays short and never spans ffmpeg.
        write = []
        for p in planned:
            if p.measured is None:
                old = await catalog.get(p.item.id)
                if old is None or old.loudness is None:
                    try:
                        p.measured = await asyncio.to_thread(
                            measure_file, catalog.data_dir / p.item.path, p.item.path
                        )
                    except ffmpeg.FfmpegError as exc:
                        report.skipped.append(f"{p.item.path}: cannot read the audio: {exc}")
                        continue
            write.append(p)
        # Each row is read again and written inside one short transaction, so an edit made while
        # the files were measured is merged, not lost.
        async with catalog.batch():
            known = await catalog.pool_descriptions()
            for name, description in POOLS.items():
                if name not in known:
                    await catalog.define_pool(name, description)
            for p in write:
                old = await catalog.get(p.item.id)
                if p.measured is not None:
                    info, loud = p.measured
                else:
                    assert old is not None and old.loudness is not None
                    info = ffmpeg.AudioInfo(
                        duration_s=old.duration_s,
                        channels=old.channels,
                        sample_rate=old.sample_rate,
                    )
                    loud = old.loudness
                new = _new_clip(p.item, p.sha256, info, loud)
                if old is None:
                    report.created.append(new.id)
                else:
                    new = _merge(old, new)
                    if new == old:
                        report.unchanged.append(new.id)
                        continue
                    report.updated.append(new.id)
                await catalog.put(new)
    return report


# -- queries ----------------------------------------------------------------------------------


async def pool_summaries(catalog: Catalog) -> list[PoolSummary]:
    descriptions = await catalog.pool_descriptions()
    return [
        PoolSummary(
            name=name,
            description=descriptions.get(name, ""),
            clips=len(clips),
            kinds=tuple(sorted({c.kind.value for c in clips})),
            accepted=sum(c.status is Status.ACCEPTED for c in clips),
        )
        for name, clips in (await catalog.pools()).items()
    ]

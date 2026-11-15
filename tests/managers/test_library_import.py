import asyncio
import dataclasses
import fcntl
import hashlib
import json
import tempfile
import threading
from pathlib import Path

import pytest
from audio import EPI_CAVE, EPI_PEBBLES, EPI_SWORD

from rpghome.infra import ffmpeg
from rpghome.managers import library
from rpghome.models.clip import Kind, License, Perspective, Source, Status
from rpghome.repositories.catalog import Catalog


def _lock(cat):
    return cat.data_dir.parent / "state" / "locks" / library.LOCK_NAME


def _hashes(root):
    return {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
async def imported(needs_ffmpeg, sound_gen, tmp_path):
    async with Catalog(tmp_path / "data") as cat:
        report = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
        yield cat, report


async def test_import_creates_clips_with_copies(imported, sound_gen):
    cat, report = imported
    assert len(report.created) == 10 and not report.missing
    clip = await cat.get("moss:sound-gen/round2/cave_ambience__default__s1")
    assert clip.path == "generated/moss/sound-gen/round2/cave_ambience__default__s1.wav"
    assert (cat.data_dir / clip.path).read_bytes() == (
        sound_gen / "samples/moss_grid/cave_ambience__default__s1.wav"
    ).read_bytes()
    assert clip.source is Source.MOSS and clip.license is License.SHAREABLE
    assert clip.details.seed == 1 and clip.details.condition == "default"
    assert clip.kind is Kind.BED and clip.loopable and clip.pools == ("cave.bed",)
    assert clip.duration_s == 5.0 and clip.channels == 1 and clip.sample_rate == 48000
    # A 0.02 sine reads -37 LUFS; the bed target is -31: +6 dB.
    assert clip.loudness.bed_lufs == pytest.approx(-37.0, abs=0.2)
    assert clip.gain_db == pytest.approx(6.0, abs=0.2)
    neg = await cat.get("moss:sound-gen/round2/cave_ambience__neg__s1")
    assert neg.details.negative_prompt == "music, hiss"

    epi = await cat.get(f"epidemic:{EPI_CAVE}")
    assert epi.path == "epidemic/sound-gen/refs/cave_ambience.wav"
    assert epi.license is License.NEVER_SHARE
    assert epi.details.title.startswith("Ambience, Underground")
    pebbles = await cat.get(f"epidemic:{EPI_PEBBLES}")
    assert pebbles.path == "epidemic/sound-gen/spots/pebbles_a.wav"
    assert pebbles.kind is Kind.SPOT and pebbles.dry is True and not pebbles.loopable
    sword = await cat.get(f"epidemic:{EPI_SWORD}")
    assert sword.kind is Kind.FX
    # fx: stereo 0.5 sine, 2 s: -6 LUFS, -7.8 over a 3 s window; to -22: -14.2 dB.
    assert sword.gain_db == pytest.approx(-14.2, abs=0.2)
    for c in await cat.all():
        assert not c.path.startswith("/")
        assert (cat.data_dir / c.path).is_file()


async def test_tags_and_perspective(imported):
    cat, _ = imported
    cave = await cat.get("moss:sound-gen/round1/cave_ambience__short")
    assert {
        "place:cave",
        "source:water",
        "ambience",
        "use:rpg",
        "batch:sound-gen",
        "short-prompt",
        "blind-round1",
    } <= set(cave.tags)
    assert "cave" not in cave.tags  # said by place:cave already
    rock = await cat.get("moss:sound-gen/spots/rockfall__s1")
    assert {"place:cave", "source:stone", "rockfall"} <= set(rock.tags)
    assert rock.perspective is Perspective.CLOSE  # the user heard it far too close
    assert "far too close" in rock.notes
    assert (await cat.get("moss:sound-gen/round1/cave_ambience__detailed")).dry is False  # echo
    epi = await cat.get(f"epidemic:{EPI_CAVE}")
    assert {"place:cave", "source:water", "ambience"} <= set(epi.tags)
    assert "underground" not in epi.tags  # a keyword: mapped to place:cave


async def test_ratings_and_status(imported):
    cat, _ = imported

    clips = {c.id: c for c in await cat.all()}

    def get(name):
        return clips[name if name.startswith("epidemic") else f"moss:sound-gen/{name}"]

    # Round 2 wins over round 1: Epidemic cave was best in round 1 (4) but got 2 in round 2.
    epi = get(f"epidemic:{EPI_CAVE}")
    assert epi.rating == 2 and epi.status is Status.REVIEW
    assert "Round 1 blind test, #1 (played from 10.0 s): picked best of 3" in epi.notes
    assert "Round 2 blind test, #3 (played from 10.0 s): rated 2/5" in epi.notes
    assert "piątki są mocne" in epi.notes
    assert get("round2/cave_ambience__default__s1").rating == 5
    assert get("round2/cave_ambience__default__s1").status is Status.ACCEPTED
    unrated = get("round2/cave_ambience__neg__s1")
    assert unrated.rating is None and unrated.status is Status.REVIEW
    assert "not rated" in unrated.notes
    middle = get("round1/cave_ambience__short")
    assert middle.rating is None and "neither best nor worst" in middle.notes
    assert get("round1/cave_ambience__detailed").rating == 2
    best = get("round1/sword_clash__short")
    assert best.rating == 4 and best.status is Status.ACCEPTED and "4 filmowe" in best.notes
    assert get(f"epidemic:{EPI_SWORD}").rating == 2
    spot = get("spots/drop__s1")
    assert spot.rating is None and spot.status is Status.REVIEW


async def test_pools_including_the_empty_one(imported):
    cat, _ = imported
    pools = {p.name: p for p in await library.pool_summaries(cat)}
    assert set(pools) == set(library.POOLS)
    assert pools["cave.bed"].clips == 5 and pools["cave.bed"].accepted == 1
    assert pools["cave.drops"].clips == 1 and pools["cave.drops"].kinds == ("spot",)
    assert pools["cave.stones"].clips == 2
    assert pools["swords.clash"].clips == 2 and pools["swords.clash"].kinds == ("fx",)
    assert pools["storm.thunder"].clips == 0 and "empty" in pools["storm.thunder"].description


async def test_tag_search(imported):
    cat, _ = imported
    stones = [c.id for c in await cat.search(tags=["place:cave", "source:stone"])]
    assert stones == [f"epidemic:{EPI_PEBBLES}", "moss:sound-gen/spots/rockfall__s1"]
    accepted_beds = await cat.search(tags=["cave"], kind=Kind.BED, status=Status.ACCEPTED)
    assert [c.id for c in accepted_beds] == ["moss:sound-gen/round2/cave_ambience__default__s1"]


async def test_reimport_changes_nothing(imported, sound_gen):
    cat, _ = imported
    export = cat.export_path.read_bytes()
    copies = _hashes(cat.data_dir / "generated") | _hashes(cat.data_dir / "epidemic")
    originals = _hashes(sound_gen)
    mtimes = {p: p.stat().st_mtime_ns for p in copies}

    def fail(path):
        raise AssertionError(f"measured again: {path}")

    report = await library.import_sound_gen(
        sound_gen, cat, lock_path=_lock(cat), probe=fail, measure=fail
    )
    assert not report.created and not report.updated and not report.copied
    assert len(report.unchanged) == 10
    assert len(await cat.all()) == 10
    assert cat.export_path.read_bytes() == export
    assert {p: p.stat().st_mtime_ns for p in copies} == mtimes
    assert _hashes(sound_gen) == originals  # the originals are only read


async def test_reimport_keeps_user_edits(imported, sound_gen):
    cat, _ = imported
    cid = "moss:sound-gen/round2/cave_ambience__default__s1"
    await cat.update(cid, rating=1, status=Status.REJECTED, notes="mine", tags=("place:cave",))
    edited = await cat.get(cid)
    await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
    assert await cat.get(cid) == edited


async def test_changed_source_file_is_copied_and_measured_again(imported, sound_gen, sine):
    cat, _ = imported
    cid = "moss:sound-gen/round2/cave_ambience__default__s1"
    await cat.update(cid, rating=1)
    sine(sound_gen / "samples/moss_grid/cave_ambience__default__s1.wav", 5, 0.04)
    report = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
    assert report.updated == [cid]
    clip = await cat.get(cid)
    assert clip.loudness.bed_lufs == pytest.approx(-31.0, abs=0.2)
    assert clip.gain_db == pytest.approx(0.0, abs=0.2)
    assert clip.rating == 1  # kept


async def test_not_a_sound_gen_folder(tmp_path):
    async with Catalog(tmp_path / "data") as cat:
        with pytest.raises(library.LibraryError):
            await library.import_sound_gen(tmp_path / "nothing", cat, lock_path=_lock(cat))


# -- review fixes -------------------------------------------------------------------------------


async def test_unsafe_names_are_skipped_before_any_write(needs_ffmpeg, sound_gen, tmp_path):
    escape = tmp_path / "escape.wav"  # where samples/spots_moss/../../../escape.wav points
    escape.write_bytes((sound_gen / "samples/spots_moss/drop__s1.wav").read_bytes())
    with (sound_gen / "results/spots_moss.jsonl").open("a") as f:
        f.write(json.dumps({"name": "../../../escape", "prompt": "x", "seed": 1}) + "\n")
        f.write(json.dumps({"name": "unknownline__s1", "prompt": "x", "seed": 1}) + "\n")
        f.write("not json\n")
    data = tmp_path / "data"
    async with Catalog(data) as cat:
        report = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
        assert len(report.created) == 10  # the good rows still go in
    skipped = "\n".join(report.skipped)
    assert "spots_moss.jsonl line 3: unsafe name '../../../escape'" in skipped
    assert "spots_moss.jsonl line 4: unknown sound-gen line 'unknownline'" in skipped
    assert "spots_moss.jsonl line 5:" in skipped
    written = [p for p in tmp_path.rglob("*escape*") if p != escape]
    assert written == []


def _item(source, path):
    details = (
        library.MossDetails(prompt="p", seed=1, steps=100, cfg=4.0, condition="default")
        if source is Source.MOSS
        else library.EpidemicDetails(id="i", title="t")
    )
    return library._Item(
        id="x", line="drop", src=Path("/nowhere"), path=path, kind=Kind.SPOT, title="",
        source=source, details=details, tags=set(), pools=(),
    )  # fmt: skip


def test_destination_must_stay_in_its_subtree(tmp_path):
    data = tmp_path / "data"
    ok = library._destination(data, _item(Source.MOSS, "generated/moss/a.wav"))
    assert ok == (data / "generated/moss/a.wav").resolve()
    for source, path in [
        (Source.EPIDEMIC, "generated/moss/a.wav"),  # Epidemic outside epidemic/
        (Source.MOSS, "epidemic/a.wav"),
        (Source.MOSS, "generated/../epidemic/a.wav"),
        (Source.MOSS, "/tmp/a.wav"),
        (Source.MOSS, "generated"),
    ]:
        with pytest.raises(library.LibraryError):
            library._destination(data, _item(source, path))
    # A symlink inside generated/ that points out of the data folder is caught by resolving.
    (data / "generated").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (data / "generated" / "moss").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(library.LibraryError):
        library._destination(data, _item(Source.MOSS, "generated/moss/a.wav"))


async def test_a_copy_changed_in_the_data_folder_is_kept(imported, sound_gen, sine):
    cat, _ = imported
    cid = "moss:sound-gen/round2/cave_ambience__default__s1"
    before = await cat.get(cid)
    copy = cat.data_dir / before.path
    sine(copy, 5, 0.3)  # e.g. trimmed by hand in an editor
    edited = copy.read_bytes()
    export = cat.export_path.read_bytes()
    report = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
    assert report.kept == [f"{before.path}: changed in the data folder, kept"]
    assert copy.read_bytes() == edited
    assert await cat.get(cid) == before  # its row is not updated from the source
    assert cat.export_path.read_bytes() == export
    # Also when the source changed too: the changed copy is still not overwritten.
    sine(sound_gen / "samples/moss_grid/cave_ambience__default__s1.wav", 5, 0.05)
    await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
    assert copy.read_bytes() == edited and await cat.get(cid) == before


async def test_an_unknown_file_at_the_target_is_not_overwritten(
    needs_ffmpeg, sound_gen, sine, tmp_path
):
    async with Catalog(tmp_path / "data") as cat:
        target = cat.data_dir / "generated/moss/sound-gen/spots/drop__s1.wav"
        sine(target, 1, 0.01)
        mine = target.read_bytes()
        report = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
        assert target.read_bytes() == mine
        assert "generated/moss/sound-gen/spots/drop__s1.wav: differs" in report.kept[0]
        assert await cat.get("moss:sound-gen/spots/drop__s1") is None


async def test_hash_is_recorded(imported):
    cat, _ = imported
    for clip in await cat.all():
        assert clip.sha256 == hashlib.sha256((cat.data_dir / clip.path).read_bytes()).hexdigest()


async def test_corrected_votes_reach_unedited_clips(imported, sound_gen):
    cat, _ = imported
    s1 = "moss:sound-gen/round2/cave_ambience__default__s1"
    neg = "moss:sound-gen/round2/cave_ambience__neg__s1"
    await cat.update(neg, rating=4, status=Status.ACCEPTED)  # the user rated the unrated clip
    votes = sound_gen / "results/blind_grid_votes.txt"
    votes.write_text("cave_ambience: 1=2 2=3 3=2 | uwagi: poprawione\n")
    report = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
    assert s1 in report.updated
    clip = await cat.get(s1)
    assert clip.rating == 2 and clip.status is Status.REVIEW
    assert "rated 2/5" in clip.notes and "poprawione" in clip.notes
    user = await cat.get(neg)
    assert user.rating == 4 and user.status is Status.ACCEPTED  # edited: kept
    assert "poprawione" in user.notes  # notes were not edited: refreshed
    # And it is stable: once more changes nothing.
    export = cat.export_path.read_bytes()
    again = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
    assert not again.updated and cat.export_path.read_bytes() == export


async def test_edited_tags_are_kept_unedited_ones_refreshed(imported, sound_gen, monkeypatch):
    cat, _ = imported
    a = "moss:sound-gen/spots/drop__s1"
    b = "moss:sound-gen/spots/rockfall__s1"
    await cat.update(a, tags=("mine",))
    monkeypatch.setitem(library.LINE_TAGS, "rockfall", ("place:cave", "mood:tense"))
    monkeypatch.setitem(library.LINE_TAGS, "drop", ("place:cave", "mood:calm"))
    await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
    assert (await cat.get(a)).tags == ("mine",)
    assert "mood:tense" in (await cat.get(b)).tags


async def test_imports_take_turns(imported, sound_gen):
    cat, _ = imported
    lock = _lock(cat)
    waiting = threading.Event()

    def progress(msg):
        if msg.startswith("waiting"):
            waiting.set()

    async def other_import():
        async with Catalog(cat.data_dir) as other:
            await library.import_sound_gen(sound_gen, other, lock_path=lock, progress=progress)

    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        task = asyncio.create_task(other_import())
        assert await asyncio.to_thread(waiting.wait, 10)
        await asyncio.sleep(0.3)  # the event loop runs while the import waits for the lock
        assert not task.done()  # still blocked while the lock is held
        fcntl.flock(held, fcntl.LOCK_UN)
    await asyncio.wait_for(task, 30)


async def test_edit_made_during_an_import_is_not_lost(imported, sound_gen, sine):
    cat, _ = imported
    cid = "moss:sound-gen/round2/cave_ambience__default__s1"
    sine(sound_gen / "samples/moss_grid/cave_ambience__default__s1.wav", 5, 0.04)

    async def edit():
        async with Catalog(cat.data_dir) as other:
            await other.update(cid, rating=1, notes="edited mid-import")

    def measure(path):
        # The user rates the clip while the import is measuring it (in a worker thread).
        asyncio.run(edit())
        return ffmpeg.loudness(path)

    await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat), measure=measure)
    clip = await cat.get(cid)
    assert clip.rating == 1 and clip.notes == "edited mid-import"
    assert clip.loudness.bed_lufs == pytest.approx(-31.0, abs=0.2)  # and the new measurement


async def test_temp_names_are_unique(imported, monkeypatch, tmp_path):
    cat, _ = imported
    seen = []
    real = tempfile.mkstemp

    def spy(*a, **kw):
        fd, name = real(*a, **kw)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy)
    src = cat.data_dir / (await cat.get("moss:sound-gen/spots/drop__s1")).path
    library._copy(src, tmp_path / "out" / "a.wav")
    library._copy(src, tmp_path / "out" / "a.wav")
    await cat.update("moss:sound-gen/spots/drop__s1", rating=2)  # an export
    assert len(set(seen)) == 3
    assert all(Path(n).name.startswith(".") for n in seen)
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.startswith(".")]


async def test_copy_from_an_interrupted_import_is_adopted(imported, sound_gen, sine):
    cat, _ = imported
    cid = "moss:sound-gen/round2/cave_ambience__default__s1"
    before = await cat.get(cid)
    # The source gets 6 dB quieter (0.02 -> 0.01: -37 -> -43 LUFS).
    sine(sound_gen / "samples/moss_grid/cave_ambience__default__s1.wav", 5, 0.01)

    def interrupted(path):
        raise KeyboardInterrupt  # Ctrl-C after the copy, before the commit

    with pytest.raises(KeyboardInterrupt):
        await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat), measure=interrupted)
    assert await cat.get(cid) == before  # nothing committed, but the copy is already the new file
    report = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
    assert report.kept == []
    assert report.updated == [cid]
    clip = await cat.get(cid)
    assert clip.loudness.bed_lufs == pytest.approx(-43.0, abs=0.2)
    assert clip.gain_db == pytest.approx(12.0, abs=0.2)
    assert clip.sha256 == hashlib.sha256((cat.data_dir / clip.path).read_bytes()).hexdigest()
    again = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
    assert again.unchanged and not again.updated and not again.kept


async def test_the_write_batch_never_spans_ffmpeg(needs_ffmpeg, sound_gen, tmp_path, monkeypatch):
    """Everything is measured before the write batch opens: the batch holds the SQLite write
    lock, and ffmpeg must not run while it is held. The plan is forced to report "nothing
    measured", which used to make the import measure inside the batch."""
    events = []
    real_plan = library._plan

    async def plan_without_measure(item, catalog, report, measure_file):
        p = await real_plan(item, catalog, report, measure_file)
        return None if p is None else dataclasses.replace(p, measured=None)

    monkeypatch.setattr(library, "_plan", plan_without_measure)

    def measure(path):
        events.append("measure")
        return ffmpeg.loudness(path)

    async with Catalog(tmp_path / "data") as cat:
        real_batch = cat.batch

        def batch(export=True):
            events.append("batch")
            return real_batch(export)

        monkeypatch.setattr(cat, "batch", batch)
        report = await library.import_sound_gen(
            sound_gen, cat, lock_path=_lock(cat), measure=measure
        )

    assert len(report.created) == 10
    assert events.count("batch") >= 1 and events.count("measure") >= 10
    assert "measure" not in events[events.index("batch") :]


async def test_a_corrupt_source_file_is_skipped(needs_ffmpeg, sound_gen, tmp_path):
    (sound_gen / "samples/spots_moss/drop__s1.wav").write_bytes(b"RIFF\x00\x00garbage")
    async with Catalog(tmp_path / "data") as cat:
        report = await library.import_sound_gen(sound_gen, cat, lock_path=_lock(cat))
        assert len(report.created) == 9  # the rest is imported
        assert await cat.get("moss:sound-gen/spots/drop__s1") is None
        path = "generated/moss/sound-gen/spots/drop__s1.wav"
        assert any(m.startswith(f"{path}: cannot read the audio, skipped") for m in report.skipped)
        assert path not in report.copied
        assert not (cat.data_dir / path).exists()  # no copy the catalog does not know

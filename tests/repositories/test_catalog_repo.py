import asyncio
import sqlite3
import warnings
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rpghome.models.clip import (
    Clip,
    EpidemicDetails,
    Kind,
    License,
    Loudness,
    MossDetails,
    Perspective,
    Source,
    Status,
)
from rpghome.repositories import catalog as repo
from rpghome.repositories import db
from rpghome.repositories.catalog import Catalog, CatalogError
from rpghome.repositories.tables import ClipRow


def _head() -> str:
    return ScriptDirectory.from_config(db.alembic_config()).get_current_head()


def _moss(n: str, **kw) -> Clip:
    base = dict(
        id=f"moss:test/{n}",
        path=f"generated/moss/test/{n}.wav",
        kind=Kind.BED,
        title=f"Cave {n}",
        source=Source.MOSS,
        details=MossDetails(
            prompt="Water drops in a cave", seed=1, steps=100, cfg=4.0, condition="default"
        ),
        license=License.SHAREABLE,
        tags=("place:cave", "source:water"),
        pools=("cave.bed",),
        loudness=Loudness(
            integrated_lufs=-30.04, bed_lufs=-31.25, max_short_term_lufs=-27.0, true_peak_dbtp=-1.2
        ),
        gain_db=0.2,
        loopable=True,
        duration_s=30.0,
        channels=1,
        sample_rate=48000,
    )
    return Clip(**{**base, **kw})


def _epidemic() -> Clip:
    return Clip(
        id="epidemic:abc",
        path="epidemic/test/sword.wav",
        kind=Kind.FX,
        title="Weapons, Sword, Clash",
        source=Source.EPIDEMIC,
        details=EpidemicDetails(id="abc", title="Weapons, Sword, Clash"),
        tags=("source:sword", "clash"),
        pools=("swords.clash",),
        perspective=Perspective.CLOSE,
        dry=True,
        rating=4,
        notes="Brzmi jak prawdziwe nagranie",
        status=Status.ACCEPTED,
    )


@pytest.fixture
async def cat(tmp_path):
    async with Catalog(tmp_path / "data") as c:
        yield c


async def test_open_upgrades_to_head_with_wal_and_foreign_keys(cat):
    with sqlite3.connect(cat.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == _head()
    async with cat._session() as session:
        assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1


async def test_unknown_schema_revision_is_refused(tmp_path):
    async with Catalog(tmp_path):
        pass
    with sqlite3.connect(tmp_path / repo.DB_NAME) as conn:
        conn.execute("UPDATE alembic_version SET version_num = '9999'")
    with pytest.raises(CatalogError, match="9999"):
        async with Catalog(tmp_path):
            pass


async def test_old_sqlite3_schema_is_refused(tmp_path):
    """A catalog made by the old sqlite3 code: its own tables, `PRAGMA user_version = 1`,
    no `alembic_version` -> one clear CatalogError, not a raw driver error."""
    path = tmp_path / "data" / repo.DB_NAME
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 1")
        conn.execute("CREATE TABLE clips (id TEXT PRIMARY KEY, path TEXT UNIQUE)")
        conn.execute("INSERT INTO clips VALUES ('moss:old', 'a.wav')")
    with pytest.raises(CatalogError, match=r"unknown schema \(no alembic_version\).*delete it"):
        async with Catalog(tmp_path / "data"):
            pass


async def test_corrupt_catalog_file_is_refused(tmp_path):
    path = tmp_path / "data" / repo.DB_NAME
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a database at all")
    with pytest.raises(db.DatabaseError, match="not a valid database"):
        await db.upgrade(path)
    with pytest.raises(CatalogError, match="not a valid database"):
        async with Catalog(tmp_path / "data"):
            pass


async def test_search_does_not_wait_for_another_process_write(tmp_path, monkeypatch):
    """Opening a catalog (search, pools) checks the revision with a plain read and takes the
    write lock only when the database is behind head: a WAL reader never waits for another
    process's write batch. With a short busy timeout, the old BEGIN IMMEDIATE on every open
    would fail here with "database is locked"."""
    monkeypatch.setattr(db, "BUSY_TIMEOUT_S", 2)
    data = tmp_path / "data"
    a = await Catalog(data).open()
    try:
        await a.put(_moss("a"))  # committed
        async with a.batch():
            await a.put(_moss("b"))  # the write transaction is open until the block ends
            b = await Catalog(data).open()  # must not block on the held write lock
            try:
                found = await b.search()  # reads the last committed state
            finally:
                await b.close()
        assert [c.id for c in found] == ["moss:test/a"]
    finally:
        await a.close()


async def test_not_open(tmp_path):
    with pytest.raises(CatalogError, match="not open"):
        await Catalog(tmp_path).get("x")


def test_every_clip_field_is_stored():
    """Columns are named like the Clip fields; tags and pools have their own tables."""
    columns = {c.key for c in ClipRow.__table__.columns}
    assert columns - {"search_text"} == set(Clip.model_fields) - {"tags", "pools"}


async def test_rows_have_real_columns(cat):
    await cat.put(_moss("a", rating=4))
    with sqlite3.connect(cat.db_path) as conn:
        row = conn.execute(
            "SELECT kind, status, source, license, rating, title, json_extract(details, '$.seed')"
            " FROM clips"
        ).fetchone()
        tags = conn.execute("SELECT tag FROM clip_tags ORDER BY tag").fetchall()
    assert row == ("bed", "review", "moss", "shareable", 4, "Cave a", 1)
    assert tags == [("place:cave",), ("source:water",)]


async def test_last_used_is_utc(cat):
    await cat.put(_moss("a"))
    local = datetime(2026, 11, 15, 21, 0, tzinfo=timezone(timedelta(hours=1)))
    await cat.mark_used("moss:test/a", local)
    used = (await cat.get("moss:test/a")).last_used
    assert used == local and used.tzinfo is UTC


async def test_update_with_a_plain_status_warns_nothing(cat):
    """`update(id, status="accepted")` must not warn (a pydantic serializer UserWarning from
    a raw string in an enum field)."""
    await cat.put(_moss("a"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        clip = await cat.update("moss:test/a", status="accepted")
    assert clip.status is Status.ACCEPTED
    assert (await cat.get("moss:test/a")).status is Status.ACCEPTED


async def test_crud_roundtrip(cat):
    a, e = _moss("a"), _epidemic()
    await cat.put(a)
    await cat.put(e)
    assert await cat.get(a.id) == a
    assert await cat.get(e.id) == e
    assert [c.id for c in await cat.all()] == [e.id, a.id]
    b = await cat.update(a.id, rating=2, notes="too loud")
    assert await cat.get(a.id) == b and b.rating == 2
    assert await cat.delete(a.id)
    assert not await cat.delete(a.id)
    assert await cat.get(a.id) is None
    with pytest.raises(CatalogError):
        await cat.update("nope", rating=1)


async def test_path_is_unique(cat):
    await cat.put(_moss("a"))
    with pytest.raises(CatalogError):
        await cat.put(_moss("b", path="generated/moss/test/a.wav"))
    assert await cat.get("moss:test/b") is None  # the failed put was rolled back


async def test_search(cat):
    await cat.put(_moss("a", rating=5, status=Status.ACCEPTED))
    await cat.put(
        _moss(
            "b",
            tags=("place:cave", "source:stone", "rockfall"),
            kind=Kind.SPOT,
            notes="far too close",
        )
    )
    await cat.put(_epidemic())

    async def ids(**kw):
        return [c.id for c in await cat.search(**kw)]

    assert await ids(tags=["place:cave"]) == ["moss:test/a", "moss:test/b"]
    assert await ids(tags=["place:cave", "source:stone"]) == ["moss:test/b"]  # AND
    assert await ids(tags=["cave"]) == [
        "moss:test/a",
        "moss:test/b",
    ]  # plain matches category value
    assert await ids(tags=["rockfall"]) == ["moss:test/b"]
    assert await ids(tags=["place:cave", "source:sword"]) == []
    assert await ids(kind=Kind.SPOT) == ["moss:test/b"]
    assert await ids(status=Status.ACCEPTED) == ["epidemic:abc", "moss:test/a"]
    assert await ids(source=Source.EPIDEMIC) == ["epidemic:abc"]
    assert await ids(text="FAR TOO") == ["moss:test/b"]  # notes, case-insensitive
    assert await ids(text="water drops") == ["moss:test/a", "moss:test/b"]  # prompt
    assert await ids(text="weapons") == ["epidemic:abc"]  # title
    assert await ids(text="prawdziwe") == ["epidemic:abc"]
    assert await ids(text="rockfall") == ["moss:test/b"]  # tags
    assert await ids(text="cave", kind=Kind.BED, tags=["source:water"]) == ["moss:test/a"]


async def test_pools(cat):
    await cat.define_pool("storm.thunder", "none yet")
    await cat.put(_moss("a"))
    await cat.put(_moss("b", pools=("cave.bed", "cave.extra")))
    pools = await cat.pools()
    assert list(pools) == ["cave.bed", "cave.extra", "storm.thunder"]
    assert [c.id for c in pools["cave.bed"]] == ["moss:test/a", "moss:test/b"]
    assert pools["storm.thunder"] == []
    assert (await cat.pool_descriptions())["storm.thunder"] == "none yet"


async def test_export_is_deterministic(tmp_path):
    data = tmp_path / "data"
    async with Catalog(data) as c:
        await c.put(_moss("b"))
        await c.put(_epidemic())
        await c.put(_moss("a"))
        first = c.export_path.read_bytes()
        assert not await c.export()  # nothing changed: not rewritten
        await c.mark_used("moss:test/a", datetime(2026, 11, 15, 20, 0, tzinfo=UTC))
        assert (await c.get("moss:test/a")).last_used.year == 2026
        assert c.export_path.read_bytes() == first  # usage is not exported
        await c.put(await c.get("moss:test/a"))  # same content again
        assert c.export_path.read_bytes() == first
    async with Catalog(data) as c:
        c.export_path.unlink()
        await c.export()
        assert c.export_path.read_bytes() == first
    lines = first.decode().splitlines()
    assert [line.split('"name": "')[1].split('"')[0] for line in lines[:2]] == [
        "cave.bed",
        "swords.clash",
    ]
    ids = [line.split('"id": "')[-1].split('"')[0] for line in lines[2:]]
    assert ids == sorted(ids) == ["epidemic:abc", "moss:test/a", "moss:test/b"]
    assert '"bed_lufs": -31.25' in lines[3] and '"integrated_lufs": -30.04' in lines[3]
    assert "last_used" not in first.decode()
    assert "Brzmi jak prawdziwe" in first.decode()  # UTF-8, not escaped


async def test_export_after_every_change(cat):
    await cat.put(_moss("a"))
    assert "moss:test/a" in cat.export_path.read_text()
    await cat.update("moss:test/a", rating=3)
    assert '"rating": 3' in cat.export_path.read_text()
    await cat.delete("moss:test/a")
    assert "moss:test/a" not in cat.export_path.read_text()


async def test_batch_exports_once_and_rolls_back(cat, monkeypatch):
    calls = []
    real = cat.export
    monkeypatch.setattr(cat, "export", lambda: calls.append(1) or real())
    async with cat.batch():
        await cat.put(_moss("a"))
        await cat.put(_moss("b"))
    assert calls == [1]
    with pytest.raises(RuntimeError):
        async with cat.batch():
            await cat.put(_moss("c"))
            raise RuntimeError
    assert await cat.get("moss:test/c") is None


async def test_child_tasks_cannot_use_the_batch_session(cat):
    """A task started inside a batch inherits the batch's context vars; sharing one AsyncSession
    between tasks is not safe, so such a task gets a clear error instead."""
    with pytest.raises(CatalogError, match="only the task that opened"):
        async with cat.batch():
            await cat.put(_moss("a"))
            await asyncio.gather(cat.put(_moss("b")), cat.put(_moss("c")))
    assert await cat.get("moss:test/a") is None  # the whole batch was rolled back
    assert await cat.get("moss:test/b") is None


async def test_export_writes_only_after_a_successful_commit(cat, monkeypatch):
    """A failed commit must leave no catalog.jsonl ahead of the database."""

    class FailingCommit(AsyncSession):
        async def commit(self):
            raise RuntimeError("commit failed")

    monkeypatch.setattr(
        cat,
        "_sessions",
        async_sessionmaker(cat._engine, class_=FailingCommit, expire_on_commit=False),
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        async with cat.batch():
            await cat.put(_moss("a"))
    assert not cat.export_path.exists()
    assert await cat.get("moss:test/a") is None


async def test_export_file_io_runs_in_a_thread(cat, monkeypatch):
    """catalog.jsonl is written in a worker thread, not on the event loop."""
    calls = []
    real = asyncio.to_thread

    async def spy(func, *args):
        calls.append(func)
        return await real(func, *args)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    await cat.put(_moss("a"))  # the batch exports at its end
    assert any(getattr(f, "__name__", "") == "_write_export" for f in calls)
    assert cat.export_path.is_file()

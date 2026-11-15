"""The clip catalog: SQLite at `<data>/catalog.sqlite` (see `db.py`, `tables.py`), exported to
`<data>/catalog.jsonl`.

The repository maps rows of `tables.py` to the pydantic models of `rpghome.models.clip` and back;
its API is async (SQLAlchemy's AsyncSession). Opening a catalog upgrades its database to the
newest Alembic migration.

The SQLite file is outside the data folder's git; the export is tracked, so it is deterministic:
one JSON object per line, pools first (by name), then clips (by id), keys sorted, floats rounded
(by the models). It is rewritten after every change, and only when its bytes change. `last_used`
is usage state, not catalog content: it stays in SQLite and is left out of the export, so playing
a clip makes no diff.
"""

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Hashable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rpghome.core.errors import RpgHomeError
from rpghome.models import tags as vocabulary
from rpghome.models.clip import Clip, Kind, Source, Status, check_pool_name
from rpghome.repositories import db
from rpghome.repositories.tables import ClipPoolRow, ClipRow, ClipTagRow, PoolRow

DB_NAME = db.DB_NAME
EXPORT_NAME = "catalog.jsonl"

# Clip fields stored in columns of the same name. Tags and pools have their own tables,
# search_text is derived, last_used is not exported (and kept as a datetime).
_COLUMNS = tuple(
    c.key for c in ClipRow.__table__.columns if c.key not in ("search_text", "last_used")
)


class CatalogError(RpgHomeError):
    pass


def clip_record(clip: Clip) -> dict[str, Any]:
    """The clip as exported: everything but `last_used`."""
    return clip.model_dump(mode="json", exclude={"last_used"})


def _dumps(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _to_clip(row: ClipRow) -> Clip:
    return Clip.model_validate(
        {
            **{name: getattr(row, name) for name in _COLUMNS},
            "tags": [t.tag for t in row.tag_rows],
            "pools": [p.pool for p in row.pool_rows],
            "last_used": row.last_used,
        }
    )


def _sync[R, K: Hashable](
    rows: list[R], wanted: tuple[K, ...], key: Callable[[R], K], make: Callable[[K], R]
) -> None:
    """Make a relationship list hold exactly `wanted`, touching only what differs."""
    have = {key(r): r for r in rows}
    for k, r in have.items():
        if k not in wanted:
            rows.remove(r)
    rows.extend(make(k) for k in wanted if k not in have)


def _fill(row: ClipRow, clip: Clip) -> None:
    data = clip.model_dump(mode="json", exclude={"tags", "pools", "last_used"})
    for name in _COLUMNS:
        setattr(row, name, data[name])
    row.last_used = clip.last_used
    row.search_text = clip.search_text()
    _sync(row.tag_rows, clip.tags, lambda r: r.tag, lambda t: ClipTagRow(tag=t))
    _sync(row.pool_rows, clip.pools, lambda r: r.pool, lambda p: ClipPoolRow(pool=p))


class Catalog:
    """`async with Catalog(data_dir) as catalog:` opens it (upgrading the database) and closes
    it. Group many changes in `async with catalog.batch():` to write them in one transaction and
    export once at the end; only the task that opened a batch may use it. Without `data_dir`,
    the configured data folder."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else db.default_path().parent
        self.db_path = db.database_path(self.data_dir)
        self.export_path = self.data_dir / EXPORT_NAME
        self._engine: AsyncEngine | None = None
        self._sessions: async_sessionmaker[AsyncSession] | None = None
        # (session, opening task) of the batch the current context is in, if any. A task started
        # inside a batch inherits this context but is not the opening task (see `_batch_session`).
        self._current: ContextVar[tuple[AsyncSession, asyncio.Task | None] | None] = ContextVar(
            f"catalog-batch-{id(self)}", default=None
        )

    async def open(self) -> Self:
        if self._engine is None:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            try:
                await db.upgrade(self.db_path)
            except db.DatabaseError as exc:
                raise CatalogError(str(exc)) from None
            self._engine = db.create_engine(self.db_path)
            self._sessions = db.session_factory(self._engine)
        return self

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = self._sessions = None

    async def __aenter__(self) -> Self:
        return await self.open()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- sessions, transactions and export ------------------------------------------------

    def _new_session(self) -> AsyncSession:
        if self._sessions is None:
            raise CatalogError("the catalog is not open: use `async with Catalog(...)`")
        return self._sessions()

    def _batch_session(self) -> AsyncSession | None:
        """The session of the batch this context is in, or None outside a batch. A task started
        inside a batch inherits the batch's context vars — sharing one AsyncSession between
        tasks is not safe, so only the task that opened the batch may use it."""
        current = self._current.get()
        if current is None:
            return None
        session, opener = current
        if opener is not asyncio.current_task():
            raise CatalogError(
                "only the task that opened `catalog.batch()` may use it: this task was started "
                "inside the batch and would share its session"
            )
        return session

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """The current batch's session, or a new one for a read."""
        current = self._batch_session()
        if current is not None:
            yield current
            return
        async with self._new_session() as session:
            yield session

    @asynccontextmanager
    async def batch(self, export: bool = True) -> AsyncIterator[Self]:
        """One write transaction (BEGIN IMMEDIATE: other writers wait). The export runs once, at
        the end of the outermost batch, after the commit succeeds: a failed commit leaves no
        export ahead of the database."""
        if self._batch_session() is not None:
            yield self
            return
        async with self._new_session() as session:
            await session.connection(execution_options=db.IMMEDIATE)
            token = self._current.set((session, asyncio.current_task()))
            try:
                yield self
                await session.commit()
                if export:
                    await self.export()
            except BaseException:
                await session.rollback()
                raise
            finally:
                self._current.reset(token)

    async def export_text(self) -> str:
        lines = [
            _dumps({"record": "pool", "name": name, "description": description})
            for name, description in (await self.pool_descriptions()).items()
        ]
        lines += [_dumps({"record": "clip", **clip_record(c)}) for c in await self.all()]
        return "".join(line + "\n" for line in lines)

    async def export(self) -> bool:
        """Write catalog.jsonl if its content changed. Returns whether it was written.
        The file I/O runs in a worker thread, never on the event loop."""
        return await asyncio.to_thread(self._write_export, (await self.export_text()).encode())

    def _write_export(self, text: bytes) -> bool:
        if self.export_path.exists() and self.export_path.read_bytes() == text:
            return False
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".catalog.", suffix=".jsonl.tmp")
        try:
            os.fchmod(fd, 0o644)  # mkstemp makes it 0600
            with os.fdopen(fd, "wb") as f:
                f.write(text)
            os.replace(tmp, self.export_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True

    # -- clips ----------------------------------------------------------------------------

    async def get(self, clip_id: str) -> Clip | None:
        async with self._session() as session:
            row = await session.get(ClipRow, clip_id)
            return _to_clip(row) if row else None

    async def all(self) -> list[Clip]:
        return await self.search()

    async def put(self, clip: Clip) -> None:
        """Insert or replace a clip (by id), with its tags and pools."""
        async with self.batch(), self._session() as session:
            for pool in clip.pools:
                await session.execute(insert(PoolRow).values(name=pool).on_conflict_do_nothing())
            row = await session.get(ClipRow, clip.id)
            if row is None:
                row = ClipRow(id=clip.id, tag_rows=[], pool_rows=[])
                session.add(row)
            _fill(row, clip)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise CatalogError(
                    f"cannot store {clip.id}: {exc.orig} (path {clip.path})"
                ) from None

    async def update(self, clip_id: str, **changes: Any) -> Clip:
        """Change some fields of a stored clip, e.g. `update(id, rating=4, status=...)`.
        Read and write happen in one transaction."""
        async with self.batch():
            clip = await self.get(clip_id)
            if clip is None:
                raise CatalogError(f"no clip {clip_id!r}")
            clip = clip.changed(**changes)
            await self.put(clip)
        return clip

    async def delete(self, clip_id: str) -> bool:
        """Remove a clip from the catalog (the audio file stays). Returns whether it existed."""
        async with self.batch(), self._session() as session:
            row = await session.get(ClipRow, clip_id)
            if row is None:
                return False
            await session.delete(row)
            await session.flush()
        return True

    async def mark_used(self, clip_id: str, when: datetime | None = None) -> None:
        async with self.batch(), self._session() as session:
            row = await session.get(ClipRow, clip_id)
            if row is not None:
                row.last_used = when or datetime.now(UTC)

    async def search(
        self,
        text: str | None = None,
        tags: list[str] | tuple[str, ...] = (),
        kind: Kind | None = None,
        status: Status | None = None,
        source: Source | None = None,
    ) -> list[Clip]:
        """Clips matching all the given filters, by id. Tags combine with AND; a plain tag such
        as `cave` also matches category tags with that value (`place:cave`). Text is a
        case-insensitive substring of the title, prompt, notes or tags."""
        query = select(ClipRow).order_by(ClipRow.id)
        for t in tags:
            q = vocabulary.normalize(t)
            match = ClipTagRow.tag == q
            if ":" not in q:
                match = or_(match, ClipTagRow.tag.endswith(f":{q}", autoescape=True))
            query = query.where(exists().where(ClipTagRow.clip_id == ClipRow.id, match))
        for column, value in (
            (ClipRow.kind, kind),
            (ClipRow.status, status),
            (ClipRow.source, source),
        ):
            if value is not None:
                query = query.where(column == value.value)
        if text:
            query = query.where(func.instr(ClipRow.search_text, text.casefold()) > 0)
        async with self._session() as session:
            return [_to_clip(row) for row in await session.scalars(query)]

    # -- pools ----------------------------------------------------------------------------

    async def define_pool(self, name: str, description: str = "") -> None:
        """Create a pool, or change its description. A pool may stay empty."""
        check_pool_name(name)
        statement = insert(PoolRow).values(name=name, description=description)
        statement = statement.on_conflict_do_update(
            index_elements=[PoolRow.name], set_={"description": description}
        )
        async with self.batch(), self._session() as session:
            await session.execute(statement)

    async def pool_descriptions(self) -> dict[str, str]:
        async with self._session() as session:
            rows = await session.execute(
                select(PoolRow.name, PoolRow.description).order_by(PoolRow.name)
            )
            return {name: description for name, description in rows}

    async def pools(self) -> dict[str, list[Clip]]:
        """Pool name -> its clips (by id), every defined pool, empty ones too."""
        out: dict[str, list[Clip]] = {name: [] for name in await self.pool_descriptions()}
        query = (
            select(ClipPoolRow.pool, ClipRow)
            .join(ClipRow, ClipRow.id == ClipPoolRow.clip_id)
            .order_by(ClipPoolRow.pool, ClipRow.id)
        )
        async with self._session() as session:
            for pool, row in await session.execute(query):
                out[pool].append(_to_clip(row))
        return out

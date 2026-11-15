"""The catalog's ORM tables (SQLAlchemy 2.0, declarative). They are the database side only:
the repository (`catalog.py`) maps rows to the pydantic models of `rpghome.models.clip` and back,
so `models/` never depends on the database.

The schema is created and changed only by the Alembic migrations in `migrations/` at the repo
root, never by `create_all`; `tests/repositories/test_migrations.py` checks that the migrations
and these tables match.

Every clip field that is searched, filtered or sorted by has its own column; source details,
loudness and the imported values are JSON. Column names are the `Clip` field names, so the
repository maps them by name.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, MetaData, String, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Names for constraints and indexes, so migrations can refer to them (SQLite batch mode).
NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class UtcDateTime(TypeDecorator[datetime]):
    """An aware datetime, stored as naive UTC (SQLite has no time zones), read back as UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"a naive datetime cannot be stored: {value}")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


Json = JSON(none_as_null=True)  # None is SQL NULL, not the JSON text 'null'


class ClipRow(Base):
    __tablename__ = "clips"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String, index=True)
    license: Mapped[str] = mapped_column(String, index=True)
    rating: Mapped[int | None]
    title: Mapped[str] = mapped_column(String)
    last_used: Mapped[datetime | None] = mapped_column(UtcDateTime)
    perspective: Mapped[str | None] = mapped_column(String)
    dry: Mapped[bool | None]
    loopable: Mapped[bool]
    gain_db: Mapped[float | None]
    duration_s: Mapped[float | None]
    channels: Mapped[int | None]
    sample_rate: Mapped[int | None]
    notes: Mapped[str] = mapped_column(String)
    sha256: Mapped[str | None] = mapped_column(String)
    details: Mapped[dict[str, Any] | None] = mapped_column(Json)
    loudness: Mapped[dict[str, Any] | None] = mapped_column(Json)
    imported: Mapped[dict[str, Any] | None] = mapped_column(Json)
    # Title, notes, tags and prompt (or Epidemic title), casefolded: what a text search reads.
    search_text: Mapped[str] = mapped_column(String)

    tag_rows: Mapped[list["ClipTagRow"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )
    pool_rows: Mapped[list["ClipPoolRow"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )


class ClipTagRow(Base):
    __tablename__ = "clip_tags"

    clip_id: Mapped[str] = mapped_column(
        ForeignKey("clips.id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(String, primary_key=True, index=True)


class PoolRow(Base):
    __tablename__ = "pools"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str] = mapped_column(String, default="", server_default="")


class ClipPoolRow(Base):
    __tablename__ = "clip_pools"

    clip_id: Mapped[str] = mapped_column(
        ForeignKey("clips.id", ondelete="CASCADE"), primary_key=True
    )
    pool: Mapped[str] = mapped_column(
        ForeignKey("pools.name", ondelete="CASCADE"), primary_key=True, index=True
    )

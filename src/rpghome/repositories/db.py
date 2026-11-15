"""The catalog database: `<data>/catalog.sqlite`, through SQLAlchemy's asyncio extension and
aiosqlite, in WAL mode with foreign keys on (set on every new connection).

Transactions: pysqlite's own transaction handling is switched off and the "begin" event emits
`BEGIN`, or `BEGIN IMMEDIATE` on a connection with the execution option `IMMEDIATE` (the
repository's write batches): such a transaction takes the write lock at its start, so a
read-then-write never fails half-way because another process wrote in between. A busy database
is waited for, up to `BUSY_TIMEOUT_S`.

The schema comes from the Alembic migrations in `migrations/` at the repo root. `upgrade` (the
catalog calls it when it opens) first reads the current revision and only takes the write lock
when the database is behind head; the migration then runs on the caller's event loop, through
Alembic's connection-sharing recipe: the connection goes into `config.attributes["connection"]`
and `migrations/env.py` uses it.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Connection, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

from rpghome.core.config import load_settings
from rpghome.core.errors import RpgHomeError

DB_NAME = "catalog.sqlite"
# src/rpghome/repositories/db.py -> the repo root. The app runs from its checkout (uv, .venv).
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
BUSY_TIMEOUT_S = 30
IMMEDIATE = {"sqlite_begin": "IMMEDIATE"}


class DatabaseError(RpgHomeError):
    pass


def database_path(data_dir: Path) -> Path:
    return Path(data_dir) / DB_NAME


def database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def create_engine(path: Path, *, foreign_keys: bool = True) -> AsyncEngine:
    """An engine for the SQLite file at `path` (its folder is created)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = _create_async_engine(database_url(path), connect_args={"timeout": BUSY_TIMEOUT_S})

    @event.listens_for(engine.sync_engine, "connect")
    def _connect(dbapi_connection: DBAPIConnection, _: ConnectionPoolEntry) -> None:
        dbapi_connection.isolation_level = None  # type: ignore[attr-defined]  # see "begin"
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(connection: Connection) -> None:
        mode = connection.get_execution_options().get("sqlite_begin", "")
        connection.exec_driver_sql(f"BEGIN {mode}".rstrip())

    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def default_path() -> Path:
    """The catalog of the configured data folder (`RPGHOME_ARTIFACTS`)."""
    return database_path(load_settings().artifacts_dir)


def default_engine() -> AsyncEngine:
    return create_engine(default_path())


def alembic_config() -> Config:
    """The Alembic config the app uses: no ini file (the URL comes from the settings or the
    shared connection, and the app's logging is left alone)."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def run_upgrade(connection: Connection, revision: str = "head") -> None:
    """`alembic upgrade` on an open (sync) connection, inside its transaction."""
    config = alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _cause(exc: SQLAlchemyError) -> str:
    orig = getattr(exc, "orig", None)
    return str(orig) if orig is not None else str(exc)


async def _read_state(engine: AsyncEngine, path: Path) -> tuple[str | None, list[str]]:
    """(alembic revision, table names) of the database, with a plain read (a WAL reader never
    waits for a writer). A file that is not a SQLite database raises `DatabaseError`."""
    try:
        async with engine.connect() as conn:
            tables = [
                row[0]
                for row in await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ]
            revision = None
            if "alembic_version" in tables:
                revision = (
                    await conn.exec_driver_sql("SELECT version_num FROM alembic_version")
                ).scalar()
            return revision, tables
    except SQLAlchemyError as exc:
        raise DatabaseError(f"{path} is not a valid database: {_cause(exc)}") from None


async def upgrade(path: Path) -> None:
    """Bring the database at `path` to the newest migration (creating it if needed).

    A file that is not a SQLite database, or a catalog made by an older rpghome (its own
    `PRAGMA user_version` schema, no `alembic_version` table), raises `DatabaseError` with a
    message saying what to do, instead of a raw driver error.

    The current revision is read first, with a plain read (a WAL reader never waits for a
    writer): only a database that is behind head takes the write lock, so opening a catalog for
    `search` or `pools` is never blocked by another process's write batch.

    The migration itself runs in one `BEGIN IMMEDIATE` transaction, so two processes upgrading
    at once take turns, and on its own connection with foreign keys off: SQLite's batch
    migrations copy a table and drop the old one, and a DROP TABLE with foreign keys on would
    fire the ON DELETE CASCADE of the rows that refer to it. `PRAGMA foreign_key_check` checks
    the result instead.
    """
    engine = create_engine(path, foreign_keys=False)
    try:
        revision, tables = await _read_state(engine, path)
        if revision == head_revision():
            return  # at head: a read was enough, no write lock
        # Tables but no revision: a catalog from the old sqlite3 code (or any foreign schema).
        # An empty `alembic_version` (a downgrade to base) is not that: it re-upgrades below.
        if revision is None and any(name != "alembic_version" for name in tables):
            raise DatabaseError(
                f"{path} has an unknown schema (no alembic_version); delete it and re-import"
            )
        async with engine.execution_options(**IMMEDIATE).begin() as conn:
            try:
                await conn.run_sync(run_upgrade)
            except CommandError as exc:
                raise DatabaseError(
                    f"{path}: {exc} (made by a newer rpghome, or a broken migration history?)"
                ) from None
            except SQLAlchemyError as exc:
                raise DatabaseError(f"{path}: migration failed: {_cause(exc)}") from None
            broken = (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            if broken:
                raise DatabaseError(f"{path}: the migration broke foreign keys: {broken[:5]}")
    finally:
        await engine.dispose()

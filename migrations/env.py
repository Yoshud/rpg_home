"""Alembic environment of the clip catalog.

- The app (`rpghome.repositories.db.upgrade`) passes its open connection in
  `config.attributes["connection"]`; the migrations then run on it, inside its transaction.
- The `alembic` command line has none: it opens the catalog of the configured data folder
  (`load_settings()`, i.e. `RPGHOME_ARTIFACTS`); the URL is never taken from alembic.ini.

`render_as_batch`: SQLite cannot do most ALTER TABLEs, so autogenerate writes batch operations
(copy the table, move the rows, swap).
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from rpghome.repositories import db
from rpghome.repositories.tables import Base, UtcDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Migrations do not import the app's column types, which may change: UtcDateTime is
    written as the plain DateTime it stores."""
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime()"
    return False  # the default rendering


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=_render_item,
        # db.create_engine switches pysqlite's transaction handling off and emits BEGIN itself,
        # so SQLite's DDL is transactional: a failed migration leaves nothing behind.
        transactional_ddl=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: print the SQL instead of running it."""
    _configure(
        url=db.database_url(db.default_path()),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = db.create_engine(db.default_path(), foreign_keys=False)  # see db.upgrade
    try:
        async with engine.execution_options(**db.IMMEDIATE).begin() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""Alembic environment.

The URL is taken from ``config.attributes["connection"]`` / the ``sqlalchemy.url`` option
when tests pass one in, otherwise from VANTAGE_DATABASE_URL via app settings.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

import app.models  # noqa: F401  (registers all tables)
from app.core.config import get_settings, normalize_database_url
from app.core.db import Base, make_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    return normalize_database_url(url) if url else get_settings().database_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite can't ALTER most things; batch mode rebuilds the table instead.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(_url())
    try:
        with engine.connect() as conn:
            sqlite = conn.dialect.name == "sqlite"
            if sqlite:
                # Batch mode rebuilds a table (copy, drop, rename). With foreign keys on,
                # dropping the old table fires ON DELETE actions in the tables that point
                # at it (SET NULL, CASCADE). The pragma only works outside a transaction.
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
                conn.commit()
            _configure(conn)
            with context.begin_transaction():
                context.run_migrations()
            if sqlite:
                broken = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                conn.commit()
                if broken:
                    raise RuntimeError(f"migration left {len(broken)} broken foreign key(s)")
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

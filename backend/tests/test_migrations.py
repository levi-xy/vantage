from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

import app.models  # noqa: F401
from app.core.db import Base, make_engine
from tests.conftest import alembic_config

EXPECTED_TABLES = {
    "users",
    "instances",
    "links",
    "dashboards",
    "alert_rules",
    "alert_events",
    "channels",
    "audit_log",
    "settings",
    # Added in M1.3 with maintainer approval (not in the original Contract 2).
    "sessions",
    # Added in M1.8 with maintainer approval.
    "login_failures",
}


def test_single_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1


def test_upgrade_creates_every_contract_table(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert tables == EXPECTED_TABLES | {"alembic_version"}


def test_migrations_match_models(engine: Engine) -> None:
    """Fails if a model changes without a migration."""
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_downgrade_removes_everything(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    eng = make_engine(db_url)
    try:
        assert set(inspect(eng).get_table_names()) <= {"alembic_version"}
    finally:
        eng.dispose()

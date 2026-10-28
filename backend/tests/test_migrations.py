import json

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

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


# A plain string: raw SQL bypasses the model's UTC datetime type.
STAMP = "2026-01-01 00:00:00"


def test_0004_backfills_actor_type(db_url: str) -> None:
    """CLI rows were told apart only by an empty user_id plus changes.source == "cli"."""
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0003")
    eng = make_engine(db_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, role, created_at) "
                    "VALUES (1, 'admin', 'h', 'admin', :at)"
                ),
                {"at": STAMP},
            )
            rows = [
                (1, 1, "create", {"name": "dev1"}),  # through the API
                (2, None, "update", {"password": "<changed>", "source": "cli"}),  # CLI
                (3, None, "login_failed", {"username": "<unknown>"}),  # API, no user
                (4, None, "delete", {"name": "dev1"}),  # API, user since deleted
                (5, 1, "update", {"source": "cli"}),  # has a user: not the CLI
            ]
            for row_id, user_id, action, changes in rows:
                conn.execute(
                    text(
                        "INSERT INTO audit_log (id, user_id, action, target_type, at, changes) "
                        "VALUES (:id, :user_id, :action, 'user', :at, :changes)"
                    ),
                    {
                        "id": row_id,
                        "user_id": user_id,
                        "action": action,
                        "at": STAMP,
                        "changes": json.dumps(changes),
                    },
                )
        command.upgrade(cfg, "head")
        with eng.connect() as conn:
            got = {r[0]: r[1] for r in conn.execute(text("SELECT id, actor_type FROM audit_log"))}
        assert got == {1: "user", 2: "cli", 3: "user", 4: "user", 5: "user"}

        # The column is required and limited to the three values.
        insert = (
            "INSERT INTO audit_log (user_id, {cols}action, target_type, at, changes) "
            "VALUES (NULL, {vals}'create', 'user', :at, '{{}}')"
        )
        for cols, vals in (("actor_type, ", "'robot', "), ("", "")):
            with pytest.raises(IntegrityError), eng.begin() as conn:
                conn.execute(text(insert.format(cols=cols, vals=vals)), {"at": STAMP})

        command.downgrade(cfg, "0003")
        assert "actor_type" not in {c["name"] for c in inspect(eng).get_columns("audit_log")}
    finally:
        eng.dispose()

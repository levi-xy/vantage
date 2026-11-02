import json

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401
from app.api.deps import CSRF_HEADER
from app.core.db import Base, make_engine
from tests.conftest import alembic_config
from tests.test_auth import do_setup, make_client

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
    # Added in P0-5 with maintainer approval (2026-11-01).
    "alert_notifications",
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


def test_0005_rewrites_only_untouched_ping_rules(db_url: str) -> None:
    old = 'vantage_ping_check_packet_loss_ratio{vantage_integration="ping_check"} >= 1'
    new = 'vantage_ping_check_packet_loss_ratio{vantage_integration="ping_check"} > 0.2'
    edited = old + " and on() vector(1)"
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0004")
    eng = make_engine(db_url)
    try:
        with eng.begin() as conn:
            for rule_id, expr, for_seconds in ((1, old, 180), (2, old, 600), (3, edited, 180)):
                conn.execute(
                    text(
                        "INSERT INTO alert_rules "
                        "(id, name, expr, for_seconds, severity, channel_ids, enabled) "
                        "VALUES (:id, 'ping', :expr, :for_seconds, 'critical', '[]', true)"
                    ),
                    {"id": rule_id, "expr": expr, "for_seconds": for_seconds},
                )
        command.upgrade(cfg, "head")
        with eng.connect() as conn:
            rules = {
                r[0]: (r[1], r[2])
                for r in conn.execute(text("SELECT id, expr, for_seconds FROM alert_rules"))
            }
            audits = conn.execute(
                text(
                    "SELECT target_id, user_id, actor_type, action, target_type, changes "
                    "FROM audit_log ORDER BY target_id"
                )
            ).all()
        assert rules == {1: (new, 300), 2: (new, 600), 3: (edited, 180)}
        assert [tuple(a[:5]) for a in audits] == [
            ("1", None, "system", "update", "alert_rule"),
            ("2", None, "system", "update", "alert_rule"),
        ]
        changes = [a[5] if isinstance(a[5], dict) else json.loads(a[5]) for a in audits]
        assert changes[0] == {
            "expr": {"old": old, "new": new},
            "for_seconds": {"old": 180, "new": 300},
        }
        assert changes[1] == {"expr": {"old": old, "new": new}}
    finally:
        eng.dispose()


def test_0006_keeps_legacy_events_readable(db_url: str) -> None:
    """Existing events become fingerprint "legacy" with their rule's name and severity, and
    still list through the API; a second open event per rule is resolved for the index."""
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0005")
    eng = make_engine(db_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO alert_rules "
                    "(id, name, expr, for_seconds, severity, channel_ids, enabled) "
                    "VALUES (1, 'Gateway', 'up == 0', 0, 'critical', '[]', true)"
                )
            )
            for event_id, state in ((1, "resolved"), (2, "firing"), (3, "firing")):
                conn.execute(
                    text(
                        "INSERT INTO alert_events (id, rule_id, state, started_at, summary) "
                        "VALUES (:id, 1, :state, :at, 'old')"
                    ),
                    {"id": event_id, "state": state, "at": STAMP},
                )
        command.upgrade(cfg, "head")
        with make_client(db_url) as client:
            client.headers[CSRF_HEADER] = do_setup(client)["csrf_token"]
            events = client.get("/api/v1/alerts/events").json()
        assert [(e["id"], e["state"], e["fingerprint"]) for e in events] == [
            (3, "firing", "legacy"),
            (2, "resolved", "legacy"),
            (1, "resolved", "legacy"),
        ]
        assert {(e["rule_id"], e["rule_name"], e["severity"]) for e in events} == {
            (1, "Gateway", "critical")
        }
        assert events[1]["end_reason"] == "rule_changed" and events[2]["end_reason"] is None

        # Downgrade keeps the events that still have their rule.
        command.downgrade(cfg, "0005")
        with eng.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM alert_events")).scalar() == 3
    finally:
        eng.dispose()


def test_0008_no_data_state_and_one_open_event_per_series(db_url: str) -> None:
    """``no_data`` is allowed, counts as open for the unique index, open events get a
    ``state_since``, and a downgrade turns ``no_data`` back into ``firing``."""
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0007")
    eng = make_engine(db_url)
    insert = text(
        "INSERT INTO alert_events (id, rule_id, fingerprint, labels, rule_name, severity, "
        "state, started_at, summary) VALUES (:id, 1, 'f1', '{}', 'G', 'critical', :state, "
        ":at, 'x')"
    )
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO alert_rules "
                    "(id, name, expr, for_seconds, severity, channel_ids, enabled) "
                    "VALUES (1, 'G', 'up == 0', 0, 'critical', '[]', true)"
                )
            )
            conn.execute(insert, {"id": 1, "state": "firing", "at": STAMP})
        command.upgrade(cfg, "0008")
        with eng.begin() as conn:
            assert conn.execute(text("SELECT state_since FROM alert_events")).scalar()
            conn.execute(text("UPDATE alert_events SET state = 'no_data' WHERE id = 1"))
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(insert, {"id": 2, "state": "firing", "at": STAMP})
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(insert, {"id": 3, "state": "bogus", "at": STAMP})
        notification = text(
            "INSERT INTO alert_notifications (event_id, kind, text, status, attempts, "
            "next_attempt_at, created_at) VALUES (1, :kind, 'x', 'pending', 0, :at, :at)"
        )
        with eng.begin() as conn:
            conn.execute(notification, {"kind": "no_data", "at": STAMP})
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(notification, {"kind": "bogus", "at": STAMP})
        command.downgrade(cfg, "0007")
        with eng.connect() as conn:
            assert conn.execute(text("SELECT state FROM alert_events")).scalar() == "firing"
            kind = conn.execute(text("SELECT kind FROM alert_notifications")).scalar()
            assert kind == "firing"
    finally:
        eng.dispose()


def test_0008_keeps_notification_links(db_url: str) -> None:
    """Rebuilding alert_events / alert_notifications (SQLite batch mode) must not fire
    ON DELETE SET NULL in the rows that point at them, either way."""
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0007")
    eng = make_engine(db_url)

    def links() -> list[tuple[int, int | None, int | None]]:
        with eng.connect() as conn:
            rows = conn.execute(
                text("SELECT id, event_id, folded_into FROM alert_notifications ORDER BY id")
            )
            return [tuple(r) for r in rows]  # type: ignore[misc]

    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO alert_rules "
                    "(id, name, expr, for_seconds, severity, channel_ids, enabled) "
                    "VALUES (1, 'G', 'up == 0', 0, 'critical', '[]', true)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO alert_events (id, rule_id, fingerprint, labels, rule_name, "
                    "severity, state, started_at, summary) VALUES "
                    "(5, 1, 'f1', '{}', 'G', 'critical', 'firing', :at, 'x')"
                ),
                {"at": STAMP},
            )
            for nid, folded in ((1, None), (2, 1)):
                conn.execute(
                    text(
                        "INSERT INTO alert_notifications (id, event_id, kind, text, status, "
                        "attempts, next_attempt_at, created_at, folded_into) VALUES "
                        "(:id, 5, 'firing', 'x', 'pending', 0, :at, :at, :folded)"
                    ),
                    {"id": nid, "at": STAMP, "folded": folded},
                )
        expected = [(1, 5, None), (2, 5, 1)]
        command.upgrade(cfg, "0008")
        assert links() == expected
        command.downgrade(cfg, "0007")
        assert links() == expected
    finally:
        eng.dispose()

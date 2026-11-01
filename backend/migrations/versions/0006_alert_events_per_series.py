"""alert_events per series, history kept; alert_rules soft delete and revision (P0-3a)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01 12:00:00.000000

Additive. ``alert_events`` gains the alert identity (``fingerprint``), snapshots taken at
open (``labels``, ``instance_id``, ``rule_name``, ``severity``) and ``end_reason``; its
``rule_id`` becomes nullable with ON DELETE SET NULL, so events outlive their rule. A
partial unique index allows one open event per (rule_id, fingerprint).

Existing events get ``fingerprint = "legacy"``, empty ``labels`` and their rule's name and
severity. The old evaluator kept one open event per rule; should a rule have more than one,
all but the newest are resolved (``end_reason = rule_changed``) so the index can be built.

Downgrade can't restore everything: it drops the new columns (identity, snapshots, end
reasons), deletes soft-deleted rules and every event without a rule (the old schema needs
one), and turns ``rule_id`` back into ON DELETE CASCADE.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_END_REASONS = ("recovered", "rule_disabled", "rule_deleted", "rule_changed", "closed_by_user")
_OPEN_INDEX = "ux_alert_events_open_series"
_FIRING = sa.text("state = 'firing'")

_events = sa.table(
    "alert_events",
    sa.column("id", sa.Integer()),
    sa.column("rule_id", sa.Integer()),
    sa.column("state", sa.String()),
    sa.column("ended_at", sa.DateTime(timezone=True)),
    sa.column("fingerprint", sa.String()),
    sa.column("labels", sa.JSON()),
    sa.column("rule_name", sa.String()),
    sa.column("severity", sa.String()),
    sa.column("end_reason", sa.String()),
)
_rules = sa.table(
    "alert_rules",
    sa.column("id", sa.Integer()),
    sa.column("name", sa.String()),
    sa.column("severity", sa.String()),
    sa.column("deleted_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    # Never batch-rebuild alert_rules: on SQLite (foreign keys on) dropping the old table
    # would cascade and delete every event. Plain ADD/DROP COLUMN works on SQLite >= 3.35.
    op.add_column("alert_rules", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column(
        "alert_rules",
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )

    with op.batch_alter_table("alert_events") as batch:
        batch.add_column(sa.Column("fingerprint", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("labels", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("instance_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("rule_name", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("severity", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("end_reason", sa.String(length=14), nullable=True))

    conn = op.get_bind()
    op.execute(
        _events.update().values(
            fingerprint="legacy",
            labels={},
            rule_name=sa.select(_rules.c.name)
            .where(_rules.c.id == _events.c.rule_id)
            .scalar_subquery(),
            severity=sa.select(_rules.c.severity)
            .where(_rules.c.id == _events.c.rule_id)
            .scalar_subquery(),
        )
    )
    # Rows the subquery couldn't fill (none expected: rule_id was NOT NULL with CASCADE).
    op.execute(_events.update().where(_events.c.rule_name.is_(None)).values(rule_name=""))
    op.execute(_events.update().where(_events.c.severity.is_(None)).values(severity=""))

    # One open event per rule before the unique index: keep the newest.
    firing = conn.execute(
        sa.select(_events.c.id, _events.c.rule_id)
        .where(_events.c.state == "firing")
        .order_by(_events.c.rule_id, _events.c.id.desc())
    ).all()
    seen: set[int] = set()
    extra: list[int] = []
    for event_id, rule_id in firing:
        if rule_id in seen:
            extra.append(event_id)
        seen.add(rule_id)
    if extra:
        op.execute(
            _events.update()
            .where(_events.c.id.in_(extra))
            .values(state="resolved", ended_at=datetime.now(UTC), end_reason="rule_changed")
        )

    with op.batch_alter_table("alert_events") as batch:
        batch.alter_column("fingerprint", existing_type=sa.String(length=32), nullable=False)
        batch.alter_column("labels", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("rule_name", existing_type=sa.String(length=128), nullable=False)
        batch.alter_column("severity", existing_type=sa.String(length=16), nullable=False)
        batch.alter_column("rule_id", existing_type=sa.Integer(), nullable=True)
        batch.drop_constraint(op.f("fk_alert_events_rule_id_alert_rules"), type_="foreignkey")
        batch.create_foreign_key(
            op.f("fk_alert_events_rule_id_alert_rules"),
            "alert_rules",
            ["rule_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            op.f("fk_alert_events_instance_id_instances"),
            "instances",
            ["instance_id"],
            ["id"],
            ondelete="SET NULL",
        )
        allowed = ", ".join(f"'{value}'" for value in _END_REASONS)
        batch.create_check_constraint(
            op.f("ck_alert_events_alert_end_reason"), sa.text(f"end_reason IN ({allowed})")
        )
        batch.create_index(op.f("ix_alert_events_instance_id"), ["instance_id"], unique=False)
        batch.create_index(
            _OPEN_INDEX,
            ["rule_id", "fingerprint"],
            unique=True,
            sqlite_where=_FIRING,
            postgresql_where=_FIRING,
        )


def downgrade() -> None:
    # The old schema has no event without a rule and no deleted rule.
    deleted = sa.select(_rules.c.id).where(_rules.c.deleted_at.is_not(None))
    op.execute(
        _events.delete().where(sa.or_(_events.c.rule_id.is_(None), _events.c.rule_id.in_(deleted)))
    )
    op.execute(_rules.delete().where(_rules.c.deleted_at.is_not(None)))

    with op.batch_alter_table("alert_events") as batch:
        batch.drop_index(_OPEN_INDEX)
        batch.drop_index(op.f("ix_alert_events_instance_id"))
        batch.drop_constraint(op.f("ck_alert_events_alert_end_reason"), type_="check")
        batch.drop_constraint(op.f("fk_alert_events_instance_id_instances"), type_="foreignkey")
        batch.drop_constraint(op.f("fk_alert_events_rule_id_alert_rules"), type_="foreignkey")
        batch.create_foreign_key(
            op.f("fk_alert_events_rule_id_alert_rules"),
            "alert_rules",
            ["rule_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.alter_column("rule_id", existing_type=sa.Integer(), nullable=False)
        batch.drop_column("end_reason")
        batch.drop_column("severity")
        batch.drop_column("rule_name")
        batch.drop_column("instance_id")
        batch.drop_column("labels")
        batch.drop_column("fingerprint")

    op.drop_column("alert_rules", "revision")
    op.drop_column("alert_rules", "deleted_at")

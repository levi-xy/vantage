"""alert_notifications: the durable delivery outbox (P0-5)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01 18:00:00.000000

Additive: a new table, nothing existing changes. Downgrade drops it (and every queued or
delivered message record with it).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = ("firing", "resolved", "rule_change", "summary")
_STATUSES = ("pending", "sending", "delivered", "failed", "folded", "cancelled")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "alert_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=11), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=32), nullable=True),
        sa.Column("folded_into", sa.Integer(), nullable=True),
        sa.Column("error_kind", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"kind IN ({_in(_KINDS)})",
            name=op.f("ck_alert_notifications_alert_notification_kind"),
        ),
        sa.CheckConstraint(
            f"status IN ({_in(_STATUSES)})",
            name=op.f("ck_alert_notifications_alert_notification_status"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["alert_events.id"],
            name=op.f("fk_alert_notifications_event_id_alert_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            name=op.f("fk_alert_notifications_channel_id_channels"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["folded_into"],
            ["alert_notifications.id"],
            name=op.f("fk_alert_notifications_folded_into_alert_notifications"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_notifications")),
    )
    op.create_index(
        op.f("ix_alert_notifications_event_id"), "alert_notifications", ["event_id"], unique=False
    )
    op.create_index(
        "ix_alert_notifications_status_next",
        "alert_notifications",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_alert_notifications_channel_id_id",
        "alert_notifications",
        ["channel_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_alert_notifications_channel_id_id", table_name="alert_notifications")
    op.drop_index("ix_alert_notifications_status_next", table_name="alert_notifications")
    op.drop_index(op.f("ix_alert_notifications_event_id"), table_name="alert_notifications")
    op.drop_table("alert_notifications")

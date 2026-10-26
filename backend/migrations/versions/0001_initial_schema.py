"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-26 12:08:09.435502
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "type",
            sa.Enum("telegram", name="channel_type", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secrets", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channels")),
    )
    op.create_table(
        "dashboards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("layout", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboards")),
    )
    op.create_table(
        "instances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("site", sa.String(length=64), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secrets", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instances")),
        sa.UniqueConstraint("name", name=op.f("uq_instances_name")),
    )
    op.create_index(op.f("ix_instances_type_id"), "instances", ["type_id"], unique=False)

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("admin", "viewer", name="role", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("expr", sa.Text(), nullable=False),
        sa.Column("for_seconds", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=True),
        sa.Column("channel_ids", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["instances.id"],
            name=op.f("fk_alert_rules_instance_id_instances"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_rules")),
    )
    op.create_index(
        op.f("ix_alert_rules_instance_id"), "alert_rules", ["instance_id"], unique=False
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_audit_log_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(op.f("ix_audit_log_at"), "audit_log", ["at"], unique=False)
    op.create_index(op.f("ix_audit_log_user_id"), "audit_log", ["user_id"], unique=False)

    op.create_table(
        "links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("a_instance_id", sa.Uuid(), nullable=False),
        sa.Column("b_instance_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("a_port", sa.String(length=64), nullable=True),
        sa.Column("b_port", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["a_instance_id"],
            ["instances.id"],
            name=op.f("fk_links_a_instance_id_instances"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["b_instance_id"],
            ["instances.id"],
            name=op.f("fk_links_b_instance_id_instances"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_links")),
    )
    op.create_index(op.f("ix_links_a_instance_id"), "links", ["a_instance_id"], unique=False)
    op.create_index(op.f("ix_links_b_instance_id"), "links", ["b_instance_id"], unique=False)

    op.create_table(
        "alert_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "firing", "resolved", name="alert_state", native_enum=False, create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["alert_rules.id"],
            name=op.f("fk_alert_events_rule_id_alert_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_events")),
    )
    op.create_index(op.f("ix_alert_events_rule_id"), "alert_events", ["rule_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_alert_events_rule_id"), table_name="alert_events")

    op.drop_table("alert_events")
    op.drop_index(op.f("ix_links_b_instance_id"), table_name="links")
    op.drop_index(op.f("ix_links_a_instance_id"), table_name="links")

    op.drop_table("links")
    op.drop_index(op.f("ix_audit_log_user_id"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_at"), table_name="audit_log")

    op.drop_table("audit_log")
    op.drop_index(op.f("ix_alert_rules_instance_id"), table_name="alert_rules")

    op.drop_table("alert_rules")
    op.drop_table("users")
    op.drop_table("settings")
    op.drop_index(op.f("ix_instances_type_id"), table_name="instances")

    op.drop_table("instances")
    op.drop_table("dashboards")
    op.drop_table("channels")

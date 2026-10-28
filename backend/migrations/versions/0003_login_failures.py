"""persistent per-user login failure counters

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_failures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_login_failures_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_failures")),
    )
    op.create_index(
        "ix_login_failures_user_id_at", "login_failures", ["user_id", "at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_login_failures_user_id_at", table_name="login_failures")
    op.drop_table("login_failures")

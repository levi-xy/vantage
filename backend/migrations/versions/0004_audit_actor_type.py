"""audit_log.actor_type: who made the change (user, cli or system)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28 18:00:00.000000

Existing rows are backfilled: the admin CLI wrote ``user_id = NULL`` with
``changes.source = "cli"``; everything else came through the API (``user``). Nothing has
written ``system`` entries yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTOR_TYPES = ("user", "cli", "system")


def upgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.add_column(sa.Column("actor_type", sa.String(length=6), nullable=True))

    audit_log = sa.table(
        "audit_log",
        sa.column("user_id", sa.Integer()),
        sa.column("changes", sa.JSON()),
        sa.column("actor_type", sa.String()),
    )
    op.execute(audit_log.update().values(actor_type="user"))
    op.execute(
        audit_log.update()
        .where(
            audit_log.c.user_id.is_(None),
            audit_log.c.changes["source"].as_string() == "cli",
        )
        .values(actor_type="cli")
    )

    with op.batch_alter_table("audit_log") as batch:
        batch.alter_column("actor_type", existing_type=sa.String(length=6), nullable=False)
        # The same CHECK the model's non-native Enum creates (ck_audit_log_actor_type).
        allowed = ", ".join(f"'{value}'" for value in _ACTOR_TYPES)
        batch.create_check_constraint(
            op.f("ck_audit_log_actor_type"), sa.text(f"actor_type IN ({allowed})")
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.drop_constraint(op.f("ck_audit_log_actor_type"), type_="check")
        batch.drop_column("actor_type")

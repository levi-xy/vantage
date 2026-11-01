"""Rewrite alert rules made from the broken ping_check template

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01 12:00:00.000000

The old ``ping-check-down`` template (``packet_loss_ratio >= 1``) could never match: at
100% loss ``ping_check`` raises ``NoReply`` and reports no loss sample at all. A rule
whose ``expr`` is still exactly that template is rewritten to its replacement (loss above
20% for 5 min); its ``for_seconds`` changes only if it still has the template's value.
Edited rules are left alone. Each rewrite writes a ``system`` audit entry. "Host down" is
now the ``instance-down-ping_check`` template (``vantage_instance_up == 0``).
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies: later template edits must not change what this migration matches.
OLD_EXPR = 'vantage_ping_check_packet_loss_ratio{vantage_integration="ping_check"} >= 1'
OLD_FOR = 180
NEW_EXPR = 'vantage_ping_check_packet_loss_ratio{vantage_integration="ping_check"} > 0.2'
NEW_FOR = 300

_rules = sa.table(
    "alert_rules",
    sa.column("id", sa.Integer()),
    sa.column("expr", sa.Text()),
    sa.column("for_seconds", sa.Integer()),
)
_audit = sa.table(
    "audit_log",
    sa.column("user_id", sa.Integer()),
    sa.column("actor_type", sa.String()),
    sa.column("action", sa.String()),
    sa.column("target_type", sa.String()),
    sa.column("target_id", sa.String()),
    sa.column("at", sa.DateTime(timezone=True)),
    sa.column("changes", sa.JSON()),
)


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.select(_rules.c.id, _rules.c.for_seconds).where(_rules.c.expr == OLD_EXPR)
    ).all()
    now = datetime.now(UTC)
    for rule_id, for_seconds in rows:
        values: dict[str, object] = {"expr": NEW_EXPR}
        changes: dict[str, object] = {"expr": {"old": OLD_EXPR, "new": NEW_EXPR}}
        if for_seconds == OLD_FOR:
            values["for_seconds"] = NEW_FOR
            changes["for_seconds"] = {"old": OLD_FOR, "new": NEW_FOR}
        conn.execute(_rules.update().where(_rules.c.id == rule_id).values(**values))
        conn.execute(
            _audit.insert().values(
                user_id=None,
                actor_type="system",
                action="update",
                target_type="alert_rule",
                target_id=str(rule_id),
                at=now,
                changes=changes,
            )
        )


def downgrade() -> None:
    # No-op on purpose: putting back an expression that can never fire would bring the
    # bug back, and a rewritten rule can't be told apart from one an admin later set to
    # the new expression. The system audit entries record what changed.
    pass

"""alert_events.source / system_kind, channels.system_alerts (P0-4 self-monitoring)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-02 18:00:00.000000

Additive (maintainer-approved 2026-11-02):

* ``alert_events.source`` (``rule`` / ``system``, NOT NULL, existing rows ``rule``) and
  ``alert_events.system_kind`` (nullable). A system alert has no rule (``rule_id`` NULL)
  and a fixed kind; a partial unique index allows at most one open event per kind.
* ``channels.system_alerts`` (boolean, NOT NULL, default true): every existing channel
  receives system alerts until an admin turns them off.

On SQLite ``alert_events`` is rebuilt (batch mode, for the CHECK constraint on
``source``); ``migrations/env.py`` turns foreign keys off for the run, so the rebuild
doesn't fire ON DELETE SET NULL in ``alert_notifications.event_id``.

Older code doesn't know system events: it would read an open one (``rule_id`` NULL) as an
event whose rule is gone. Downgrade therefore resolves every open system event before it
drops the columns; their history rows stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_INDEX = "ux_alert_events_open_system"
_SYSTEM_OPEN = sa.text("state IN ('firing', 'no_data') AND system_kind IS NOT NULL")
_SOURCE_CHECK = "ck_alert_events_alert_event_source"

_events = sa.table(
    "alert_events",
    sa.column("source", sa.String()),
    sa.column("state", sa.String()),
    sa.column("ended_at", sa.DateTime(timezone=True)),
    sa.column("end_reason", sa.String()),
)


def upgrade() -> None:
    with op.batch_alter_table("alert_events") as batch:
        batch.add_column(
            sa.Column("source", sa.String(length=6), nullable=False, server_default="rule")
        )
        # The same CHECK the model's non-native Enum creates.
        batch.create_check_constraint(op.f(_SOURCE_CHECK), sa.text("source IN ('rule', 'system')"))
        batch.add_column(sa.Column("system_kind", sa.String(length=32), nullable=True))
        batch.create_index(
            _SYSTEM_INDEX,
            ["system_kind"],
            unique=True,
            sqlite_where=_SYSTEM_OPEN,
            postgresql_where=_SYSTEM_OPEN,
        )
    with op.batch_alter_table("channels") as batch:
        batch.add_column(
            sa.Column("system_alerts", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    op.execute(
        _events.update()
        .where(_events.c.source == "system", _events.c.state.in_(("firing", "no_data")))
        .values(state="resolved", ended_at=sa.func.current_timestamp(), end_reason="recovered")
    )
    with op.batch_alter_table("channels") as batch:
        batch.drop_column("system_alerts")
    with op.batch_alter_table("alert_events") as batch:
        batch.drop_index(_SYSTEM_INDEX)
        batch.drop_constraint(op.f(_SOURCE_CHECK), type_="check")
        batch.drop_column("system_kind")
        batch.drop_column("source")

"""alert_events: NO DATA state, its reason, state_since, reminder_sent_at (P0-3b)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-02 12:00:00.000000

Additive. ``alert_events.state`` may now be ``no_data`` (open, but no fresh value to tell
whether the condition still holds). New nullable columns: ``no_data_reason`` (free text
without a CHECK, so later reasons need no migration), ``state_since`` (when the event
entered its current state; existing open events get their ``started_at``) and
``reminder_sent_at``. ``alert_notifications.kind`` may now be ``no_data`` (the NO DATA
message and its reminder). The partial unique index now allows one open event per
(rule_id, fingerprint) across ``firing`` and ``no_data``.

Older code doesn't know ``no_data``: it treats such rows as neither firing nor resolved,
so after a downgrade they would never close. Downgrade therefore turns every ``no_data``
row back into ``firing`` before it drops the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN_INDEX = "ux_alert_events_open_series"
_STATE_CHECK = "ck_alert_events_alert_state"
_OPEN_NEW = sa.text("state IN ('firing', 'no_data')")
_OPEN_OLD = sa.text("state = 'firing'")
_KIND_CHECK = "ck_alert_notifications_alert_notification_kind"
_KINDS_OLD = "'firing', 'resolved', 'rule_change', 'summary'"
_KINDS_NEW = _KINDS_OLD + ", 'no_data'"

_notifications = sa.table("alert_notifications", sa.column("kind", sa.String()))
_events = sa.table(
    "alert_events",
    sa.column("state", sa.String()),
    sa.column("started_at", sa.DateTime(timezone=True)),
    sa.column("state_since", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    with op.batch_alter_table("alert_events") as batch:
        batch.add_column(sa.Column("no_data_reason", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("state_since", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True))
        batch.drop_index(_OPEN_INDEX)
        batch.drop_constraint(op.f(_STATE_CHECK), type_="check")
        batch.create_check_constraint(
            op.f(_STATE_CHECK), sa.text("state IN ('firing', 'resolved', 'no_data')")
        )
        batch.create_index(
            _OPEN_INDEX,
            ["rule_id", "fingerprint"],
            unique=True,
            sqlite_where=_OPEN_NEW,
            postgresql_where=_OPEN_NEW,
        )
    op.execute(
        _events.update().where(_events.c.state == "firing").values(state_since=_events.c.started_at)
    )
    with op.batch_alter_table("alert_notifications") as batch:
        batch.drop_constraint(op.f(_KIND_CHECK), type_="check")
        batch.create_check_constraint(op.f(_KIND_CHECK), sa.text(f"kind IN ({_KINDS_NEW})"))


def downgrade() -> None:
    # The old schema has no no_data kind: those messages become firing ones.
    op.execute(
        _notifications.update().where(_notifications.c.kind == "no_data").values(kind="firing")
    )
    with op.batch_alter_table("alert_notifications") as batch:
        batch.drop_constraint(op.f(_KIND_CHECK), type_="check")
        batch.create_check_constraint(op.f(_KIND_CHECK), sa.text(f"kind IN ({_KINDS_OLD})"))
    op.execute(_events.update().where(_events.c.state == "no_data").values(state="firing"))
    with op.batch_alter_table("alert_events") as batch:
        batch.drop_index(_OPEN_INDEX)
        batch.drop_constraint(op.f(_STATE_CHECK), type_="check")
        batch.create_check_constraint(
            op.f(_STATE_CHECK), sa.text("state IN ('firing', 'resolved')")
        )
        batch.create_index(
            _OPEN_INDEX,
            ["rule_id", "fingerprint"],
            unique=True,
            sqlite_where=_OPEN_OLD,
            postgresql_where=_OPEN_OLD,
        )
        batch.drop_column("reminder_sent_at")
        batch.drop_column("state_since")
        batch.drop_column("no_data_reason")

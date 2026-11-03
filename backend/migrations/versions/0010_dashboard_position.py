"""dashboards.position (multiple dashboards)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-02 20:00:00.000000

Additive (maintainer-approved 2026-11-02): ``dashboards.position`` (int, NOT NULL) orders
the dashboard tabs. Existing dashboards keep their ids and layouts and get dense
positions in id order, so dashboard 1 stays the main one.

Ids are never reused: on SQLite the table is rebuilt with AUTOINCREMENT (Postgres
sequences never reuse). Dashboard 1 used to be inserted with an explicit id, which on
Postgres leaves the id sequence behind; the upgrade moves the sequence to the highest id
so new dashboards get fresh ids.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_dashboards = sa.table(
    "dashboards", sa.column("id", sa.Integer()), sa.column("position", sa.Integer())
)


def upgrade() -> None:
    # On SQLite the table is rebuilt with AUTOINCREMENT, so a deleted dashboard's id is
    # never reused (a tab still open on /d/<id> must not reach a new dashboard).
    with op.batch_alter_table(
        "dashboards", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch:
        batch.add_column(sa.Column("position", sa.Integer(), nullable=False, server_default="0"))
    bind = op.get_bind()
    ids = bind.execute(sa.select(_dashboards.c.id).order_by(_dashboards.c.id)).scalars().all()
    for position, dashboard_id in enumerate(ids):
        bind.execute(
            _dashboards.update().where(_dashboards.c.id == dashboard_id).values(position=position)
        )
    if bind.dialect.name == "postgresql" and ids:
        op.execute(
            "SELECT setval(pg_get_serial_sequence('dashboards', 'id'), "
            "(SELECT MAX(id) FROM dashboards))"
        )


def downgrade() -> None:
    with op.batch_alter_table("dashboards") as batch:
        batch.drop_column("position")

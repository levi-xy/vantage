"""instances.color (device colour-coding)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-03 12:00:00.000000

Additive (maintainer-approved 2026-11-03): ``instances.color`` (nullable string), one of
a fixed palette of named colours. Existing instances get colours in ``created_at`` order,
each taking the first palette colour not yet used, cycling once all are used.

The palette is copied here (not imported) so this migration keeps doing what it did when
the app's palette changes later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PALETTE = (
    "blue",
    "violet",
    "teal",
    "pink",
    "orange",
    "lime",
    "cyan",
    "indigo",
    "fuchsia",
    "sky",
    "purple",
)

_instances = sa.table(
    "instances",
    sa.column("id", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("color", sa.String(16)),
)


def upgrade() -> None:
    with op.batch_alter_table("instances") as batch:
        batch.add_column(sa.Column("color", sa.String(length=16), nullable=True))
    bind = op.get_bind()
    ids = (
        bind.execute(sa.select(_instances.c.id).order_by(_instances.c.created_at, _instances.c.id))
        .scalars()
        .all()
    )
    for index, instance_id in enumerate(ids):
        bind.execute(
            _instances.update()
            .where(_instances.c.id == instance_id)
            .values(color=_PALETTE[index % len(_PALETTE)])
        )


def downgrade() -> None:
    with op.batch_alter_table("instances") as batch:
        batch.drop_column("color")

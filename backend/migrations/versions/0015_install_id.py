"""settings: the install id

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-18 12:00:00.000000

Data migration (pending maintainer approval, lite install). Adds the ``settings`` row
``install_id`` = ``{"id": "<uuid4>"}`` when it is missing; an existing row is never
touched, so running it twice changes nothing. Downgrade deletes the row. Self-contained:
it does not import app code.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_settings = sa.table("settings", sa.column("key", sa.String()), sa.column("value", sa.JSON()))


def upgrade() -> None:
    conn = op.get_bind()
    found = conn.execute(sa.select(_settings.c.key).where(_settings.c.key == "install_id")).first()
    if found is None:
        conn.execute(_settings.insert().values(key="install_id", value={"id": str(uuid.uuid4())}))


def downgrade() -> None:
    op.get_bind().execute(_settings.delete().where(_settings.c.key == "install_id"))

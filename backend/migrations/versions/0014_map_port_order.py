"""network maps: a port group's ``odd_top`` flag becomes ``order``

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-08 18:00:00.000000

Maintainer-approved 2026-11-08. Data migration over ``network_maps.document``: in every box
panel group, ``odd_top: true`` becomes ``order: "odd_top"`` and ``odd_top: false`` becomes
``order: "rows"``. Groups without ``odd_top`` are untouched, so running it twice changes
nothing. Downgrade maps ``odd_top`` back to true and everything else (``rows``,
``odd_bottom``) to false. Self-contained: it does not import app code.
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_maps = sa.table("network_maps", sa.column("id", sa.Integer()), sa.column("document", sa.JSON()))


def _up(group: dict[str, Any]) -> bool:
    if "odd_top" not in group:
        return False
    odd_top = group.pop("odd_top")
    group.setdefault("order", "odd_top" if odd_top else "rows")
    return True


def _down(group: dict[str, Any]) -> bool:
    if "order" not in group:
        return False
    group["odd_top"] = group.pop("order") == "odd_top"
    return True


def _convert(forward: bool) -> None:
    bind = op.get_bind()
    change = _up if forward else _down
    for row in bind.execute(sa.select(_maps.c.id, _maps.c.document)).all():
        document = row.document
        if isinstance(document, str):
            document = json.loads(document)
        elements = document.get("elements") if isinstance(document, dict) else None
        if not isinstance(elements, list):
            continue
        changed = False
        for element in elements:
            if not (isinstance(element, dict) and element.get("kind") == "box"):
                continue
            panel = element.get("panel")
            if not isinstance(panel, dict):
                continue
            for group in panel.get("groups") or []:
                if isinstance(group, dict) and change(group):
                    changed = True
        if changed:
            bind.execute(_maps.update().where(_maps.c.id == row.id).values(document=document))


def upgrade() -> None:
    _convert(True)


def downgrade() -> None:
    _convert(False)

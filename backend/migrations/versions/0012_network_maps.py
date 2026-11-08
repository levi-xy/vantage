"""network_maps (user-drawn network maps); links replaced

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-07 12:00:00.000000

Maintainer-approved 2026-11-07 (M9.1). Creates ``network_maps``. Existing ``links`` rows
become one map named "Network": one box per linked instance in a grid (4 per row) and
one cable per link, the link's label and ports in the cable label. ``network-map`` cards
are removed from every saved dashboard layout. ``links`` is dropped.

Downgrade recreates an empty ``links`` table and drops ``network_maps``.
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_links = sa.table(
    "links",
    sa.column("id", sa.Integer()),
    sa.column("a_instance_id", sa.Uuid()),
    sa.column("b_instance_id", sa.Uuid()),
    sa.column("label", sa.String()),
    sa.column("a_port", sa.String()),
    sa.column("b_port", sa.String()),
)
_instances = sa.table("instances", sa.column("id", sa.Uuid()), sa.column("name", sa.String()))
_dashboards = sa.table("dashboards", sa.column("id", sa.Integer()), sa.column("layout", sa.JSON()))
_maps = sa.table(
    "network_maps",
    sa.column("name", sa.String()),
    sa.column("position", sa.Integer()),
    sa.column("document", sa.JSON()),
    sa.column("revision", sa.Integer()),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def _cable_label(label: str | None, a_port: str | None, b_port: str | None) -> str:
    text = label or ""
    if a_port or b_port:
        ports = f"{a_port or ''} ↔ {b_port or ''}"
        text = f"{text} · {ports}" if text else ports
    return text[:64]


def _links_to_map(bind: sa.Connection) -> None:
    links = bind.execute(sa.select(_links).order_by(_links.c.id)).all()
    if not links:
        return
    names = dict(bind.execute(sa.select(_instances.c.id, _instances.c.name)).all())
    box_of: dict[Any, str] = {}
    elements: list[dict[str, Any]] = []
    for link in links:
        for instance_id in (link.a_instance_id, link.b_instance_id):
            if instance_id in box_of:
                continue
            index = len(box_of)
            box_of[instance_id] = f"b{index + 1}"
            elements.append(
                {
                    "id": f"b{index + 1}",
                    "kind": "box",
                    "x": 40 + (index % 4) * 260,
                    "y": 40 + (index // 4) * 160,
                    "w": 200,
                    "h": 96,
                    "label": str(names.get(instance_id, ""))[:64],
                    "icon": "generic",
                    "instance_id": str(instance_id),
                }
            )
    for link in links:
        if link.a_instance_id == link.b_instance_id:
            continue  # a cable needs two different ends; the box is still placed
        elements.append(
            {
                "id": f"c{link.id}",
                "kind": "cable",
                "from": {"element": box_of[link.a_instance_id], "port": None},
                "to": {"element": box_of[link.b_instance_id], "port": None},
                "label": _cable_label(link.label, link.a_port, link.b_port),
            }
        )
    document = {"version": 1, "vlans": [], "elements": elements}
    bind.execute(
        _maps.insert().values(
            name="Network",
            position=0,
            document=document,
            revision=1,
            updated_at=sa.func.now(),
        )
    )


def _strip_map_cards(bind: sa.Connection) -> None:
    for row in bind.execute(sa.select(_dashboards.c.id, _dashboards.c.layout)).all():
        layout = row.layout
        if isinstance(layout, str):
            layout = json.loads(layout)
        cards = layout.get("cards") if isinstance(layout, dict) else None
        if not isinstance(cards, list):
            continue
        kept = [
            c for c in cards if not (isinstance(c, dict) and c.get("card_type") == "network-map")
        ]
        if len(kept) != len(cards):
            bind.execute(
                _dashboards.update()
                .where(_dashboards.c.id == row.id)
                .values(layout={**layout, "cards": kept})
            )


def upgrade() -> None:
    op.create_table(
        "network_maps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_network_maps")),
        sqlite_autoincrement=True,
    )
    bind = op.get_bind()
    _links_to_map(bind)
    _strip_map_cards(bind)
    op.drop_index(op.f("ix_links_b_instance_id"), table_name="links")
    op.drop_index(op.f("ix_links_a_instance_id"), table_name="links")
    op.drop_table("links")


def downgrade() -> None:
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
    op.drop_table("network_maps")

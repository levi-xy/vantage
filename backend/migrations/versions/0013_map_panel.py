"""network maps: a box's ``ports`` strip becomes a ``panel`` with port groups

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-08 12:00:00.000000

Maintainer-approved 2026-11-08 (M11.1). Data migration over ``network_maps.document``: a
box's old ``ports`` object ``{source, layout, ports: [{id, label, kind, vlans}]}`` becomes
``panel``: consecutive ports of the same kind form one group (two rows when the old layout
was ``panel``), every port keeps its ``id`` (cables still point at it), the label becomes
the caption, VLANs are kept. A null ``ports`` is dropped. Documents without ``ports`` are
untouched, so running it twice changes nothing. Self-contained: it does not import app code.

Downgrade turns panels back into ``ports`` (groups' kind, rows and captions only; the new
fields are dropped).
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_maps = sa.table("network_maps", sa.column("id", sa.Integer()), sa.column("document", sa.JSON()))


def _to_panel(old: dict[str, Any]) -> dict[str, Any]:
    rows = 2 if old.get("layout") == "panel" else 1
    device = old.get("source") == "device"
    groups: list[dict[str, Any]] = []
    ports: list[dict[str, Any]] = []
    for port in old.get("ports") or []:
        kind = "sfp" if port.get("kind") == "sfp" else "rj45"
        if not groups or groups[-1]["kind"] != kind:
            groups.append(
                {
                    "id": f"g{len(groups) + 1}",
                    "kind": kind,
                    "count": 0,
                    "rows": rows,
                    "odd_top": False,
                    "first": 1,
                    "prefix": "",
                }
            )
        group = groups[-1]
        group["count"] += 1
        ports.append(
            {
                "id": port["id"],
                "group": group["id"],
                "n": group["count"],
                "caption": port.get("label", ""),
                "interface": port["id"] if device else "",
                "vlans": port.get("vlans") or {"untagged": None, "tagged": []},
                "hidden": False,
            }
        )
    return {
        "look": "flat",
        "place": "bottom",
        "size": "m",
        "numbers": True,
        "captions": "typed",
        "source": "device" if device else "manual",
        "groups": groups,
        "ports": ports,
    }


def _to_ports(panel: dict[str, Any]) -> dict[str, Any]:
    groups = {g.get("id"): g for g in panel.get("groups") or []}
    rows = max((g.get("rows", 1) for g in groups.values()), default=1)
    ports = []
    for port in panel.get("ports") or []:
        kind = groups.get(port.get("group"), {}).get("kind", "rj45")
        ports.append(
            {
                "id": port["id"],
                "label": port.get("caption", ""),
                "kind": "rj45" if kind == "rj45" else "sfp",
                "vlans": port.get("vlans") or {"untagged": None, "tagged": []},
            }
        )
    return {
        "source": panel.get("source", "manual"),
        "layout": "panel" if rows == 2 else "row",
        "ports": ports[:64],
    }


def _convert(forward: bool) -> None:
    bind = op.get_bind()
    old_key, new_key = ("ports", "panel") if forward else ("panel", "ports")
    convert = _to_panel if forward else _to_ports
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
            if old_key not in element:
                continue
            value = element.pop(old_key)
            if isinstance(value, dict) and element.get(new_key) is None:
                element[new_key] = convert(value)
            changed = True
        if changed:
            bind.execute(_maps.update().where(_maps.c.id == row.id).values(document=document))


def upgrade() -> None:
    _convert(True)


def downgrade() -> None:
    _convert(False)

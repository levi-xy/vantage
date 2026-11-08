import json

from alembic import command
from sqlalchemy import text

from app.api.map_document import MapDocument
from app.core.db import make_engine
from tests.conftest import alembic_config


def test_0013_turns_a_ports_strip_into_a_panel_and_cables_stay_valid(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0012")
    eng = make_engine(db_url)
    old_ports = {
        "source": "manual",
        "layout": "panel",
        "ports": [
            {"id": "p1", "label": "uplink", "kind": "rj45", "vlans": {"untagged": "10"}},
            {"id": "p2", "label": "", "kind": "rj45"},
            {"id": "p3", "label": "wan", "kind": "sfp"},
        ],
    }
    document = {
        "version": 1,
        "vlans": [{"id": "10", "name": "LAN", "color": "blue"}],
        "elements": [
            {"id": "a", "kind": "box", "x": 0, "y": 0, "w": 100, "h": 50, "ports": old_ports},
            {"id": "b", "kind": "box", "x": 0, "y": 0, "w": 100, "h": 50, "ports": None},
            {
                "id": "c",
                "kind": "cable",
                "from": {"element": "a", "port": "p3"},
                "to": {"element": "b", "port": None},
            },
        ],
    }
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO network_maps (id, name, position, document, revision, "
                    "updated_at) VALUES (1, 'M', 0, :d, 1, '2026-11-08 00:00:00')"
                ),
                {"d": json.dumps(document)},
            )
        command.upgrade(cfg, "0013")
        with eng.connect() as conn:
            raw = conn.execute(text("SELECT document FROM network_maps")).scalar()
        doc = raw if isinstance(raw, dict) else json.loads(raw)
        box_a, box_b = doc["elements"][0], doc["elements"][1]
        assert "ports" not in box_a and "ports" not in box_b
        panel = box_a["panel"]
        assert [(g["kind"], g["count"], g["rows"]) for g in panel["groups"]] == [
            ("rj45", 2, 2),
            ("sfp", 1, 2),
        ]
        assert [(p["id"], p["caption"], p["group"], p["n"]) for p in panel["ports"]] == [
            ("p1", "uplink", "g1", 1),
            ("p2", "", "g1", 2),
            ("p3", "wan", "g2", 1),
        ]
        assert panel["ports"][0]["vlans"]["untagged"] == "10"
        assert "panel" not in box_b
        MapDocument.model_validate(doc)  # the cable to port p3 is still valid
    finally:
        eng.dispose()

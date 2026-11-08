import json

from alembic import command
from sqlalchemy import inspect, text

from app.api.map_document import MapDocument
from app.core.db import make_engine
from tests.conftest import alembic_config
from tests.test_migrations import STAMP


def test_0012_turns_links_into_a_map_and_strips_map_cards(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0011")
    eng = make_engine(db_url)
    ids = [f"{n:032x}" for n in (1, 2, 3)]  # how SQLite stores a Uuid
    keep = {"card_type": "ping-check"}
    layout = {"version": 2, "cards": [keep, {"card_type": "network-map"}]}
    try:
        with eng.begin() as conn:
            for n, iid in enumerate(ids):
                conn.execute(
                    text(
                        "INSERT INTO instances (id, type_id, name, tags, enabled, config, "
                        "created_at, updated_at) VALUES (:id, 'ping_check', :name, '[]', :en, "
                        "'{}', :at, :at)"
                    ),
                    {"id": iid, "name": f"dev-{n}", "en": True, "at": STAMP},
                )
            conn.execute(
                text(
                    "INSERT INTO links (id, a_instance_id, b_instance_id, label, a_port, b_port) "
                    "VALUES (1, :a, :b, 'uplink', 'ether1', 'igb0'), (2, :b, :c, NULL, NULL, NULL)"
                ),
                {"a": ids[0], "b": ids[1], "c": ids[2]},
            )
            conn.execute(
                text("INSERT INTO dashboards (id, name, layout) VALUES (1, 'D', :l)"),
                {"l": json.dumps(layout)},
            )
        command.upgrade(cfg, "0012")
        with eng.connect() as conn:
            assert "links" not in inspect(conn).get_table_names()
            name, doc = conn.execute(text("SELECT name, document FROM network_maps")).one()
            raw = conn.execute(text("SELECT layout FROM dashboards")).scalar()
        assert name == "Network"
        doc = doc if isinstance(doc, dict) else json.loads(doc)
        boxes = [e for e in doc["elements"] if e["kind"] == "box"]
        cables = [e for e in doc["elements"] if e["kind"] == "cable"]
        assert [(b["label"], b["x"], b["y"]) for b in boxes] == [
            ("dev-0", 40, 40),
            ("dev-1", 300, 40),
            ("dev-2", 560, 40),
        ]
        assert [c["label"] for c in cables] == ["uplink · ether1 ↔ igb0", ""]
        MapDocument.model_validate(doc)  # the migrated document passes the API's schema
        layout_after = raw if isinstance(raw, dict) else json.loads(raw)
        assert layout_after == {"version": 2, "cards": [keep]}
    finally:
        eng.dispose()

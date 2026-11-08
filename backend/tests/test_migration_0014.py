import json

from alembic import command
from sqlalchemy import text

from app.api.map_document import MapDocument
from app.core.db import make_engine
from tests.conftest import alembic_config


def _group(gid: str, **extra: object) -> dict[str, object]:
    return {"id": gid, "kind": "rj45", "count": 2, "rows": 2, "first": 1, "prefix": "", **extra}


def test_0014_turns_odd_top_into_order_and_back(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0013")
    eng = make_engine(db_url)
    panel = {"groups": [_group("g1", odd_top=True), _group("g2", odd_top=False)], "ports": []}
    document = {
        "version": 1,
        "vlans": [],
        "elements": [{"id": "a", "kind": "box", "x": 0, "y": 0, "w": 100, "h": 50, "panel": panel}],
    }

    def groups() -> list[dict[str, object]]:
        with eng.connect() as conn:
            raw = conn.execute(text("SELECT document FROM network_maps")).scalar()
        doc = raw if isinstance(raw, dict) else json.loads(raw)
        MapDocument.model_validate(doc)
        return doc["elements"][0]["panel"]["groups"]

    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO network_maps (id, name, position, document, revision, "
                    "updated_at) VALUES (1, 'M', 0, :d, 1, '2026-11-08 00:00:00')"
                ),
                {"d": json.dumps(document)},
            )
        command.upgrade(cfg, "0014")
        assert [(g.get("order"), "odd_top" in g) for g in groups()] == [
            ("odd_top", False),
            ("rows", False),
        ]
        command.downgrade(cfg, "0013")
        assert [g.get("odd_top") for g in groups()] == [True, False]
    finally:
        eng.dispose()


def test_a_group_with_the_old_odd_top_key_still_parses() -> None:
    group = MapDocument.model_validate(
        {
            "version": 1,
            "elements": [
                {
                    "id": "a",
                    "kind": "box",
                    "x": 0,
                    "y": 0,
                    "w": 100,
                    "h": 50,
                    "panel": {"groups": [_group("g1", odd_top=True)]},
                }
            ],
        }
    ).elements[0]
    assert group.panel.groups[0].order == "odd_top"  # type: ignore[union-attr]

"""Network maps API (M9.1, approved 2026-11-07)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.deps import CSRF_HEADER
from app.core.db import make_session_factory
from app.models import AuditLog, Role
from tests.test_auth import add_user, do_setup, login, make_client

API = "/api/v1"


@pytest.fixture
def admin(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        yield c


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


def box(id: str, **extra: object) -> dict[str, object]:
    return {"id": id, "kind": "box", "x": 0, "y": 0, "w": 100, "h": 50, **extra}


def cable(id: str, a: str, b: str, **extra: object) -> dict[str, object]:
    return {"id": id, "kind": "cable", "from": {"element": a}, "to": {"element": b}, **extra}


def text_el(id: str) -> dict[str, object]:
    return {"id": id, "kind": "text", "x": 0, "y": 0, "w": 9, "h": 9, "text": "x" * 4000}


def doc(*elements: dict[str, object]) -> dict[str, object]:
    return {"version": 1, "vlans": [], "elements": list(elements)}


def test_crud_revision_and_audit(admin: TestClient, db: Session) -> None:
    assert admin.get(f"{API}/maps").json() == []
    first = admin.post(f"{API}/maps", json={"name": " Home "}).json()
    second = admin.post(f"{API}/maps", json={"name": "Lab"}).json()
    assert (first["name"], first["revision"], second["position"]) == ("Home", 1, 1)
    assert first["document"] == {"version": 1, "vlans": [], "elements": []}

    body = {"document": doc(box("a"), box("b"), cable("c", "a", "b")), "revision": 1}
    saved = admin.put(f"{API}/maps/{first['id']}", json=body)
    assert saved.status_code == 200 and saved.json()["revision"] == 2
    again = admin.get(f"{API}/maps/{first['id']}").json()
    assert again["document"]["elements"][2]["from"] == {"element": "a", "port": None}
    # A stale revision changes nothing.
    assert admin.put(f"{API}/maps/{first['id']}", json=body).status_code == 409

    moved = admin.patch(f"{API}/maps/{second['id']}", json={"name": "Lab 2", "position": 0})
    assert moved.json() == {"id": second["id"], "name": "Lab 2", "position": 0}
    assert [m["id"] for m in admin.get(f"{API}/maps").json()] == [second["id"], first["id"]]
    for m in admin.get(f"{API}/maps").json():  # the last map may be deleted too
        assert admin.delete(f"{API}/maps/{m['id']}").status_code == 204
    assert admin.get(f"{API}/maps").json() == []
    assert admin.get(f"{API}/maps/{first['id']}").status_code == 404

    rows = db.scalars(select(AuditLog).where(AuditLog.target_type == "network_map")).all()
    assert sorted(r.action for r in rows) == ["create"] * 2 + ["delete"] * 2 + ["update"]
    assert all("document" not in r.changes for r in rows)  # drawing saves aren't audited


def test_viewer_reads_but_cannot_write(admin: TestClient, db: Session) -> None:
    created = admin.post(f"{API}/maps", json={"name": "Home"}).json()
    add_user(db, "viewer1", Role.VIEWER)
    v = TestClient(admin.app, base_url="https://testserver")
    v.headers[CSRF_HEADER] = login(v, "viewer1")["csrf_token"]
    url = f"{API}/maps/{created['id']}"
    assert v.get(f"{API}/maps").status_code == 200
    assert v.get(url).status_code == 200
    assert v.post(f"{API}/maps", json={"name": "X"}).status_code == 403
    assert v.put(url, json={"document": doc(), "revision": 1}).status_code == 403
    assert v.patch(url, json={"name": "X"}).status_code == 403
    assert v.delete(url).status_code == 403


MANUAL_PORTS = {"source": "manual", "ports": [{"id": "p1"}]}


@pytest.mark.parametrize(
    "document",
    [
        doc(box("a"), cable("c", "a", "missing")),
        doc(box("a"), cable("c", "a", "a")),
        doc(box("a"), box("a")),
        doc(
            box("a", ports=MANUAL_PORTS),
            box("b"),
            cable("c", "a", "b", **{"from": {"element": "a", "port": "p9"}}),
        ),
        doc(box("a"), box("b"), cable("c", "a", "b", vlan="nope")),
        doc(box("a", surprise=1)),
        doc(box("a", icon="di:Not A Slug")),
        doc(box("a", shape="triangle")),
        doc(box("a", sections=[{"id": f"s{i}"} for i in range(5)])),
        doc(box("a", sections=[{"id": "s", "items": [{"id": f"i{i}"} for i in range(13)]}])),
        doc(box("a", panel={"groups": [{"id": f"g{i}"} for i in range(9)]})),
        doc(box("a", panel={"ports": [{"id": f"p{i}"} for i in range(129)]})),
        doc(box("a", panel={"ports": [{"id": "p1", "group": "nope"}]})),
        doc(box("a", nas={"bays": 17})),
        doc({"id": "t", "kind": "table", "x": 0, "y": 0, "w": 9, "h": 9, "badges": [8]}),
        doc(*[box(f"b{i}") for i in range(501)]),  # element count
        doc(*[text_el(f"t{i}") for i in range(70)]),  # over 256 KB
    ],
)
def test_bad_documents_are_refused(admin: TestClient, document: dict[str, object]) -> None:
    created = admin.post(f"{API}/maps", json={"name": "Home"}).json()
    r = admin.put(f"{API}/maps/{created['id']}", json={"document": document, "revision": 1})
    assert r.status_code == 422
    assert admin.get(f"{API}/maps/{created['id']}").json()["revision"] == 1


def test_new_fields_are_stored_and_old_ports_are_read_as_a_panel(admin: TestClient) -> None:
    created = admin.post(f"{API}/maps", json={"name": "Home"}).json()
    rich = box(
        "a",
        shape="nas",
        role="router",
        icon="di:example-icon",
        model="m",
        sections=[{"id": "s", "kind": "list", "items": [{"id": "i", "figure": "f"}]}],
        panel={
            "groups": [{"id": "g1", "kind": "sfp_plus"}],
            "ports": [{"id": "g1-1", "group": "g1"}],
        },
        nas={"bays": 4},
    )
    legacy = box("b", ports=MANUAL_PORTS)
    networks = {"id": "n", "kind": "networks", "x": 0, "y": 0, "w": 9, "h": 9}
    document = doc(rich, legacy, networks)
    document["vlans"] = [{"id": "10", "name": "LAN", "color": "blue", "subnet": "192.0.2.0/24"}]
    r = admin.put(f"{API}/maps/{created['id']}", json={"document": document, "revision": 1})
    assert r.status_code == 200, r.text
    stored = r.json()["document"]["elements"]
    assert stored[0]["shape"] == "nas" and stored[0]["panel"]["groups"][0]["kind"] == "sfp_plus"
    assert "ports" not in stored[1] and stored[1]["panel"]["ports"][0]["id"] == "p1"

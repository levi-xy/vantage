"""Links API (M6.1)."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.deps import CSRF_HEADER
from app.core.db import make_session_factory
from app.models import AuditLog, Instance, Link, Role
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


def add_instance(db: Session, name: str) -> str:
    row = Instance(type_id="ping_check", name=name, tags=[], enabled=True, config={}, secrets=b"")
    db.add(row)
    db.commit()
    return str(row.id)


def test_crud_and_audit(admin: TestClient, db: Session) -> None:
    a, b = add_instance(db, "sw1"), add_instance(db, "fw1")
    body = {"a_instance_id": a, "b_instance_id": b, "label": " uplink ", "a_port": "ether1"}
    r = admin.post(f"{API}/links", json=body)
    assert r.status_code == 201
    link = r.json()
    assert link["label"] == "uplink" and link["b_port"] is None
    assert admin.get(f"{API}/links").json() == [link]
    r = admin.patch(f"{API}/links/{link['id']}", json={"b_port": "igb0"})
    assert r.json()["b_port"] == "igb0"
    assert admin.delete(f"{API}/links/{link['id']}").status_code == 204
    assert admin.get(f"{API}/links").json() == []
    assert admin.delete(f"{API}/links/{link['id']}").status_code == 404
    entries = db.scalars(select(AuditLog).where(AuditLog.target_type == "link")).all()
    assert sorted(e.action for e in entries) == ["create", "delete", "update"]
    assert all(e.actor_type == "user" and e.user_id is not None for e in entries)


def test_validation(admin: TestClient, db: Session) -> None:
    a = add_instance(db, "sw1")
    ghost = str(uuid.uuid4())
    bad = [
        {"a_instance_id": a, "b_instance_id": a},  # same device
        {"a_instance_id": a, "b_instance_id": ghost},  # unknown device
        {"a_instance_id": a, "b_instance_id": add_instance(db, "x"), "label": "a\x00b"},
        {"a_instance_id": a, "b_instance_id": add_instance(db, "y"), "a_port": "p" * 65},
    ]
    for body in bad:
        assert admin.post(f"{API}/links", json=body).status_code == 422, body
    assert db.scalars(select(Link)).all() == []


def test_deleting_an_instance_removes_its_links(admin: TestClient, db: Session) -> None:
    a, b = add_instance(db, "sw1"), add_instance(db, "fw1")
    r = admin.post(f"{API}/links", json={"a_instance_id": a, "b_instance_id": b})
    assert r.status_code == 201
    assert admin.delete(f"{API}/instances/{a}").status_code == 204
    assert admin.get(f"{API}/links").json() == []


def test_viewer_reads_but_cannot_write(admin: TestClient, db: Session) -> None:
    a, b = add_instance(db, "sw1"), add_instance(db, "fw1")
    body = {"a_instance_id": a, "b_instance_id": b}
    link = admin.post(f"{API}/links", json=body).json()
    add_user(db, "viewer1", Role.VIEWER)
    v = TestClient(admin.app, base_url="https://testserver")
    v.headers[CSRF_HEADER] = login(v, "viewer1")["csrf_token"]
    assert v.get(f"{API}/links").status_code == 200
    assert v.post(f"{API}/links", json=body).status_code == 403
    assert v.patch(f"{API}/links/{link['id']}", json={"label": "x"}).status_code == 403
    assert v.delete(f"{API}/links/{link['id']}").status_code == 403
    # the admin session without its CSRF token is refused
    del admin.headers[CSRF_HEADER]
    assert admin.post(f"{API}/links", json=body).status_code == 403

"""Dashboards API (M5.2)."""

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
CARD = {
    "instance_id": "00000000-0000-0000-0000-000000000001",
    "card_type": "ping-check",
    "x": 0,
    "y": 0,
    "w": 4,
    "h": 3,
    "settings": {},
}


@pytest.fixture
def admin(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        yield c


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


def viewer_client(admin: TestClient, db: Session) -> TestClient:
    """Another browser on the same app, logged in as a viewer."""
    add_user(db, "viewer1", Role.VIEWER)
    c = TestClient(admin.app, base_url="https://testserver")
    c.headers[CSRF_HEADER] = login(c, "viewer1")["csrf_token"]
    return c


def test_default_dashboard_is_created_on_first_get(admin: TestClient) -> None:
    r = admin.get(f"{API}/dashboards/1")
    assert r.status_code == 200
    assert r.json() == {"id": 1, "name": "Default", "layout": {"cards": []}}
    assert admin.get(f"{API}/dashboards/2").status_code == 404


def test_admin_saves_layout_and_viewer_reads_it(admin: TestClient, db: Session) -> None:
    body = {"layout": {"cards": [CARD]}}
    assert admin.put(f"{API}/dashboards/1", json=body).status_code == 404  # no GET yet
    admin.get(f"{API}/dashboards/1")
    r = admin.put(f"{API}/dashboards/1", json=body)
    assert r.status_code == 200
    assert r.json()["layout"]["cards"] == [CARD]

    viewer = viewer_client(admin, db)
    assert viewer.get(f"{API}/dashboards/1").json()["layout"]["cards"] == [CARD]

    entry = db.scalars(select(AuditLog).where(AuditLog.target_type == "dashboard")).one()
    assert entry.changes == {"cards": 1}
    assert entry.actor_type.value == "user"


def test_viewer_cannot_save_and_csrf_is_required(admin: TestClient, db: Session) -> None:
    admin.get(f"{API}/dashboards/1")
    body = {"layout": {"cards": [CARD]}}
    assert admin.put(f"{API}/dashboards/1", json=body, headers={CSRF_HEADER: ""}).status_code == 403
    viewer = viewer_client(admin, db)
    assert viewer.put(f"{API}/dashboards/1", json=body).status_code == 403
    assert admin.get(f"{API}/dashboards/1").json()["layout"]["cards"] == []


def test_needs_a_session(admin: TestClient) -> None:
    anonymous = TestClient(admin.app, base_url="https://testserver")
    assert anonymous.get(f"{API}/dashboards/1").status_code == 401


def test_invalid_layouts_are_refused(admin: TestClient) -> None:
    admin.get(f"{API}/dashboards/1")
    bad = [
        {"cards": [{**CARD, "w": 0}]},
        {"cards": [{**CARD, "x": 12}]},
        {"cards": [{**CARD, "extra": 1}]},
        {"cards": [{**CARD, "settings": {"k": {"nested": 1}}}]},
        {"cards": [CARD] * 501},
        {"unknown": []},
    ]
    for layout in bad:
        assert admin.put(f"{API}/dashboards/1", json={"layout": layout}).status_code == 422

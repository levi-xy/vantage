"""Dashboards API (M5.2; several dashboards, 2026-11-02)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.dashboards import MAX_DASHBOARDS
from app.api.deps import CSRF_HEADER
from app.core.db import make_session_factory
from app.models import AuditLog, Dashboard, Role, User
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
    assert r.json() == {"id": 1, "name": "Default", "layout": {"version": 1, "cards": []}}
    assert admin.get(f"{API}/dashboards/2").status_code == 404


def test_any_user_saves_layout_and_csrf_is_required(admin: TestClient, db: Session) -> None:
    body = {"layout": {"cards": [CARD]}}
    assert admin.put(f"{API}/dashboards/1", json=body).status_code == 404  # no GET yet
    admin.get(f"{API}/dashboards/1")
    viewer = viewer_client(admin, db)
    no_csrf = {CSRF_HEADER: ""}
    assert viewer.put(f"{API}/dashboards/1", json=body, headers=no_csrf).status_code == 403
    assert viewer.put(f"{API}/dashboards/1", json=body).status_code == 200
    assert admin.get(f"{API}/dashboards/1").json()["layout"]["cards"] == [CARD]

    # Layout saves are not audited (too noisy).
    assert db.scalars(select(AuditLog).where(AuditLog.target_type == "dashboard")).all() == []


def test_dashboard_wide_cards_have_no_instance(admin: TestClient) -> None:
    admin.get(f"{API}/dashboards/1")
    widget = {**CARD, "instance_id": None, "card_type": "devices-health"}
    r = admin.put(f"{API}/dashboards/1", json={"layout": {"version": 2, "cards": [widget, CARD]}})
    assert r.status_code == 200
    assert r.json()["layout"] == {"version": 2, "cards": [widget, CARD]}


def summaries(c: TestClient) -> list[tuple[int, str, int]]:
    return [(d["id"], d["name"], d["position"]) for d in c.get(f"{API}/dashboards").json()]


def dashboard_audit(db: Session) -> list[tuple[str, int | None, object]]:
    db.expire_all()
    rows = db.scalars(
        select(AuditLog).where(AuditLog.target_type == "dashboard").order_by(AuditLog.id)
    )
    return [(r.action, r.user_id, r.changes) for r in rows]


def test_viewer_creates_copies_moves_renames_and_deletes(admin: TestClient, db: Session) -> None:
    assert summaries(admin) == [(1, "Default", 0)]  # the list creates "Default" too
    admin.put(f"{API}/dashboards/1", json={"layout": {"version": 2, "cards": [CARD]}})
    viewer = viewer_client(admin, db)
    viewer_id = db.scalars(select(User.id).where(User.username == "viewer1")).one()

    r = viewer.post(f"{API}/dashboards", json={"name": "  Rack  "})
    assert r.status_code == 201
    assert r.json() == {"id": 2, "name": "Rack", "layout": {"version": 1, "cards": []}}
    r = viewer.post(f"{API}/dashboards", json={"name": "Rack", "copy_from": 1})
    assert r.status_code == 201  # duplicate names are fine
    assert r.json()["layout"] == {"version": 2, "cards": [CARD]}
    assert summaries(admin) == [(1, "Default", 0), (2, "Rack", 1), (3, "Rack", 2)]

    assert viewer.patch(f"{API}/dashboards/3", json={"position": 0}).status_code == 200
    assert summaries(admin) == [(3, "Rack", 0), (1, "Default", 1), (2, "Rack", 2)]
    r = viewer.patch(f"{API}/dashboards/2", json={"name": "Lab", "position": 99})
    assert r.json() == {"id": 2, "name": "Lab", "position": 2}

    # Deleting the main one makes the next one main; positions stay dense.
    assert viewer.delete(f"{API}/dashboards/3").status_code == 204
    assert summaries(admin) == [(1, "Default", 0), (2, "Lab", 1)]
    assert viewer.delete(f"{API}/dashboards/3").status_code == 404
    # Dashboard 1 isn't recreated once other dashboards exist.
    assert viewer.delete(f"{API}/dashboards/1").status_code == 204
    assert admin.get(f"{API}/dashboards/1").status_code == 404
    assert viewer.delete(f"{API}/dashboards/2").status_code == 409  # the last one stays
    assert summaries(admin) == [(2, "Lab", 0)]
    # Deleted ids are never reused: a tab still open on /d/3 can't reach a new dashboard.
    assert viewer.post(f"{API}/dashboards", json={"name": "New"}).json()["id"] == 4
    viewer.delete(f"{API}/dashboards/4")

    assert dashboard_audit(db) == [
        ("create", viewer_id, {"name": "Rack"}),
        ("create", viewer_id, {"name": "Rack", "copy_from": 1}),
        ("update", viewer_id, {"position": {"old": 2, "new": 0}}),
        ("update", viewer_id, {"name": {"old": "Rack", "new": "Lab"}}),
        ("delete", viewer_id, {"name": "Rack"}),
        ("delete", viewer_id, {"name": "Default"}),
        ("create", viewer_id, {"name": "New"}),
        ("delete", viewer_id, {"name": "New"}),
    ]


def test_state_changes_need_csrf_and_valid_input(admin: TestClient) -> None:
    admin.get(f"{API}/dashboards")
    no_csrf = {CSRF_HEADER: ""}
    one = f"{API}/dashboards/1"
    assert admin.post(f"{API}/dashboards", json={"name": "A"}, headers=no_csrf).status_code == 403
    assert admin.patch(one, json={"name": "A"}, headers=no_csrf).status_code == 403
    assert admin.delete(one, headers=no_csrf).status_code == 403
    for body in ({"name": "   "}, {"name": "x" * 65}, {"name": "A", "copy_from": 9}):
        assert admin.post(f"{API}/dashboards", json=body).status_code == 422
    assert admin.patch(one, json={"position": -1}).status_code == 422
    assert admin.patch(f"{API}/dashboards/9", json={"name": "A"}).status_code == 404
    assert summaries(admin) == [(1, "Default", 0)]


def test_at_most_50_dashboards(admin: TestClient, db: Session) -> None:
    db.add_all(Dashboard(name=f"D{i}", layout={}, position=i) for i in range(MAX_DASHBOARDS))
    db.commit()
    assert admin.post(f"{API}/dashboards", json={"name": "One more"}).status_code == 409


def test_needs_a_session(admin: TestClient) -> None:
    anonymous = TestClient(admin.app, base_url="https://testserver")
    assert anonymous.get(f"{API}/dashboards/1").status_code == 401


def test_invalid_layouts_are_refused(admin: TestClient) -> None:
    admin.get(f"{API}/dashboards/1")
    bad = [
        {"cards": [{**CARD, "w": 0}]},
        {"cards": [{**CARD, "instance_id": ""}]},
        {"version": 0, "cards": []},
        {"cards": [{**CARD, "x": 12}]},
        {"cards": [{**CARD, "extra": 1}]},
        {"cards": [{**CARD, "settings": {"k": {"nested": 1}}}]},
        {"cards": [CARD] * 501},
        {"unknown": []},
    ]
    for layout in bad:
        assert admin.put(f"{API}/dashboards/1", json={"layout": layout}).status_code == 422

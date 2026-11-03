"""``GET /instances/{id}/now-playing``: Emby's live list, any logged-in user, never stored."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.deps import CSRF_HEADER
from app.integrations.builtin.emby.integration import NOW_PLAYING, PlayingRow
from app.models import Instance, Role
from tests.test_auth import add_user, do_setup, login, make_client

API = "/api/v1"


@pytest.fixture
def admin(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        yield c


def add_instance(session: Session, type_id: str, name: str, *, enabled: bool = True) -> uuid.UUID:
    row = Instance(id=uuid.uuid4(), type_id=type_id, name=name, enabled=enabled, config={})
    session.add(row)
    session.commit()
    return row.id


def test_any_user_reads_the_latest_list(admin: TestClient, session: Session) -> None:
    media = add_instance(session, "emby", "media")
    url = f"{API}/instances/{media}/now-playing"
    assert admin.get(url).json() == {"checked_at": None, "sessions": []}  # not polled yet

    NOW_PLAYING.remember(media, (PlayingRow(user="example-alice", title="Example Movie"),))
    try:
        add_user(session, "viewer", Role.VIEWER)
        viewer = TestClient(admin.app, base_url="https://testserver")
        login(viewer, "viewer")
        r = viewer.get(url)
        assert r.status_code == 200
        assert r.json()["sessions"] == [{"user": "example-alice", "title": "Example Movie"}]
        assert r.json()["checked_at"] is not None
        assert TestClient(admin.app, base_url="https://testserver").get(url).status_code == 401
    finally:
        NOW_PLAYING.forget(media)


def test_other_types_unknown_ids_and_disabled(admin: TestClient, session: Session) -> None:
    ping = add_instance(session, "ping_check", "gw")
    assert admin.get(f"{API}/instances/{ping}/now-playing").status_code == 404
    assert admin.get(f"{API}/instances/{uuid.uuid4()}/now-playing").status_code == 404
    off = add_instance(session, "emby", "off", enabled=False)
    NOW_PLAYING.remember(off, (PlayingRow(user="u", title="t"),))
    try:
        assert admin.get(f"{API}/instances/{off}/now-playing").json()["sessions"] == []
    finally:
        NOW_PLAYING.forget(off)


def test_deleting_the_device_forgets_its_list(admin: TestClient, session: Session) -> None:
    media = add_instance(session, "emby", "media")
    NOW_PLAYING.remember(media, (PlayingRow(user="u", title="t"),))
    assert admin.delete(f"{API}/instances/{media}").status_code == 204
    assert NOW_PLAYING.latest(media) is None

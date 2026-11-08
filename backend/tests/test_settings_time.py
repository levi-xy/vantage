"""Timezone setting (maintainer-approved 2026-11-07)."""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.alerts.selfcheck import hhmm
from app.api.deps import CSRF_HEADER
from app.core import timezone
from app.core.db import make_session_factory
from app.main import _ZoneFormatter
from app.models import AuditLog, Role
from tests.test_auth import add_user, do_setup, login, make_client

API = "/api/v1/settings/time"


@pytest.fixture(autouse=True)
def reset_zone() -> Iterator[None]:
    timezone.set_zone("UTC")
    yield
    timezone.set_zone("UTC")


@pytest.fixture
def admin(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        yield c


def test_default_is_utc_and_lists_zones(admin: TestClient) -> None:
    body = admin.get(API).json()
    assert body["timezone"] == "UTC"
    assert body["clock_source"] == "host"
    assert "Europe/Berlin" in body["timezones"]
    assert datetime.fromisoformat(body["server_time"]).tzinfo is not None


def test_put_is_admin_only_validated_and_audited(admin: TestClient, engine: Engine) -> None:
    assert admin.put(API, json={"timezone": "Mars/Base"}).status_code == 422
    r = admin.put(API, json={"timezone": "Europe/Berlin"})
    assert r.status_code == 200 and r.json()["timezone"] == "Europe/Berlin"
    assert admin.get(API).json()["timezone"] == "Europe/Berlin"

    with make_session_factory(engine)() as db:
        (entry,) = db.scalars(select(AuditLog).where(AuditLog.target_type == "settings")).all()
        assert (entry.action, entry.target_id) == ("update", "timezone")
        assert entry.changes == {"from": "UTC", "to": "Europe/Berlin"}
        add_user(db, "viewer1", Role.VIEWER)
    viewer = TestClient(admin.app, base_url="https://testserver")
    viewer.headers[CSRF_HEADER] = login(viewer, "viewer1")["csrf_token"]
    assert viewer.get(API).json()["timezone"] == "Europe/Berlin"
    assert viewer.put(API, json={"timezone": "UTC"}).status_code == 403


def test_logs_and_telegram_text_use_the_zone() -> None:
    timezone.set_zone("Europe/Berlin")
    at = datetime(2026, 11, 7, 13, 5, 3, tzinfo=UTC)
    assert hhmm(at) == "14:05 CET"
    record = logging.LogRecord("app.x", logging.INFO, "", 0, "hello", None, None)
    record.created = at.timestamp()
    line = _ZoneFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s").format(record)
    assert line == "2026-11-07 14:05:03+01:00 INFO app.x: hello"

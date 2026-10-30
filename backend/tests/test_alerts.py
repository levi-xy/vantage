"""Alert rules API, evaluator and templates (M7.1, M7.3)."""

import logging
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.alerts.evaluator import AlertEvaluator, clean
from app.alerts.templates import TEMPLATES
from app.api.deps import CSRF_HEADER
from app.collectors.hub import EventHub, Subscriber
from app.core.db import make_session_factory, utcnow
from app.models import AlertEvent, AlertRule, AlertState, AuditLog, Instance, Role
from app.query.proxy import PrometheusClient
from tests.test_auth import ADMIN, add_user, do_setup, login, make_client

API = "/api/v1"
INSTANCE_ID = "00000000-0000-0000-0000-000000000001"
OTHER_ID = "00000000-0000-0000-0000-000000000002"
INSTANCE_UUID = uuid.UUID(INSTANCE_ID)


class FakeProm:
    """Answers every instant query with ``vector`` (a list of label dicts) or an error."""

    def __init__(self) -> None:
        self.vector: list[dict[str, str]] = []
        self.error: dict[str, str] | None = None

    def client(self) -> PrometheusClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if self.error:
                return httpx.Response(200, json={"status": "error", **self.error})
            result = [{"metric": m, "value": [1.0, "1"]} for m in self.vector]
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
            )

        return PrometheusClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://prom.test")
        )


@pytest.fixture
def prom() -> FakeProm:
    return FakeProm()


@pytest.fixture
def admin(engine: Engine, db_url: str, prom: FakeProm) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        c.app.state.prometheus = prom.client()  # type: ignore[attr-defined]
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        yield c


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


RULE = {"name": "Disk full", "expr": "up == 0", "for_seconds": 60, "severity": "critical"}


def test_rule_crud_is_audited(admin: TestClient, db: Session) -> None:
    r = admin.post(f"{API}/alerts/rules", json=RULE)
    assert r.status_code == 201
    rule_id = r.json()["id"]
    assert r.json()["enabled"] is True
    r = admin.patch(f"{API}/alerts/rules/{rule_id}", json={"for_seconds": 120, "enabled": False})
    assert r.json()["for_seconds"] == 120 and r.json()["enabled"] is False
    assert [x["id"] for x in admin.get(f"{API}/alerts/rules").json()] == [rule_id]
    assert admin.delete(f"{API}/alerts/rules/{rule_id}").status_code == 204
    assert admin.get(f"{API}/alerts/rules/{rule_id}").status_code == 404
    entries = db.scalars(select(AuditLog).where(AuditLog.target_type == "alert_rule")).all()
    assert sorted(e.action for e in entries) == ["create", "delete", "update"]
    assert {e.actor_type.value for e in entries} == {"user"}


def test_rule_validation(admin: TestClient, prom: FakeProm) -> None:
    prom.error = {"errorType": "bad_data", "error": "parse error at char 3"}
    r = admin.post(f"{API}/alerts/rules", json=RULE)
    assert r.status_code == 422 and "parse error" not in r.text
    prom.error = None
    prom.vector = [{"n": str(i)} for i in range(3)]
    bad_instance = {**RULE, "instance_id": INSTANCE_ID}
    assert admin.post(f"{API}/alerts/rules", json=bad_instance).status_code == 422
    assert admin.post(f"{API}/alerts/rules", json={**RULE, "channel_ids": [99]}).status_code == 422
    assert admin.post(f"{API}/alerts/rules", json={**RULE, "expr": "x" * 2001}).status_code == 422
    assert admin.post(f"{API}/alerts/rules", json={**RULE, "severity": "meh"}).status_code == 422


def test_over_budget_result_is_refused(admin: TestClient, prom: FakeProm) -> None:
    prom.vector = [{"n": str(i)} for i in range(501)]
    assert admin.post(f"{API}/alerts/rules", json=RULE).status_code == 422


def test_permissions(admin: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    v = TestClient(admin.app, base_url="https://testserver")
    v.headers[CSRF_HEADER] = login(v, "viewer1")["csrf_token"]
    assert v.post(f"{API}/alerts/rules", json=RULE).status_code == 403
    assert v.get(f"{API}/alerts/rules").status_code == 403
    assert v.get(f"{API}/alerts/templates").status_code == 403
    assert v.get(f"{API}/alerts/events").status_code == 200
    no_csrf = TestClient(admin.app, base_url="https://testserver")
    login(no_csrf, ADMIN)
    assert no_csrf.post(f"{API}/alerts/rules", json=RULE).status_code == 403


def test_events_newest_first_with_paging(admin: TestClient, db: Session) -> None:
    rule_id = admin.post(f"{API}/alerts/rules", json=RULE).json()["id"]
    for i in range(3):
        state = AlertState.FIRING if i == 2 else AlertState.RESOLVED
        db.add(AlertEvent(rule_id=rule_id, state=state, started_at=utcnow(), summary=f"s{i}"))
    db.commit()
    page = admin.get(f"{API}/alerts/events?limit=2").json()
    assert [e["summary"] for e in page] == ["s2", "s1"]
    rest = admin.get(f"{API}/alerts/events?limit=2&before_id={page[-1]['id']}").json()
    assert [e["summary"] for e in rest] == ["s0"]
    assert [e["summary"] for e in admin.get(f"{API}/alerts/events?state=firing").json()] == ["s2"]


def test_templates_are_sound(admin: TestClient) -> None:
    got = admin.get(f"{API}/alerts/templates").json()
    assert len(got) == len(TEMPLATES) > 10
    assert len({t["id"] for t in got}) == len(got)
    assert any("ping_percent_packet_loss" in t["expr"] and ">= 100" in t["expr"] for t in got)
    assert all(t["integration_types"] for t in got)


# --- evaluator ---------------------------------------------------------------------------


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[list[int], str]] = []
        self.fail = fail

    async def send(self, channel_ids: list[int], text: str) -> None:
        if self.fail:
            raise RuntimeError("example-token leaked in error")
        self.sent.append((channel_ids, text))


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self) -> datetime:
        return self.now


def make_eval(
    engine: Engine, prom: FakeProm, notifier: Recorder, clock: Clock
) -> tuple[AlertEvaluator, Subscriber]:
    hub = EventHub()
    sub = hub.subscribe()
    assert sub is not None
    ev = AlertEvaluator(
        session_factory=make_session_factory(engine),
        prometheus=prom.client,
        hub=hub,
        notifier=lambda: notifier,
        clock=clock,
    )
    return ev, sub


def add_rule(db: Session, **kw: object) -> AlertRule:
    fields: dict[str, object] = {
        "name": "Gateway",
        "expr": "up == 0",
        "for_seconds": 60,
        "severity": "critical",
        "channel_ids": [7],
        **kw,
    }
    rule = AlertRule(**fields)
    db.add(rule)
    db.commit()
    return rule


async def test_fires_after_for_then_resolves(engine: Engine, db: Session, prom: FakeProm) -> None:
    add_rule(db)
    rec, clock = Recorder(), Clock()
    ev, sub = make_eval(engine, prom, rec, clock)
    prom.vector = [{"vantage_instance": "fw\nINJECT", "vantage_instance_id": INSTANCE_ID}]
    await ev.tick()
    assert rec.sent == []  # pending, not yet 60 s
    clock.now += timedelta(seconds=61)
    await ev.tick()
    await ev.tick()  # still one event
    assert len(rec.sent) == 1 and rec.sent[0][0] == [7]
    assert rec.sent[0][1] == "FIRING: Gateway (critical): fw INJECT"
    msg = sub.queue.get_nowait()
    assert msg["type"] == "alert_event" and msg["state"] == "firing"
    assert INSTANCE_ID not in str(msg)
    prom.vector = []
    clock.now += timedelta(seconds=30)
    await ev.tick()
    db.expire_all()
    event = db.scalars(select(AlertEvent)).one()
    assert event.state == AlertState.RESOLVED and event.ended_at is not None
    assert rec.sent[1][1].startswith("RESOLVED: ")


async def test_blip_shorter_than_for_does_not_fire(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    add_rule(db)
    rec, clock = Recorder(), Clock()
    ev, _ = make_eval(engine, prom, rec, clock)
    prom.vector = [{"a": "b"}]
    await ev.tick()
    clock.now += timedelta(seconds=40)
    prom.vector = []
    await ev.tick()
    prom.vector = [{"a": "b"}]
    clock.now += timedelta(seconds=40)
    await ev.tick()
    assert rec.sent == [] and db.scalars(select(AlertEvent)).all() == []


async def test_restart_keeps_open_event_and_prometheus_errors_change_nothing(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    add_rule(db, for_seconds=0)
    rec, clock = Recorder(), Clock()
    prom.vector = [{"a": "b"}]
    ev, _ = make_eval(engine, prom, rec, clock)
    await ev.tick()
    ev2, _ = make_eval(engine, prom, rec, clock)  # "restart"
    await ev2.tick()
    assert len(db.scalars(select(AlertEvent)).all()) == 1
    prom.error = {"errorType": "timeout", "error": "boom"}
    await ev2.tick()
    db.expire_all()
    assert db.scalars(select(AlertEvent)).one().state == AlertState.FIRING


async def test_instance_scope_and_disabled_rule(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    db.add(
        Instance(
            id=INSTANCE_UUID,
            type_id="pfsense",
            name="fw",
            tags=[],
            enabled=True,
            config={},
            secrets=None,
        )
    )
    db.commit()
    rule = add_rule(db, for_seconds=0, instance_id=INSTANCE_UUID)
    rec, clock = Recorder(), Clock()
    ev, _ = make_eval(engine, prom, rec, clock)
    prom.vector = [{"vantage_instance_id": OTHER_ID}]
    await ev.tick()
    assert rec.sent == []
    prom.vector = [{"vantage_instance_id": INSTANCE_ID}]
    await ev.tick()
    assert len(rec.sent) == 1
    rule.enabled = False
    db.commit()
    await ev.tick()
    assert rec.sent[1][1].startswith("RESOLVED")


async def test_failing_notifier_never_breaks_the_loop(
    engine: Engine, db: Session, prom: FakeProm, caplog: pytest.LogCaptureFixture
) -> None:
    add_rule(db, for_seconds=0)
    prom.vector = [{"a": "b"}]
    ev, _ = make_eval(engine, prom, Recorder(fail=True), Clock())
    with caplog.at_level(logging.DEBUG):
        await ev.tick()
    db.expire_all()
    assert db.scalars(select(AlertEvent)).one().state == AlertState.FIRING
    assert "example-token" not in caplog.text


def test_clean_strips_control_characters_and_caps() -> None:
    assert clean("a\x00b‮c\n d") == "a b c d"
    assert len(clean("x" * 500)) == 48

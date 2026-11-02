"""Alert rules API, evaluator and templates (M7.1, M7.3); one alert per series (P0-3a)."""

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
from app.models import (
    AlertEvent,
    AlertNotification,
    AlertRule,
    AlertState,
    AuditLog,
    Channel,
    ChannelType,
    EndReason,
    Instance,
    NotificationKind,
    Role,
)
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
        with make_session_factory(engine)() as db:
            add_channel(db)  # RULE sends to channel 7
        yield c


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


RULE = {
    "name": "Disk full",
    "expr": "up == 0",
    "for_seconds": 60,
    "severity": "critical",
    "channel_ids": [7],
}


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
        db.add(
            AlertEvent(
                rule_id=rule_id,
                fingerprint=f"f{i}",
                rule_name="Disk full",
                severity="critical",
                state=state,
                started_at=utcnow(),
                summary=f"s{i}",
            )
        )
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


def test_ping_check_has_instance_down_template() -> None:
    """At 100% loss ping_check reports no loss sample, so "down" must use instance_up."""
    by_id = {t.id: t for t in TEMPLATES}
    down = by_id["instance-down-ping_check"]
    assert down.expr == 'vantage_instance_up{vantage_integration="ping_check"} == 0'
    assert not any(">= 1" in t.expr and "ping_check" in t.expr for t in TEMPLATES)


# --- evaluator ---------------------------------------------------------------------------


def add_channel(db: Session, channel_id: int = 7) -> None:
    """A channel row for the outbox's foreign key (never sent to in these tests)."""
    if db.get(Channel, channel_id) is None:
        db.add(Channel(id=channel_id, type=ChannelType.TELEGRAM, name=f"ch{channel_id}"))
        db.commit()


class Recorder:
    """What was queued in the alert_notifications outbox, in order (P0-5). Only FIRING and
    RESOLVED messages unless ``rule_changes``."""

    def __init__(self, rule_changes: bool = False) -> None:
        self.engine: Engine | None = None
        self.kinds = [NotificationKind.FIRING, NotificationKind.RESOLVED]
        if rule_changes:
            self.kinds.append(NotificationKind.RULE_CHANGE)

    @property
    def sent(self) -> list[tuple[list[int], str]]:
        assert self.engine is not None
        with make_session_factory(self.engine)() as db:
            rows = db.scalars(
                select(AlertNotification)
                .where(AlertNotification.kind.in_(self.kinds))
                .order_by(AlertNotification.id)
            )
            return [([r.channel_id or 0], r.text) for r in rows]


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
    notifier.engine = engine
    with make_session_factory(engine)() as db:
        add_channel(db)
    ev = AlertEvaluator(
        session_factory=make_session_factory(engine),
        prometheus=prom.client,
        hub=hub,
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


async def test_dead_ping_host_fires_instance_down(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    tpl = next(t for t in TEMPLATES if t.id == "instance-down-ping_check")
    add_rule(db, name=tpl.name, expr=tpl.expr, for_seconds=tpl.for_seconds)
    rec, clock = Recorder(), Clock()
    ev, _ = make_eval(engine, prom, rec, clock)
    # What the collector exports once poll() has raised NoReply for N cycles in a row:
    # vantage_instance_up 0. The collector already waited, so the template fires at once.
    assert tpl.for_seconds == 0
    prom.vector = [{"vantage_instance": "nas-ping", "vantage_integration": "ping_check"}]
    await ev.tick()
    assert [text for _, text in rec.sent] == [f"FIRING: {tpl.name} (critical): nas-ping"]


def test_down_templates_wait_only_for_scrape_only_integrations() -> None:
    down = {t.id: t.for_seconds for t in TEMPLATES if t.id.startswith("instance-down-")}
    assert down["instance-down-snmp_device"] == 0  # vantage_instance_up: N cycles already
    assert down["instance-down-pfsense"] == 180  # Prometheus' up flips on one failed scrape


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
    # Disabled behind the API's back: the evaluator's safety net closes the event.
    rule.enabled = False
    db.commit()
    await ev.tick()
    db.expire_all()
    event = db.scalars(select(AlertEvent)).one()
    assert event.state == AlertState.RESOLVED and event.end_reason == EndReason.RULE_DISABLED
    assert event.instance_id == INSTANCE_UUID


def test_clean_strips_control_characters_and_caps() -> None:
    assert clean("a\x00b‮c\n d") == "a b c d"
    assert len(clean("x" * 500)) == 48


# --- P0-3a: one alert per series, history kept --------------------------------------------


def add_instance(db: Session, instance_id: uuid.UUID, name: str) -> None:
    db.add(Instance(id=instance_id, type_id="pfsense", name=name, tags=[], config={}))
    db.commit()


def open_event(
    db: Session, rule_id: int, fp: str, instance_id: uuid.UUID | None = None
) -> AlertEvent:
    event = AlertEvent(
        rule_id=rule_id,
        fingerprint=fp,
        instance_id=instance_id,
        rule_name="Disk full",
        severity="critical",
        state=AlertState.FIRING,
        started_at=utcnow(),
        summary=fp,
    )
    db.add(event)
    db.commit()
    return event


def events_by_fp(db: Session) -> dict[str, AlertEvent]:
    db.expire_all()
    return {e.fingerprint: e for e in db.scalars(select(AlertEvent))}


async def test_two_devices_down_under_one_rule_are_two_alerts(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    add_instance(db, INSTANCE_UUID, "fw-a")
    add_rule(db, for_seconds=0)
    rec, clock = Recorder(), Clock()
    ev, sub = make_eval(engine, prom, rec, clock)
    prom.vector = [{"vantage_instance": "fw-a", "vantage_instance_id": INSTANCE_ID}]
    await ev.tick()
    prom.vector.append({"vantage_instance": "fw-b", "vantage_instance_id": OTHER_ID})
    await ev.tick()
    await ev.tick()
    assert [t for _, t in rec.sent] == [
        "FIRING: Gateway (critical): fw-a",
        "FIRING: Gateway (critical): fw-b",
    ]
    first = sub.queue.get_nowait()
    assert first["instance_id"] == INSTANCE_ID and len(first["fingerprint"]) == 32
    events = db.scalars(select(AlertEvent).order_by(AlertEvent.id)).all()
    assert [e.instance_id for e in events] == [INSTANCE_UUID, None]  # fw-b isn't an instance
    assert events[1].labels["vantage_instance"] == "fw-b"
    assert {e.rule_name for e in events} == {"Gateway"}

    # One recovers: only its event resolves.
    prom.vector = prom.vector[:1]
    await ev.tick()
    db.expire_all()
    assert [
        (e.state, e.end_reason) for e in db.scalars(select(AlertEvent).order_by(AlertEvent.id))
    ] == [
        (AlertState.FIRING, None),
        (AlertState.RESOLVED, EndReason.RECOVERED),
    ]
    assert rec.sent[-1][1] == "RESOLVED: Gateway (critical): fw-b"


async def test_legacy_event_is_adopted_by_a_single_series(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    rule = add_rule(db, for_seconds=0)
    open_event(db, rule.id, "legacy")
    rec = Recorder()
    ev, _ = make_eval(engine, prom, rec, Clock())
    prom.vector = [{"vantage_instance": "fw", "if_index": "1"}]
    await ev.tick()
    (event,) = events_by_fp(db).values()
    assert event.fingerprint != "legacy" and event.labels["if_index"] == "1"
    assert event.state == AlertState.FIRING and rec.sent == []  # no second FIRING message


async def test_legacy_event_with_several_series_closes_quietly(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    """Which device the old event meant is unknown: it must not swallow B's FIRING."""
    rule = add_rule(db, for_seconds=0)
    open_event(db, rule.id, "legacy")
    rec = Recorder()
    ev, _ = make_eval(engine, prom, rec, Clock())
    prom.vector = [
        {"vantage_instance": "fw-a", "vantage_instance_id": INSTANCE_ID},
        {"vantage_instance": "fw-b", "vantage_instance_id": OTHER_ID},
    ]
    await ev.tick()
    legacy = events_by_fp(db)["legacy"]
    assert (legacy.state, legacy.end_reason) == (AlertState.RESOLVED, EndReason.RULE_CHANGED)
    assert sorted(t for _, t in rec.sent) == [
        "FIRING: Gateway (critical): fw-a",
        "FIRING: Gateway (critical): fw-b",
    ]


async def test_disable_and_enable_within_one_tick_waits_the_full_for(
    admin: TestClient,
    engine: Engine,
    db: Session,
    prom: FakeProm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule_id = admin.post(f"{API}/alerts/rules", json={**RULE, "for_seconds": 600}).json()["id"]
    rec, clock = Recorder(), Clock()
    monkeypatch.setattr("app.api.alerts.utcnow", clock)
    ev, _ = make_eval(engine, prom, rec, clock)
    prom.vector = [{"vantage_instance": "fw"}]
    await ev.tick()
    clock.now += timedelta(seconds=601)
    await ev.tick()
    assert len(rec.sent) == 1  # FIRING
    url = f"{API}/alerts/rules/{rule_id}"
    admin.patch(url, json={"enabled": False})
    admin.patch(url, json={"enabled": True})
    clock.now += timedelta(seconds=30)
    await ev.tick()
    assert len(rec.sent) == 1  # the old timer must not fire it again at once
    clock.now += timedelta(seconds=571)
    await ev.tick()
    assert len(rec.sent) == 2 and rec.sent[1][1].startswith("FIRING")


async def test_closing_an_event_counts_for_again_from_the_close(
    admin: TestClient,
    engine: Engine,
    db: Session,
    prom: FakeProm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin.post(f"{API}/alerts/rules", json=RULE)  # for 60 s
    rec, clock = Recorder(), Clock()
    monkeypatch.setattr("app.api.alerts.utcnow", clock)
    ev, _ = make_eval(engine, prom, rec, clock)
    prom.vector = [{"vantage_instance": "fw"}]
    await ev.tick()
    clock.now += timedelta(seconds=61)
    await ev.tick()
    (event,) = events_by_fp(db).values()
    clock.now += timedelta(seconds=100)
    assert admin.post(f"{API}/alerts/events/{event.id}/close").status_code == 200
    clock.now += timedelta(seconds=30)
    await ev.tick()
    assert len(rec.sent) == 1
    clock.now += timedelta(seconds=31)
    await ev.tick()
    assert len(rec.sent) == 2


async def test_deleting_the_instance_of_a_scoped_rule_closes_its_events(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    """The rule row goes with its instance (CASCADE), its events stay with rule_id NULL,
    and the next tick resolves them as rule_deleted (no channel message: the rule is gone)."""
    add_instance(db, INSTANCE_UUID, "fw-a")
    rule = add_rule(db, for_seconds=0, instance_id=INSTANCE_UUID)
    rule_id = rule.id
    rec = Recorder()
    ev, _ = make_eval(engine, prom, rec, Clock())
    prom.vector = [{"vantage_instance_id": INSTANCE_ID}]
    await ev.tick()
    assert len(rec.sent) == 1
    db.execute(Instance.__table__.delete().where(Instance.id == INSTANCE_UUID))
    db.commit()
    await ev.tick()
    db.expire_all()
    event = db.scalars(select(AlertEvent)).one()
    assert event.rule_id is None and db.get(AlertRule, rule_id) is None
    assert (event.state, event.end_reason) == (AlertState.RESOLVED, EndReason.RULE_DELETED)
    assert len(rec.sent) == 1


def test_deleting_a_rule_keeps_its_events(admin: TestClient, db: Session, engine: Engine) -> None:
    rec = Recorder(rule_changes=True)
    rec.engine = engine
    add_channel(db)
    rule_id = admin.post(f"{API}/alerts/rules", json={**RULE, "channel_ids": [7]}).json()["id"]
    open_event(db, rule_id, "f1")
    assert admin.delete(f"{API}/alerts/rules/{rule_id}").status_code == 204
    assert admin.delete(f"{API}/alerts/rules/{rule_id}").status_code == 404
    assert admin.get(f"{API}/alerts/rules").json() == []
    (event,) = admin.get(f"{API}/alerts/events").json()
    assert event["rule_id"] is None and event["rule_name"] == "Disk full"
    assert event["state"] == "resolved" and event["end_reason"] == "rule_deleted"
    assert [t for _, t in rec.sent] == ["Disk full: Rule deleted: 1 open alert closed"]


def test_no_rule_operation_loses_an_event(admin: TestClient, db: Session) -> None:
    add_instance(db, INSTANCE_UUID, "fw-a")
    rule_id = admin.post(f"{API}/alerts/rules", json=RULE).json()["id"]
    for i in range(3):
        open_event(db, rule_id, f"f{i}", INSTANCE_UUID if i else None)
    url = f"{API}/alerts/rules/{rule_id}"
    patches = [
        {"name": "Renamed"},
        {"for_seconds": 5},
        {"severity": "info"},
        {"channel_ids": []},
        {"instance_id": INSTANCE_ID},
        {"expr": "up == 2"},
        {"enabled": False},
        {"enabled": True},
    ]
    for patch in patches:
        assert admin.patch(url, json=patch).status_code == 200, patch
        assert admin.get(f"{API}/alerts/events").json().__len__() == 3
    assert admin.delete(url).status_code == 204
    assert db.query(AlertEvent).count() == 3


def test_rule_edits_close_the_right_events(admin: TestClient, db: Session, engine: Engine) -> None:
    rec = Recorder(rule_changes=True)
    rec.engine = engine
    add_channel(db)
    add_instance(db, INSTANCE_UUID, "fw-a")
    add_instance(db, uuid.UUID(OTHER_ID), "fw-b")
    rule_id = admin.post(f"{API}/alerts/rules", json={**RULE, "channel_ids": [7]}).json()["id"]
    url = f"{API}/alerts/rules/{rule_id}"
    open_event(db, rule_id, "a", INSTANCE_UUID)
    open_event(db, rule_id, "b", uuid.UUID(OTHER_ID))

    # Severity, name, for and whitespace in the expression keep the events (and snapshots).
    admin.patch(url, json={"severity": "info", "name": "New", "expr": "up  ==\n0"})
    assert {e.state for e in events_by_fp(db).values()} == {AlertState.FIRING}
    assert events_by_fp(db)["a"].severity == "critical" and rec.sent == []

    # Scope: only the out-of-scope event closes.
    admin.patch(url, json={"instance_id": INSTANCE_ID})
    got = events_by_fp(db)
    assert got["a"].state == AlertState.FIRING
    assert (got["b"].state, got["b"].end_reason) == (AlertState.RESOLVED, EndReason.RULE_CHANGED)

    # Expression: everything closes, one message for the rule.
    admin.patch(url, json={"expr": "up == 1"})
    assert events_by_fp(db)["a"].end_reason == EndReason.RULE_CHANGED
    assert [t for _, t in rec.sent] == [
        "New: Rule changed: 1 open alert closed, re-checking",
        "New: Rule changed: 1 open alert closed, re-checking",
    ]

    # Disable.
    open_event(db, rule_id, "c", INSTANCE_UUID)
    admin.patch(url, json={"enabled": False})
    assert events_by_fp(db)["c"].end_reason == EndReason.RULE_DISABLED
    assert db.get(AlertRule, rule_id).revision == 4  # type: ignore[union-attr]


async def test_expression_change_restarts_pending_timers(
    admin: TestClient, engine: Engine, db: Session, prom: FakeProm
) -> None:
    rule_id = admin.post(f"{API}/alerts/rules", json=RULE).json()["id"]  # for 60 s
    rec, clock = Recorder(), Clock()
    ev, _ = make_eval(engine, prom, rec, clock)
    prom.vector = [{"a": "b"}]
    await ev.tick()
    clock.now += timedelta(seconds=50)
    admin.patch(f"{API}/alerts/rules/{rule_id}", json={"expr": "up == 1"})
    await ev.tick()
    clock.now += timedelta(seconds=20)  # 70 s since first seen, 20 s under the new expr
    await ev.tick()
    assert rec.sent == []
    clock.now += timedelta(seconds=45)
    await ev.tick()
    assert len(rec.sent) == 1


def test_close_event_endpoint(admin: TestClient, db: Session) -> None:
    rule_id = admin.post(f"{API}/alerts/rules", json=RULE).json()["id"]
    event_id = open_event(db, rule_id, "f1").id
    url = f"{API}/alerts/events/{event_id}/close"

    add_user(db, "viewer1", Role.VIEWER)
    viewer = TestClient(admin.app, base_url="https://testserver")
    viewer.headers[CSRF_HEADER] = login(viewer, "viewer1")["csrf_token"]
    assert viewer.post(url).status_code == 403
    no_csrf = TestClient(admin.app, base_url="https://testserver")
    login(no_csrf, ADMIN)
    assert no_csrf.post(url).status_code == 403

    r = admin.post(url)
    assert r.status_code == 200
    assert r.json()["state"] == "resolved" and r.json()["end_reason"] == "closed_by_user"
    assert admin.post(url).status_code == 409
    assert admin.post(f"{API}/alerts/events/999/close").status_code == 404
    entry = db.scalars(select(AuditLog).where(AuditLog.target_type == "alert_event")).one()
    assert entry.target_id == str(event_id) and entry.changes["end_reason"] == "closed_by_user"


async def test_api_never_returns_a_sensitive_label_value(
    admin: TestClient, engine: Engine, db: Session, prom: FakeProm
) -> None:
    add_rule(db, for_seconds=0)
    ev, _ = make_eval(engine, prom, Recorder(), Clock())
    prom.vector = [
        {
            "vantage_instance": "fw",
            "api_token": "example-token",
            "url": "https://admin:example-password@192.0.2.10/x?auth=example-query",
        }
    ]
    await ev.tick()
    body = admin.get(f"{API}/alerts/events").text
    for raw in ("example-token", "example-password", "example-query"):
        assert raw not in body
    (event,) = admin.get(f"{API}/alerts/events").json()
    assert event["labels"]["api_token"] == "<redacted>"
    assert event["labels"]["url"] == "https://192.0.2.10/x"

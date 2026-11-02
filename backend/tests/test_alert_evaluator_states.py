"""The evaluator with the P0-3b state machine: a fake Prometheus and a fake clock."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.alerts.evaluator import PROBE_QUERY, SCRAPED_QUERY, AlertEvaluator
from app.alerts.templates import TEMPLATES
from app.collectors.hub import EventHub
from app.core.db import make_session_factory
from app.models import AlertEvent, AlertRule, AlertState, EndReason
from tests import test_alerts
from tests.test_alerts import (
    API,
    INSTANCE_ID,
    INSTANCE_UUID,
    RULE,
    Clock,
    FakeProm,
    Recorder,
    add_channel,
    add_instance,
    add_rule,
    open_event,
)

# The fixtures of test_alerts.
admin, db, prom = test_alerts.admin, test_alerts.db, test_alerts.prom

EXPR = "vantage_ping_check_temperature > 85"
VALUE = "vantage_ping_check_temperature"
LABELS = {"vantage_instance": "fw", "vantage_instance_id": INSTANCE_ID}


class World:
    """One device, one rule, the evaluator, and helpers to move time and data."""

    def __init__(self, engine: Engine, db: Session, prom: FakeProm, **rule: object) -> None:
        self.engine, self.db, self.prom = engine, db, prom
        add_instance(db, INSTANCE_UUID, "fw")
        self.rule = add_rule(db, **{"expr": EXPR, "for_seconds": 0, **rule})
        self.clock = Clock()
        self.rec = Recorder()
        self.rec.engine = engine
        self.states: dict[uuid.UUID, str] = {}
        self.poll_ts = self.clock.now.timestamp() - 5
        self.ev = self.evaluator()

    def evaluator(self) -> AlertEvaluator:
        with make_session_factory(self.engine)() as db:
            add_channel(db)
        return AlertEvaluator(
            session_factory=make_session_factory(self.engine),
            prometheus=self.prom.client,
            hub=EventHub(),
            clock=self.clock,
            poll_interval=lambda type_id: 60.0,
            instance_state=self.states.get,
        )

    def value(self, value: float | None, expr: str = EXPR, query: str = VALUE) -> None:
        """What the device reports now (None: nothing) and the rule's result for it."""
        self.prom.answers[query] = [] if value is None else [(LABELS, repr(value))]
        self.prom.answers[expr] = [] if value is None or value <= 85 else [(LABELS, repr(value))]

    def advance(self, seconds: float, *, polled: bool = True) -> None:
        self.clock.now += timedelta(seconds=seconds)
        if polled:
            self.poll_ts = self.clock.now.timestamp() - 5
        self.prom.poll(INSTANCE_ID, self.poll_ts)

    async def tick(self, seconds: float = 0, *, polled: bool = True) -> None:
        self.advance(seconds, polled=polled)
        await self.ev.tick()

    def event(self) -> AlertEvent:
        self.db.expire_all()
        return self.db.scalars(select(AlertEvent).order_by(AlertEvent.id.desc())).first()  # type: ignore[return-value]

    @property
    def texts(self) -> list[str]:
        return [t for _, t in self.rec.sent]

    async def fire(self) -> None:
        self.value(90)
        await self.tick()
        assert self.event().state == AlertState.FIRING and len(self.texts) == 1


async def test_device_down_while_firing_is_no_data_not_resolved(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = World(engine, db, prom)
    await w.fire()
    w.value(None)  # the poll fails: the collector drops the samples
    await w.tick(60, polled=False)
    assert w.event().state == AlertState.FIRING  # last poll still fresh: wait
    await w.tick(60, polled=False)
    w.states[INSTANCE_UUID] = "down"
    await w.tick(60, polled=False)  # 185 s since the last poll > 150 s
    event = w.event()
    assert (event.state, event.no_data_reason) == (AlertState.NO_DATA, "source_down")
    assert w.texts == [w.texts[0]]  # quiet for 5 minutes
    for _ in range(10):
        await w.tick(30, polled=False)
    assert w.texts[1:] == ["NO DATA: Gateway (critical): fw (device not answering)"]
    assert w.event().ended_at is None

    # Replugged: the value is back and fine; two polls later it resolves.
    w.value(70)
    await w.tick(60)
    assert w.event().state == AlertState.NO_DATA
    await w.tick(60)
    event = w.event()
    assert (event.state, event.end_reason) == (AlertState.RESOLVED, EndReason.RECOVERED)
    assert w.texts[-1] == "RESOLVED: Gateway (critical): fw"


async def test_stale_source_without_down_status(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = World(engine, db, prom)
    await w.fire()
    w.value(None)
    await w.tick(160, polled=False)
    assert w.event().no_data_reason == "source_stale"


async def test_metric_removed_while_the_device_stays_fresh(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = World(engine, db, prom)
    await w.fire()
    w.value(None)
    await w.tick(60)  # a newer poll without the metric
    event = w.event()
    assert (event.state, event.no_data_reason) == (AlertState.NO_DATA, "metric_missing")


async def test_the_same_poll_twice_is_one_clean_check(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = World(engine, db, prom)
    await w.fire()
    w.value(70)
    await w.tick(30)
    for _ in range(3):
        await w.tick(10, polled=False)  # Prometheus still has the same poll
    assert w.event().state == AlertState.FIRING and len(w.texts) == 1
    await w.tick(30)  # a second poll
    assert w.event().state == AlertState.RESOLVED


async def test_restart_with_pending_instances_sends_nothing(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = World(engine, db, prom)
    await w.fire()
    # Restart: the collector hasn't polled yet, so Prometheus has no last success.
    w.ev = w.evaluator()
    prom.answers.pop("vantage_poll_last_success_timestamp_seconds")
    w.value(None)
    for _ in range(8):  # 4 minutes
        w.clock.now += timedelta(seconds=30)
        await w.ev.tick()
    assert w.event().state == AlertState.NO_DATA
    w.value(90)  # first poll: still hot
    await w.tick(30)
    assert w.event().state == AlertState.FIRING
    assert len(w.texts) == 1  # only the original FIRING


async def test_flapping_does_not_fire(engine: Engine, db: Session, prom: FakeProm) -> None:
    w = World(engine, db, prom, for_seconds=180)
    for i in range(20):
        w.value(90 if i % 2 == 0 else 70)
        await w.tick(30)
    assert w.texts == [] and w.event() is None


async def test_probe_failure_changes_nothing_and_pauses_timers(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = World(engine, db, prom, for_seconds=120)
    w.value(90)
    await w.tick()
    await w.tick(60)  # 60 s pending
    prom.failing = {PROBE_QUERY}
    for _ in range(8):
        await w.tick(30)
    prom.failing = set()
    await w.tick(30)  # resumes: the outage doesn't count
    assert w.event() is None
    await w.tick(60)
    assert w.event().state == AlertState.FIRING

    # While firing: a probe failure keeps the alert as it is, even with stale sources.
    w.value(None)
    prom.failing = {PROBE_QUERY}
    for _ in range(20):
        await w.tick(30, polled=False)
    assert w.event().state == AlertState.FIRING


async def test_query_failing_three_ticks(engine: Engine, db: Session, prom: FakeProm) -> None:
    w = World(engine, db, prom)
    await w.fire()
    prom.failing = {EXPR}
    await w.tick(30)
    await w.tick(30)
    assert w.event().state == AlertState.FIRING
    await w.tick(30)
    assert w.event().no_data_reason == "query_failed" and len(w.texts) == 1
    for _ in range(9):
        await w.tick(30)
    assert len(w.texts) == 1  # 4.5 min
    await w.tick(30)
    assert w.texts[1] == "NO DATA: Gateway (critical): fw (query failing)"
    assert w.ev.rule_error(w.rule.id) == "query could not be executed"


async def test_no_data_for_a_day_sends_one_reminder(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = World(engine, db, prom)
    await w.fire()
    w.value(None)
    await w.tick(200, polled=False)
    for _ in range(30):
        await w.tick(3600, polled=False)
    reminders = [t for t in w.texts if t.startswith("STILL NO DATA")]
    assert reminders == ["STILL NO DATA after 24 h: Gateway (critical): fw (device not reporting)"]
    assert w.event().reminder_sent_at is not None
    # The value returns while the alert still holds: one "data back" message.
    w.value(95)
    await w.tick(60)
    assert w.texts[-1] == "DATA BACK, still firing: Gateway (critical): fw"
    assert w.event().state == AlertState.FIRING


async def test_empty_result_rule_needs_a_fresh_advanced_source(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    expr = "x > 0 and y > 0"  # and: no witness possible
    w = World(engine, db, prom, expr=expr)
    prom.answers[expr] = [(LABELS, "1")]
    await w.tick()
    assert w.event().state == AlertState.FIRING
    prom.answers[expr] = []
    await w.tick(10, polled=False)
    await w.tick(10, polled=False)
    assert w.event().state == AlertState.FIRING  # same poll: absence proves nothing
    await w.tick(60)
    await w.tick(10, polled=False)
    assert w.event().state == AlertState.FIRING
    await w.tick(60)
    assert w.event().state == AlertState.RESOLVED


async def test_raid_or_template(engine: Engine, db: Session, prom: FakeProm) -> None:
    tpl = next(t for t in TEMPLATES if t.id == "raid-degraded-linux-host")
    w = World(engine, db, prom, expr=tpl.expr)
    degraded = 'node_md_degraded{vantage_integration="linux_host"}'
    failed = 'node_md_disks{vantage_integration="linux_host",state="failed"}'
    md0 = {**LABELS, "__name__": "node_md_degraded", "device": "md0"}
    disks = {**LABELS, "__name__": "node_md_disks", "device": "md0", "state": "failed"}
    scrape = [0.0]

    def scraped(seconds: float) -> None:
        scrape[0] = w.clock.now.timestamp() + seconds - 3
        prom.answers[SCRAPED_QUERY] = [({"vantage_instance_id": INSTANCE_ID}, repr(scrape[0]))]

    prom.answers.pop("vantage_poll_last_success_timestamp_seconds", None)
    w.advance = lambda seconds, polled=True: setattr(  # type: ignore[method-assign]
        w.clock, "now", w.clock.now + timedelta(seconds=seconds)
    )
    prom.answers[tpl.expr] = [(md0, "1")]
    prom.answers[degraded] = [(md0, "1")]
    prom.answers[failed] = [(disks, "0")]
    scraped(0)
    await w.tick()
    assert w.event().state == AlertState.FIRING
    # Repaired: both sides false for md0.
    prom.answers[tpl.expr] = []
    prom.answers[degraded] = [(md0, "0")]
    scraped(30)
    await w.tick(30)
    assert w.event().state == AlertState.FIRING  # one clean check
    scraped(30)
    await w.tick(30)
    assert w.event().state == AlertState.RESOLVED


async def test_rule_edit_drops_the_recovering_count(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = World(engine, db, prom)
    await w.fire()
    w.value(70)
    await w.tick(60)  # RECOVERING
    rule = db.get(AlertRule, w.rule.id)
    assert rule is not None
    rule.name, rule.revision = "Renamed", rule.revision + 1
    db.commit()
    await w.tick(60)  # counts as the first clean check again
    assert w.event().state == AlertState.FIRING
    await w.tick(60)
    assert w.event().state == AlertState.RESOLVED


def test_scoped_rule_without_instance_label_is_refused(
    admin: TestClient, db: Session, prom: FakeProm
) -> None:
    add_instance(db, INSTANCE_UUID, "fw")
    body = {**RULE, "expr": "sum(x) > 5", "instance_id": INSTANCE_ID}
    prom.vector = [{"job": "a"}]
    r = admin.post(f"{API}/alerts/rules", json=body)
    assert r.status_code == 422 and "vantage_instance_id" in r.text
    prom.vector = [{"vantage_instance_id": INSTANCE_ID}]
    r = admin.post(f"{API}/alerts/rules", json=body)
    assert r.status_code == 201
    assert r.json()["recovery_mode"] == "value" and r.json()["error"] is None

    # Prometheus can't answer: saved, with the error shown.
    prom.error = {"errorType": "timeout", "error": "boom"}
    r = admin.post(f"{API}/alerts/rules", json={**body, "expr": "a > 1 and b"})
    assert r.status_code == 201
    assert r.json()["recovery_mode"] == "empty_result" and r.json()["error"] == "query timed out"
    listed = {x["id"]: x for x in admin.get(f"{API}/alerts/rules").json()}
    assert listed[r.json()["id"]]["error"] == "query timed out"


@pytest.mark.parametrize("reason", ["source_down", "metric_missing"])
def test_no_data_event_in_the_api_and_closable(admin: TestClient, db: Session, reason: str) -> None:
    rule_id = admin.post(f"{API}/alerts/rules", json=RULE).json()["id"]
    event = open_event(db, rule_id, "f1")
    event.state, event.no_data_reason = AlertState.NO_DATA, reason
    event.state_since = event.started_at
    db.commit()
    (got,) = admin.get(f"{API}/alerts/events", params={"state": "no_data"}).json()
    assert got["state"] == "no_data" and got["no_data_reason"] == reason
    assert got["state_since"] is not None
    r = admin.post(f"{API}/alerts/events/{event.id}/close")
    assert r.status_code == 200 and r.json()["end_reason"] == "closed_by_user"
    assert r.json()["no_data_reason"] is None

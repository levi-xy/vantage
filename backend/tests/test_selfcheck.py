"""Self-monitoring (P0-4 part 1): system alerts, the watchdog, /healthz; fake clock."""

import asyncio
import logging
from datetime import timedelta

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.alerts.selfcheck import METRICS_QUERY, Health, SelfMonitor, Watchdog
from app.alerts.templates import TEMPLATES
from app.api.deps import CSRF_HEADER
from app.core.db import make_session_factory, utcnow
from app.models import (
    AlertEvent,
    AlertNotification,
    AlertState,
    Channel,
    ChannelType,
    EventSource,
    NotificationKind,
    NotificationStatus,
    SystemKind,
)
from tests import test_alerts
from tests.test_alert_evaluator_states import World
from tests.test_alerts import API, INSTANCE_UUID, FakeProm
from tests.test_auth import do_setup, make_client
from tests.test_channels import public_test_net
from tests.test_delivery import TOKEN

__all__ = ["public_test_net"]  # the autouse fixture: api.telegram.org on TEST-NET-1

db, prom = test_alerts.db, test_alerts.prom

K = NotificationKind
S = NotificationStatus
DOWN = next(t for t in TEMPLATES if t.id == "instance-down-ping_check")
DOWN_VALUE = 'vantage_instance_up{vantage_integration="ping_check"}'
LOAD = "vantage_ping_check_load > 5"
LOAD_VALUE = "vantage_ping_check_load"


class SW(World):
    """``World`` plus helpers for system alerts."""

    def systems(self) -> list[AlertEvent]:
        self.db.expire_all()
        stmt = select(AlertEvent).where(AlertEvent.source == EventSource.SYSTEM)
        return list(self.db.scalars(stmt.order_by(AlertEvent.id)))

    def alert(self, rule_id: int | None = None) -> AlertEvent:
        self.db.expire_all()
        stmt = select(AlertEvent).where(AlertEvent.rule_id == (rule_id or self.rule.id))
        return self.db.scalars(stmt.order_by(AlertEvent.id.desc())).first()  # type: ignore[return-value]

    def rows(self) -> list[AlertNotification]:
        self.db.expire_all()
        return list(self.db.scalars(select(AlertNotification).order_by(AlertNotification.id)))


# --- Prometheus unreachable --------------------------------------------------------------


async def test_prometheus_refusing_for_two_minutes_sends_one_system_alert(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    await w.fire()
    prom.down = True
    for _ in range(4):  # 0 to 90 s of failed probes
        await w.tick(30, polled=False)
    assert w.systems() == [] and len(w.rows()) == 1
    await w.tick(30, polled=False)  # 2 minutes
    (system,) = w.systems()
    assert (system.system_kind, system.state, system.rule_id) == (
        "prometheus_unreachable",
        AlertState.FIRING,
        None,
    )
    carrier = w.rows()[-1]
    assert carrier.event_id == system.id and carrier.kind == K.FIRING
    assert carrier.text.startswith("SYSTEM ALERT: Prometheus unreachable: since ")
    assert carrier.text.endswith("UTC. 1 open alert and 1 rule can't be checked")
    assert w.ev.monitor.health() == Health.PROMETHEUS_UNREACHABLE
    count = len(w.rows())
    for _ in range(10):  # the outage goes on: nothing more
        await w.tick(30, polled=False)
    assert len(w.rows()) == count and len(w.systems()) == 1

    prom.down = False
    await w.tick(30)
    assert w.alert().state == AlertState.FIRING
    assert w.systems()[0].state == AlertState.RESOLVED
    resolved = w.rows()[-1]
    assert (resolved.kind, resolved.event_id) == (K.RESOLVED, system.id)
    assert resolved.text.startswith("RESOLVED: Prometheus back after ")
    assert resolved.text.endswith(" min.")
    assert w.ev.monitor.health() is None


async def test_a_90_second_outage_changes_nothing(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    await w.fire()
    prom.down = True
    for _ in range(4):
        await w.tick(30, polled=False)
    prom.down = False
    await w.tick(30)
    assert w.systems() == [] and w.alert().state == AlertState.FIRING
    assert len(w.rows()) == 1
    assert w.ev.monitor.health() is None


async def test_channels_without_system_alerts_get_none(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    db.execute(update(Channel).values(system_alerts=False))
    db.add(Channel(id=8, type=ChannelType.TELEGRAM, name="ops", system_alerts=True))
    db.commit()
    await w.fire()
    prom.down = True
    for _ in range(5):
        await w.tick(30, polled=False)
    (system,) = w.systems()
    carriers = [r for r in w.rows() if r.event_id == system.id]
    assert [r.channel_id for r in carriers] == [8]


# --- metrics not collected ---------------------------------------------------------------


async def test_metrics_not_collected(engine: Engine, db: Session, prom: FakeProm) -> None:
    w = SW(engine, db, prom)
    w.states[INSTANCE_UUID] = "up"  # a polled device
    await w.fire()
    prom.answers[METRICS_QUERY] = []  # Prometheus stopped scraping Vantage
    for _ in range(4):
        await w.tick(30)
    assert w.systems() == []
    await w.tick(30)  # 2 minutes without a scrape
    (system,) = w.systems()
    assert system.system_kind == "metrics_not_collected"
    (carrier,) = w.rows()[1:]
    assert carrier.text.endswith("1 open alert on polled devices can't be checked")
    assert w.ev.monitor.health() == Health.METRICS_NOT_COLLECTED

    prom.answers[METRICS_QUERY] = [({}, "10")]
    await w.tick(30)
    assert w.rows()[-1].text.startswith("RESOLVED: Metrics collected again after ")
    assert w.ev.monitor.health() is None


async def test_old_scrape_right_after_prometheus_returns_is_not_an_alarm(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    await w.fire()
    prom.down = True
    for _ in range(6):
        await w.tick(30, polled=False)
    prom.down = False
    prom.answers[METRICS_QUERY] = [({}, "200")]  # the last scrape before the outage
    await w.tick(30)
    assert [s.system_kind for s in w.systems()] == ["prometheus_unreachable"]


# --- the watchdog ------------------------------------------------------------------------


async def test_blocked_evaluator_raises_a_system_alert_and_is_restarted_once(
    engine: Engine, db: Session, prom: FakeProm, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = SW(engine, db, prom)
    ev = w.ev
    block = asyncio.Event()

    async def stuck() -> None:
        await block.wait()

    restarts = 0

    async def restart() -> None:
        nonlocal restarts
        restarts += 1
        await ev.restart()

    monkeypatch.setattr(ev, "tick", stuck)
    dog = Watchdog(ev.monitor, restart)
    ev.start()
    try:
        await asyncio.sleep(0)
        await dog.check()
        assert restarts == 0 and w.systems() == []
        w.clock.now += timedelta(seconds=91)
        await dog.check()
        assert restarts == 1
        (system,) = w.systems()
        assert (system.system_kind, system.state) == ("evaluator_stalled", AlertState.FIRING)
        assert w.rows()[-1].text.startswith("SYSTEM ALERT: Alert evaluation stopped")
        assert ev.monitor.health() == Health.EVALUATOR_STALLED
        w.clock.now += timedelta(seconds=300)
        await dog.check()
        assert restarts == 1  # once

        monkeypatch.undo()
        await ev.tick()  # it ticks again
        await dog.check()
        assert w.systems()[0].state == AlertState.RESOLVED
        assert ev.monitor.health() is None
    finally:
        await ev.stop()


# --- /healthz ----------------------------------------------------------------------------


def test_healthz_reasons(engine: Engine, db_url: str) -> None:
    client = make_client(db_url)
    monitor: SelfMonitor = client.app.state.self_monitor  # type: ignore[attr-defined]
    now = utcnow()

    def check(reason: str | None) -> None:
        r = client.get("/healthz")
        if reason is None:
            assert (r.status_code, r.json()) == (200, {"status": "ok"})
        else:
            assert (r.status_code, r.json()) == (503, {"reason": reason})
        assert client.get("/healthz/live").status_code == 200

    check(None)
    with make_session_factory(engine)() as s:
        s.add(Channel(id=3, type=ChannelType.TELEGRAM, name="t"))
        s.add(
            AlertNotification(
                channel_id=3,
                kind=K.FIRING,
                text="x",
                status=S.PENDING,
                next_attempt_at=now,
                created_at=now - timedelta(minutes=20),
            )
        )
        s.commit()
    check("notifications_stuck")
    monitor.record_probe(True, now - timedelta(minutes=5))
    monitor.record_metrics(400.0, now)
    check("metrics_not_collected")
    monitor.record_probe(False, now - timedelta(minutes=3))
    check("prometheus_unreachable")
    monitor.evaluator_started(now - timedelta(minutes=5))
    check("evaluator_stalled")
    monitor.tick_finished(utcnow())
    monitor.record_probe(True, now - timedelta(minutes=5))
    monitor.record_metrics(5.0, now)
    with make_session_factory(engine)() as s:
        s.execute(update(AlertNotification).values(status=S.DELIVERED, finished_at=now))
        s.commit()
    check(None)


# --- the API -----------------------------------------------------------------------------


def test_api_system_alerts_and_channel_setting(
    engine: Engine, db_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with make_client(db_url) as c:
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        body = {"type": "telegram", "name": "ops", "config": {"chat_id": "1", "bot_token": TOKEN}}
        created = c.post(f"{API}/channels", json=body).json()
        assert created["system_alerts"] is True
        quiet = c.post(f"{API}/channels", json={**body, "name": "q", "system_alerts": False})
        assert quiet.json()["system_alerts"] is False
        patched = c.patch(f"{API}/channels/{created['id']}", json={"system_alerts": False})
        assert patched.json()["system_alerts"] is False
        c.patch(f"{API}/channels/{created['id']}", json={"system_alerts": True})
        assert (
            c.patch(f"{API}/channels/{created['id']}", json={"system_alerts": None}).status_code
            == 422
        )

        monitor: SelfMonitor = c.app.state.self_monitor  # type: ignore[attr-defined]
        assert monitor.open_alert(SystemKind.PROMETHEUS_UNREACHABLE, "since now", since=utcnow())
        assert not monitor.open_alert(SystemKind.PROMETHEUS_UNREACHABLE, "again", since=utcnow())
        status = c.get(f"{API}/alerts/delivery-status").json()
        assert [a["system_kind"] for a in status["system_alerts"]] == ["prometheus_unreachable"]
        (event,) = c.get(f"{API}/alerts/events").json()
        assert (event["source"], event["system_kind"], event["rule_id"]) == (
            "system",
            "prometheus_unreachable",
            None,
        )
        assert [d["channel_name"] for d in event["deliveries"]] == ["ops"]
        assert c.post(f"{API}/alerts/events/{event['id']}/close").status_code == 409
        bodies = [
            c.get(path).text
            for path in (f"{API}/alerts/events", f"{API}/channels", f"{API}/alerts/delivery-status")
        ]
        bodies.append(c.get("/healthz").text)
        assert all(TOKEN not in b for b in bodies)
        with make_session_factory(engine)() as s:
            texts = list(s.scalars(select(AlertNotification.text)))
        assert texts and all(TOKEN not in t for t in texts)
    assert TOKEN not in caplog.text

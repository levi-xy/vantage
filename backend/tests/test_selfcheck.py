"""Self-monitoring and shared-failure folding (P0-4): a fake Prometheus and a fake clock."""

import asyncio
import logging
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.alerts.delivery import DeliveryDispatcher
from app.alerts.evaluator import AlertEvaluator
from app.alerts.selfcheck import METRICS_QUERY, Health, SelfMonitor, Watchdog
from app.alerts.templates import TEMPLATES
from app.api.deps import CSRF_HEADER
from app.collectors.hub import EventHub
from app.core.crypto import SecretBox
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
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import FakeNetwork
from tests.test_alert_evaluator_states import LABELS, World
from tests.test_alerts import API, INSTANCE_UUID, FakeProm, add_rule
from tests.test_auth import do_setup, make_client
from tests.test_channels import ADDRESS, factory_for, public_test_net
from tests.test_delivery import CHAT_A, TOKEN, FakeTelegram
from tests.test_delivery import add_channel as add_telegram

__all__ = ["public_test_net"]  # the autouse fixture: api.telegram.org on TEST-NET-1

db, prom = test_alerts.db, test_alerts.prom

K = NotificationKind
S = NotificationStatus
DOWN = next(t for t in TEMPLATES if t.id == "instance-down-ping_check")
DOWN_VALUE = 'vantage_instance_up{vantage_integration="ping_check"}'
LOAD = "vantage_ping_check_load > 5"
LOAD_VALUE = "vantage_ping_check_load"


class SW(World):
    """``World`` plus the collector's failure streak."""

    def __init__(self, engine: Engine, db: Session, prom: FakeProm, **rule: object) -> None:
        self.failed: dict[uuid.UUID, int] = {}
        super().__init__(engine, db, prom, **rule)

    def evaluator(self) -> AlertEvaluator:
        with make_session_factory(self.engine)() as db:
            test_alerts.add_channel(db)
        return AlertEvaluator(
            session_factory=make_session_factory(self.engine),
            prometheus=self.prom.client,
            hub=EventHub(),
            clock=self.clock,
            poll_interval=lambda type_id: 60.0,
            instance_state=self.states.get,
            failed_cycles=lambda i: self.failed.get(i, 0),
        )

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

    def load(self, value: float) -> None:
        self.prom.answers[LOAD_VALUE] = [(LABELS, repr(value))]
        self.prom.answers[LOAD] = [(LABELS, repr(value))] if value > 5 else []

    def device_down(self, down: bool) -> None:
        self.prom.answers[DOWN_VALUE] = [(LABELS, "0" if down else "1")]
        self.prom.answers[DOWN.expr] = [(LABELS, "0")] if down else []


# --- Prometheus unreachable --------------------------------------------------------------


async def test_prometheus_refusing_for_two_minutes_sends_one_system_alert(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    await w.fire()
    load = add_rule(db, name="Load", expr=LOAD, for_seconds=0)
    w.load(9)
    await w.tick(30)
    assert w.alert(load.id).state == AlertState.FIRING
    before = len(w.rows())

    prom.down = True
    for _ in range(4):  # 0 to 90 s of failed probes
        await w.tick(30, polled=False)
    assert w.systems() == [] and len(w.rows()) == before
    await w.tick(30, polled=False)  # 2 minutes
    (system,) = w.systems()
    assert (system.system_kind, system.state, system.rule_id) == (
        "prometheus_unreachable",
        AlertState.FIRING,
        None,
    )
    for rule_id in (w.rule.id, load.id):
        alert = w.alert(rule_id)
        assert (alert.state, alert.no_data_reason) == (AlertState.NO_DATA, "prometheus_unreachable")
    new = w.rows()[before:]
    carrier = next(r for r in new if r.event_id == system.id)
    assert carrier.status == S.PENDING and carrier.kind == K.FIRING
    assert carrier.text.startswith("SYSTEM ALERT: Prometheus unreachable: since ")
    assert carrier.text.endswith("UTC. 2 open alerts and 2 rules can't be checked")
    folded = [r for r in new if r.event_id != system.id]
    assert [(r.kind, r.status, r.folded_into) for r in folded] == [
        (K.NO_DATA, S.FOLDED, carrier.id)
    ] * 2
    count = len(w.rows())
    for _ in range(10):  # the outage goes on: nothing more
        await w.tick(30, polled=False)
    assert len(w.rows()) == count and len(w.systems()) == 1

    # Back: the first tick re-checks everything. Still hot: FIRING again, silently.
    prom.down = False
    w.load(1)  # recovering: one clean check
    await w.tick(30)
    assert w.alert().state == AlertState.FIRING
    assert w.systems()[0].state == AlertState.RESOLVED
    tail = w.rows()[count:]
    assert [(r.kind, r.event_id) for r in tail] == [(K.RESOLVED, system.id)]
    assert tail[0].text.startswith("RESOLVED: Prometheus back after ")
    assert tail[0].text.endswith(" min. 1 alert still firing, 1 recovering.")
    await w.tick(30)  # the second clean check: RESOLVED as usual
    assert w.rows()[-1].text == "RESOLVED: Load (critical): fw"


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


async def test_system_alert_reaches_telegram_once_and_folded_rows_share_its_outcome(
    engine: Engine, db: Session, prom: FakeProm, caplog: pytest.LogCaptureFixture
) -> None:
    network = FakeNetwork()
    tg = FakeTelegram()
    network.serve(ADDRESS, 443, tg)
    cid = add_telegram(db)
    w = SW(engine, db, prom, channel_ids=[cid])
    db.execute(update(Channel).where(Channel.id != cid).values(system_alerts=False))
    db.commit()
    dispatcher = DeliveryDispatcher(
        session_factory=make_session_factory(engine),
        secret_box=SecretBox(TEST_MASTER_KEY),
        client_factory=factory_for(network),
        clock=w.clock,
    )
    caplog.set_level(logging.DEBUG)
    await w.fire()
    await dispatcher.run_once()
    prom.down = True
    for _ in range(5):
        await w.tick(30, polled=False)
    await dispatcher.run_once()
    assert tg.texts[CHAT_A][1].startswith("SYSTEM ALERT: Prometheus unreachable")
    assert len(tg.texts[CHAT_A]) == 2  # the FIRING before, then the system alert only
    assert {r.status for r in w.rows()} == {S.DELIVERED, S.FOLDED}
    assert all(r.finished_at is not None for r in w.rows())
    assert TOKEN not in caplog.text and TOKEN not in str(tg.texts)


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
    # The alert's own message (channel 7) is still recorded, carried by that message.
    (no_data,) = [r for r in w.rows() if r.kind == K.NO_DATA]
    assert (no_data.channel_id, no_data.folded_into) == (7, carriers[0].id)


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
    alert = w.alert()
    assert (alert.state, alert.no_data_reason) == (AlertState.NO_DATA, "metrics_not_collected")
    carrier, folded = w.rows()[1:]
    assert carrier.text.endswith("1 open alert on polled devices can't be checked")
    assert (folded.status, folded.folded_into) == (S.FOLDED, carrier.id)
    assert w.ev.monitor.health() == Health.METRICS_NOT_COLLECTED

    prom.answers[METRICS_QUERY] = [({}, "10")]
    await w.tick(30)
    assert w.alert().state == AlertState.FIRING
    assert w.rows()[-1].text.startswith("RESOLVED: Metrics collected again after ")
    assert w.rows()[-1].text.endswith("1 alert still firing.")


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


# --- a device down -----------------------------------------------------------------------


async def device_goes_down(w: SW) -> None:
    """The device stops answering: stale after 150 s, down at the third failed cycle."""
    w.value(None)
    w.failed[INSTANCE_UUID] = 1
    await w.tick(60, polled=False)
    w.failed[INSTANCE_UUID] = 2
    await w.tick(60, polled=False)
    w.failed[INSTANCE_UUID] = 3
    w.states[INSTANCE_UUID] = "down"
    await w.tick(60, polled=False)


async def test_device_down_folds_its_other_alerts_into_the_down_message(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    down = add_rule(db, name=DOWN.name, expr=DOWN.expr, for_seconds=0)
    w.device_down(False)
    await w.fire()
    await device_goes_down(w)
    assert w.alert().no_data_reason == "source_down"
    w.device_down(True)  # Prometheus scrapes vantage_instance_up 0
    await w.tick(30, polled=False)
    down_event = w.alert(down.id)
    assert down_event.state == AlertState.FIRING
    for _ in range(12):  # well past the 5 minutes a NO DATA message waits
        await w.tick(30, polled=False)
    rows = w.rows()
    assert [r.kind for r in rows] == [K.FIRING, K.FIRING, K.NO_DATA]
    carrier, folded = rows[1], rows[2]
    assert carrier.event_id == down_event.id and carrier.status == S.PENDING
    assert carrier.text == (
        f"FIRING: {down_event.summary}. 1 other alert on this device can't be checked"
    )
    assert (folded.event_id, folded.status, folded.folded_into) == (
        w.alert().id,
        S.FOLDED,
        carrier.id,
    )


async def test_device_down_after_the_down_message_was_sent_gets_one_follow_up(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    down = add_rule(db, name=DOWN.name, expr=DOWN.expr, for_seconds=0)
    await w.fire()
    w.device_down(True)  # down per Prometheus while the last poll is still fresh
    await w.tick(30)
    db.execute(
        update(AlertNotification)
        .where(AlertNotification.event_id == w.alert(down.id).id)
        .values(status=S.DELIVERED, finished_at=w.clock.now)
    )
    db.commit()
    w.value(None)
    for _ in range(18):  # stale from 150 s, its NO DATA message due 5 minutes later
        await w.tick(30, polled=False)
    folded, follow_up = w.rows()[2:]
    assert follow_up.kind == K.NO_DATA and follow_up.status == S.PENDING
    assert follow_up.text.startswith("STILL DOWN: ")
    assert follow_up.text.endswith("1 other alert on this device can't be checked")
    assert folded.folded_into == follow_up.id


async def test_failure_streak_holds_no_data_messages(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    await w.fire()
    w.value(None)
    w.failed[INSTANCE_UUID] = 1
    await w.tick(160, polled=False)
    assert w.alert().no_data_reason == "source_stale"
    w.failed[INSTANCE_UUID] = 2
    for _ in range(12):  # 6 minutes: the message would be due, but the streak holds it
        await w.tick(30, polled=False)
    assert len(w.rows()) == 1
    w.failed[INSTANCE_UUID] = 3
    w.states[INSTANCE_UUID] = "down"  # no instance-down rule: sent on its own
    await w.tick(30, polled=False)
    assert w.rows()[-1].text == "NO DATA: Gateway (critical): fw (device not answering)"
    assert w.rows()[-1].status == S.PENDING


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

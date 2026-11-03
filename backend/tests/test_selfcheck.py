"""Self-monitoring and shared-failure folding (P0-4): a fake Prometheus and a fake clock."""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.alerts import selfcheck
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
from app.query.proxy import PrometheusClient
from tests import test_alerts
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import FakeNetwork
from tests.test_alert_evaluator_states import LABELS, World
from tests.test_alerts import API, INSTANCE_UUID, Clock, FakeProm, add_rule
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


async def test_no_data_folds_only_into_a_message_to_the_same_channel(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    """Channel 7 doesn't take system alerts, channel 8 does; the rule sends to both. Its
    NO DATA row on 8 is folded into the system alert on 8; the one on 7 is never folded
    into another channel's message: it waits the usual 5 minutes and is sent on 7."""
    db.add(Channel(id=8, type=ChannelType.TELEGRAM, name="ops", system_alerts=True))
    db.commit()
    w = SW(engine, db, prom, channel_ids=[7, 8])
    db.execute(update(Channel).where(Channel.id == 7).values(system_alerts=False))
    db.commit()
    w.value(90)  # fire on both channels
    await w.tick()
    prom.down = True
    for _ in range(5):  # 2 minutes: the system alert, nothing for the alert yet
        await w.tick(30, polled=False)
    (system,) = w.systems()
    (carrier,) = [r for r in w.rows() if r.event_id == system.id]
    assert carrier.channel_id == 8
    assert [r for r in w.rows() if r.kind == K.NO_DATA] == []
    for _ in range(10):  # 5 minutes in NO DATA
        await w.tick(30, polled=False)
    rows = {r.channel_id: r for r in w.rows() if r.kind == K.NO_DATA}
    assert (rows[8].status, rows[8].folded_into) == (S.FOLDED, carrier.id)
    assert (rows[7].status, rows[7].folded_into) == (S.PENDING, None)
    assert rows[7].text == "NO DATA: Gateway (critical): fw (Prometheus unreachable)"


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


async def test_device_down_folds_only_on_the_down_rules_channels(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    db.add(Channel(id=8, type=ChannelType.TELEGRAM, name="ops"))
    db.commit()
    w = SW(engine, db, prom, channel_ids=[7, 8])
    down = add_rule(db, name=DOWN.name, expr=DOWN.expr, for_seconds=0, channel_ids=[7])
    w.device_down(False)
    w.value(90)  # fire on both channels
    await w.tick()
    await device_goes_down(w)
    w.device_down(True)
    await w.tick(30, polled=False)
    carrier = next(r for r in w.rows() if r.event_id == w.alert(down.id).id)
    rows = {r.channel_id: r for r in w.rows() if r.kind == K.NO_DATA}
    assert (rows[7].status, rows[7].folded_into) == (S.FOLDED, carrier.id)
    assert (rows[8].status, rows[8].folded_into) == (S.PENDING, None)


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


class Gate(httpx.AsyncBaseTransport):
    """Runs ``before`` ahead of every query FakeProm answers (inside the tick)."""

    def __init__(self, prom: FakeProm, before: Callable[[], Awaitable[None]]) -> None:
        self._inner = prom.client()._client._transport  # the FakeProm handler
        self._before = before

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._before()
        return await self._inner.handle_async_request(request)


def gated(w: SW, before: Callable[[], Awaitable[None]]) -> AlertEvaluator:
    transport = Gate(w.prom, before)
    return AlertEvaluator(
        session_factory=make_session_factory(w.engine),
        prometheus=lambda: PrometheusClient(
            httpx.AsyncClient(transport=transport, base_url="http://prom.test")
        ),
        hub=EventHub(),
        clock=w.clock,
        poll_interval=lambda type_id: 60.0,
        instance_state=w.states.get,
    )


def system_firing_rows(w: SW) -> list[AlertNotification]:
    ids = {e.id for e in w.systems()}
    return [r for r in w.rows() if r.event_id in ids and r.kind == K.FIRING]


async def test_hung_tick_raises_one_system_alert_and_one_restart(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    block = asyncio.Event()

    async def hang() -> None:
        await block.wait()

    ev = gated(w, hang)
    restarts = 0

    async def restart() -> None:
        nonlocal restarts
        restarts += 1
        await ev.restart()

    dog = Watchdog(ev.monitor, restart)
    ev.start()
    try:
        await asyncio.sleep(0.01)  # the first tick hangs on its probe
        await dog.check()
        assert restarts == 0 and w.systems() == []
        w.clock.now += timedelta(seconds=91)
        await dog.check()
        assert restarts == 1
        assert ev.monitor.last_tick is None  # the cancelled tick didn't count as finished
        assert ev.monitor.health() == Health.EVALUATOR_STALLED
        for _ in range(6):  # the restarted tick hangs too: no second restart, no flapping
            await asyncio.sleep(0.01)
            w.clock.now += timedelta(seconds=100)
            await dog.check()
        assert restarts == 1
        (system,) = w.systems()
        assert (system.system_kind, system.state) == ("evaluator_stalled", AlertState.FIRING)
        assert len(system_firing_rows(w)) == 1
        assert system_firing_rows(w)[0].text.startswith("SYSTEM ALERT: Alert evaluation stopped")

        block.set()  # it answers again: the restarted tick finishes
        for _ in range(200):
            if ev.monitor.last_tick is not None:
                break
            await asyncio.sleep(0.01)
        await dog.check()
        assert w.systems()[0].state == AlertState.RESOLVED
        assert ev.monitor.health() is None
    finally:
        await ev.stop()


async def test_slow_tick_that_keeps_progressing_is_not_a_stall(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    w.value(90)
    restarts = 0
    dog: Watchdog | None = None

    async def slow() -> None:  # every query takes 40 s; the watchdog looks in between
        w.clock.now += timedelta(seconds=40)
        assert dog is not None
        await dog.check()

    ev = gated(w, slow)

    async def restart() -> None:
        nonlocal restarts
        restarts += 1

    dog = Watchdog(ev.monitor, restart)
    start = w.clock.now
    ev.monitor.evaluator_started(start)
    await ev.tick()
    assert (w.clock.now - start).total_seconds() >= 150
    assert restarts == 0 and w.systems() == []
    assert w.alert().state == AlertState.FIRING  # and the tick did its work


async def test_leftover_stall_alert_waits_for_a_finished_tick(
    engine: Engine, db: Session, prom: FakeProm
) -> None:
    w = SW(engine, db, prom)
    monitor = w.ev.monitor
    assert monitor.open_alert(SystemKind.EVALUATOR_STALLED, "before", since=w.clock.now)
    dog = Watchdog(monitor, w.ev.restart)  # as after a backend restart
    monitor.evaluator_started(w.clock.now)
    await dog.check()
    assert w.systems()[0].state == AlertState.FIRING  # no tick has finished yet
    await w.tick(10)
    await dog.check()
    assert w.systems()[0].state == AlertState.RESOLVED


def test_healthz_counts_stuck_notifications_at_most_every_5_seconds(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def counting(db: Session, now: object = None) -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(selfcheck, "stuck_count", counting)
    clock = Clock()
    monitor = SelfMonitor(session_factory=make_session_factory(engine), hub=EventHub(), clock=clock)
    for _ in range(50):
        assert monitor.health() is None
    assert calls == 1
    clock.now += timedelta(seconds=6)
    monitor.health()
    assert calls == 2


# --- /healthz ----------------------------------------------------------------------------


def test_healthz_reasons(engine: Engine, db_url: str) -> None:
    client = make_client(db_url)
    monitor: SelfMonitor = client.app.state.self_monitor  # type: ignore[attr-defined]
    monitor.stuck_cache_seconds = 0  # cached for 5 s in production
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
    monitor.key_mismatch = True  # P0-6: first in the order
    check("key_mismatch")
    monitor.key_mismatch = False
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

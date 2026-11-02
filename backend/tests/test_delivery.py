"""Durable alert notification delivery (P0-5): the outbox and its dispatcher.

Telegram is faked at the socket layer (``FakeNetwork``) under the real SSRF guard, as in
``test_channels``; the clock is fake, so retries are checked without real sleeps. The bot
token is built at runtime from obviously fake parts.
"""

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, select, update
from sqlalchemy.orm import Session

from app.alerts import channels as ch
from app.alerts import delivery
from app.alerts.delivery import DeliveryCollector, DeliveryDispatcher, enqueue
from app.alerts.evaluator import AlertEvaluator
from app.api.alerts import _deliveries
from app.api.deps import CSRF_HEADER
from app.collectors.hub import EventHub
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.models import (
    AlertEvent,
    AlertNotification,
    AlertRule,
    AlertState,
    Channel,
    ChannelType,
    NotificationKind,
    NotificationStatus,
)
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import FakeNetwork, FakeRequest
from tests.test_alerts import FakeProm
from tests.test_auth import do_setup, make_client
from tests.test_channels import ADDRESS, factory_for, public_test_net, telegram

__all__ = ["public_test_net"]  # the autouse fixture: api.telegram.org on TEST-NET-1

API = "/api/v1"
TOKEN = ":".join(("1" * 9, "example-token-" + "0" * 20))
CHAT_A = "-100" + "1" * 10
CHAT_B = "-100" + "2" * 10
FINAL = {
    NotificationStatus.DELIVERED,
    NotificationStatus.FAILED,
    NotificationStatus.FOLDED,
    NotificationStatus.CANCELLED,
}
S = NotificationStatus


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeTelegram:
    """Answers sendMessage per chat; records each delivered text."""

    def __init__(self) -> None:
        self.down: set[str] = set()
        self.answers: dict[str, list[bytes]] = {}
        self.texts: dict[str, list[str]] = {}
        self.calls = 0
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def wait_for_calls(self, n: int) -> None:
        while self.calls < n:
            self.entered.clear()
            await asyncio.wait_for(self.entered.wait(), 5)

    async def __call__(self, request: FakeRequest) -> bytes:
        self.calls += 1
        self.entered.set()
        payload = json.loads(request.body)
        chat = payload["chat_id"]
        if self.hold is not None:
            self.entered.set()
            await self.hold.wait()
        queued = self.answers.get(chat)
        if queued:
            return queued.pop(0)
        if chat in self.down:
            return telegram(502, {"ok": False})
        self.texts.setdefault(chat, []).append(payload["text"])
        return telegram()


@pytest.fixture
def network() -> FakeNetwork:
    return FakeNetwork()


@pytest.fixture
def tg(network: FakeNetwork) -> FakeTelegram:
    fake = FakeTelegram()
    network.serve(ADDRESS, 443, fake)
    return fake


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


def add_channel(db: Session, chat: str = CHAT_A, token: str = TOKEN) -> int:
    row = Channel(type=ChannelType.TELEGRAM, name=f"chat {chat[-2:]}", config={"chat_id": chat})
    db.add(row)
    db.flush()
    row.secrets = ch.encrypt(SecretBox(TEST_MASTER_KEY), row.id, {"bot_token": token})
    db.commit()
    return row.id


def queue(
    db: Session,
    clock: Clock,
    channel_ids: list[int],
    text: str,
    kind: NotificationKind = NotificationKind.FIRING,
    event_id: int | None = None,
) -> None:
    enqueue(db, channel_ids=channel_ids, kind=kind, text=text, event_id=event_id, now=clock.now)
    db.commit()


def make_dispatcher(engine: Engine, network: FakeNetwork, clock: Clock) -> DeliveryDispatcher:
    return DeliveryDispatcher(
        session_factory=make_session_factory(engine),
        secret_box=SecretBox(TEST_MASTER_KEY),
        client_factory=factory_for(network),
        clock=clock,
        poll_seconds=0.01,
    )


def rows(db: Session) -> list[AlertNotification]:
    db.expire_all()
    return list(db.scalars(select(AlertNotification).order_by(AlertNotification.id)))


def one(db: Session) -> AlertNotification:
    (row,) = rows(db)
    return row


def add_event(db: Session, summary: str = "Gateway (critical): fw") -> AlertEvent:
    rule = AlertRule(name="Gateway", expr="up == 0", severity="critical")
    db.add(rule)
    db.flush()
    event = AlertEvent(
        rule_id=rule.id,
        fingerprint=f"fp{rule.id}",
        labels={},
        rule_name=rule.name,
        severity="critical",
        state=AlertState.FIRING,
        started_at=utcnow(),
        summary=summary,
    )
    db.add(event)
    db.commit()
    return event


# --- retries -----------------------------------------------------------------------------


async def test_two_failures_then_success(
    engine: Engine,
    db: Session,
    network: FakeNetwork,
    tg: FakeTelegram,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    cid = add_channel(db)
    tg.answers[CHAT_A] = [telegram(502, {"ok": False}), telegram(504, {"ok": False})]
    queue(db, clock, [cid], "FIRING: Gateway")
    d = make_dispatcher(engine, network, clock)

    assert await d.run_once() == 1
    row = one(db)
    assert (row.status, row.attempts, row.error_kind) == (S.PENDING, 1, "http_5xx")
    assert row.next_attempt_at == clock.now + timedelta(seconds=10)
    assert await d.run_once() == 0  # not due yet
    clock.advance(10)
    await d.run_once()
    assert one(db).next_attempt_at == clock.now + timedelta(seconds=30)
    clock.advance(30)
    await d.run_once()
    row = one(db)
    assert (row.status, row.attempts, row.error_kind) == (S.DELIVERED, 3, None)
    assert row.finished_at == clock.now and tg.texts[CHAT_A] == ["FIRING: Gateway"]
    assert f"delivered: channel {cid}, rows {row.id}, attempts 3" in caplog.text
    assert caplog.text.count(f"to channel {cid} failed: http_5xx") == 2
    assert TOKEN not in caplog.text


async def test_rate_limit_waits_retry_after(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    tg.answers[CHAT_A] = [telegram(429, {"ok": False, "parameters": {"retry_after": 5}})]
    queue(db, clock, [cid], "FIRING: x")
    d = make_dispatcher(engine, network, clock)
    await d.run_once()
    row = one(db)
    assert row.error_kind == "rate_limited"
    assert row.next_attempt_at - clock.now >= timedelta(seconds=5)
    clock.advance(4)
    assert await d.run_once() == 0
    clock.advance(1)
    await d.run_once()
    assert one(db).status == S.DELIVERED


async def test_unauthorized_fails_after_one_attempt(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    tg.answers[CHAT_A] = [telegram(401, {"ok": False, "description": f"Unauthorized {TOKEN}"})]
    queue(db, clock, [cid], "FIRING: x")
    d = make_dispatcher(engine, network, clock)
    await d.run_once()
    row = one(db)
    assert (row.status, row.attempts, row.error_kind) == (S.FAILED, 1, "unauthorized")
    assert d.failed_total == 1
    clock.advance(3600)
    assert await d.run_once() == 0 and tg.calls == 1


async def test_unreadable_credentials_fail_at_once(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    channel = db.get(Channel, cid)
    assert channel is not None
    channel.secrets = b"not-a-valid-blob"
    db.commit()
    queue(db, clock, [cid], "FIRING: x")
    await make_dispatcher(engine, network, clock).run_once()
    row = one(db)
    assert (row.status, row.error_kind) == (S.FAILED, "credentials_unreadable")
    assert tg.calls == 0


async def test_always_failing_expires_at_24_hours(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    tg.down.add(CHAT_A)
    queue(db, clock, [cid], "FIRING: x")
    created = clock.now
    d = make_dispatcher(engine, network, clock)
    waits = []
    while (row := one(db)).status == S.PENDING:
        waits.append((row.next_attempt_at - clock.now).total_seconds())
        clock.now = row.next_attempt_at
        await d.run_once()
    assert waits[1:7] == [10, 30, 60, 120, 300, 600]
    assert (row.status, row.error_kind) == (S.FAILED, "expired")
    assert row.finished_at is not None and row.finished_at - created >= timedelta(hours=24)
    assert row.finished_at - created < timedelta(hours=24, minutes=10)
    assert row.attempts == tg.calls and d.failed_total == 1


# --- crash and restart -------------------------------------------------------------------


async def test_crash_between_claim_and_send_never_loses_the_message(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    queue(db, clock, [cid], "FIRING: first")
    d = make_dispatcher(engine, network, clock)
    assert len(d._claim(4)) == 1  # claimed, then the process dies before sending
    assert one(db).status == S.SENDING

    restarted = make_dispatcher(engine, network, clock)
    assert restarted.recover() == 1
    row = one(db)
    assert (row.status, row.attempts) == (S.PENDING, 1)
    await restarted.run_once()
    assert one(db).status == S.DELIVERED and tg.texts[CHAT_A] == ["FIRING: first"]

    # Sent, but the process dies before marking it: delivered twice, never zero times.
    queue(db, clock, [cid], "FIRING: second")
    crashing = make_dispatcher(engine, network, clock)

    def crash(*_args: object) -> None:
        raise RuntimeError("process killed")

    crashing._finish = crash  # type: ignore[method-assign]
    await crashing.run_once()
    again = make_dispatcher(engine, network, clock)
    again.recover()
    await again.run_once()
    assert rows(db)[1].status == S.DELIVERED
    assert tg.texts[CHAT_A] == ["FIRING: first", "FIRING: second", "FIRING: second"]


async def test_rolled_back_change_queues_nothing(engine: Engine, db: Session, clock: Clock) -> None:
    cid = add_channel(db)
    rule = AlertRule(name="r", expr="up == 0", severity="warning")
    db.add(rule)
    db.flush()
    event = AlertEvent(
        rule_id=rule.id,
        fingerprint="f",
        labels={},
        rule_name="r",
        severity="warning",
        state=AlertState.FIRING,
        started_at=clock.now,
    )
    db.add(event)
    db.flush()
    enqueue(db, channel_ids=[cid], kind=NotificationKind.FIRING, text="x", event_id=event.id)
    db.rollback()
    assert rows(db) == [] and db.scalar(select(func.count()).select_from(AlertEvent)) == 0


# --- order, batching, overflow -----------------------------------------------------------


async def test_firing_then_resolved_arrive_in_order(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    event = add_event(db)
    tg.down.add(CHAT_A)
    d = make_dispatcher(engine, network, clock)
    queue(db, clock, [cid], "FIRING: fw", event_id=event.id)
    await d.run_once()
    clock.advance(1)
    queue(db, clock, [cid], "RESOLVED: fw", NotificationKind.RESOLVED, event.id)
    assert await d.run_once() == 0  # the head waits, so the RESOLVED can't go first
    tg.down.clear()
    clock.advance(10)
    await d.run_once()
    assert tg.texts[CHAT_A] == ["FIRING: fw\nRESOLVED: fw"]
    firing, resolved = rows(db)
    assert firing.status == S.DELIVERED
    assert (resolved.status, resolved.folded_into) == (S.FOLDED, firing.id)


async def test_batch_of_25_is_one_message(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    for i in range(25):
        queue(db, clock, [cid], f"FIRING: device {i}")
    d = make_dispatcher(engine, network, clock)
    assert await d.run_once() == 1
    (text,) = tg.texts[CHAT_A]
    lines = text.split("\n")
    assert lines[:20] == [f"FIRING: device {i}" for i in range(20)]
    assert lines[20:] == ["and 5 more, see Alerts"]
    head, *rest = rows(db)
    assert head.status == S.DELIVERED
    assert {(r.status, r.folded_into) for r in rest} == {(S.FOLDED, head.id)}


def overflow(db: Session, clock: Clock, cid: int, n: int = 600) -> AlertNotification:
    """``n`` alert changes for one channel (alternating firing/resolved, each with its own
    event); returns the overflow summary row."""
    for i in range(n):
        kind = NotificationKind.FIRING if i % 2 == 0 else NotificationKind.RESOLVED
        ev = add_event(db, f"device {i}") if i >= OVERFLOW else None
        enqueue(
            db,
            channel_ids=[cid],
            kind=kind,
            text=f"{kind.value} {i}",
            event_id=ev.id if ev else None,
            now=clock.now,
        )
    db.commit()
    (summary,) = [r for r in rows(db) if r.kind == NotificationKind.SUMMARY]
    return summary


OVERFLOW = delivery.OVERFLOW_LIMIT


async def test_overflow_folds_into_one_summary(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    summary = overflow(db, clock, cid)
    assert summary.status == S.PENDING
    assert summary.text == (
        "100 more alert changes while Telegram was unreachable: 50 firing, 50 resolved"
    )
    all_rows = rows(db)
    carried = [r for r in all_rows if r.folded_into == summary.id]
    assert len(carried) == 100
    # Not final yet: nothing has been sent.
    assert all(r.status == S.FOLDED and r.finished_at is None for r in carried)
    assert sum(r.status == S.PENDING for r in all_rows) == 501  # 500 + the summary

    await make_dispatcher(engine, network, clock).run_once()
    final = rows(db)
    assert all(r.status in FINAL and r.finished_at is not None for r in final)
    assert sum(r.folded_into == summary.id for r in final) == 100
    (text,) = tg.texts[CHAT_A]
    # 501 rows, 20 shown: 480 more rows plus the 100 changes the summary stands for.
    assert text.endswith("and 580 more, see Alerts")


async def test_overflow_rows_fail_with_their_summary(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    summary = overflow(db, clock, cid)
    event_ids = [r.event_id for r in rows(db) if r.folded_into == summary.id]
    d = make_dispatcher(engine, network, clock)
    tg.down.add(CHAT_A)
    clock.advance(25 * 3600)
    await d.run_once()
    db.expire_all()
    got = _deliveries(db, [e for e in event_ids if e is not None])
    assert len(got) == 100
    for entries in got.values():
        (entry,) = entries
        assert (entry.status, entry.error_kind) == (S.FAILED, "expired")
        assert entry.finished_at is not None
    assert d.failed_total == 601  # 500 + the summary + its 100 rows


async def test_summary_claimed_mid_fold_starts_a_new_summary(
    engine: Engine, db: Session, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = add_channel(db)
    first = overflow(db, clock, cid, OVERFLOW + 1)
    original = delivery._pending_summary
    calls = 0

    def claimed_meanwhile(session: Session, channel_id: int) -> AlertNotification | None:
        nonlocal calls
        calls += 1
        found = original(session, channel_id)
        if calls == 1 and found is not None:
            # The dispatcher claims it between this select and the text update.
            session.execute(
                update(AlertNotification)
                .where(AlertNotification.id == found.id)
                .values(status=S.SENDING)
                .execution_options(synchronize_session=False)
            )
        return found

    monkeypatch.setattr(delivery, "_pending_summary", claimed_meanwhile)
    queue(db, clock, [cid], "resolved late", NotificationKind.RESOLVED)
    summaries = [r for r in rows(db) if r.kind == NotificationKind.SUMMARY]
    old, new = summaries
    assert old.id == first.id
    assert old.text.startswith("1 more alert change while")  # unchanged
    assert new.text == "1 more alert change while Telegram was unreachable: 1 resolved"
    late = [r for r in rows(db) if r.text == "resolved late"]
    assert [r.folded_into for r in late] == [new.id]


async def test_retry_after_zero_waits_at_least_a_second(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    tg.answers[CHAT_A] = [telegram(429, {"ok": False, "parameters": {"retry_after": 0}})]
    queue(db, clock, [cid], "FIRING: x")
    await make_dispatcher(engine, network, clock).run_once()
    assert one(db).next_attempt_at - clock.now == timedelta(seconds=1)


async def test_idle_pass_does_not_write(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    writes: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        if not statement.lstrip().upper().startswith("SELECT"):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert await make_dispatcher(engine, network, clock).run_once() == 0
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert [w for w in writes if w.lstrip().upper().startswith(("UPDATE", "INSERT"))] == []


async def test_stop_cancels_a_send_and_the_next_start_recovers_it(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    queue(db, clock, [cid], "FIRING: x")
    tg.hold = asyncio.Event()  # Telegram hangs
    d = make_dispatcher(engine, network, clock)
    d.start()
    await tg.wait_for_calls(1)
    await d.stop()
    assert one(db).status == S.SENDING
    tg.hold = None
    again = make_dispatcher(engine, network, clock)
    again.start()  # recovers the row, then sends it
    try:
        await tg.wait_for_calls(2)
        for _ in range(50):
            if one(db).status == S.DELIVERED:
                break
            await asyncio.sleep(0.01)
    finally:
        await again.stop()
    row = one(db)
    assert (row.status, row.attempts) == (S.DELIVERED, 2)


# --- isolation ---------------------------------------------------------------------------


async def test_one_channel_down_does_not_block_another(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    a = add_channel(db, CHAT_A)
    b = add_channel(db, CHAT_B)
    tg.down.add(CHAT_A)
    queue(db, clock, [a, b], "FIRING: fw")
    await make_dispatcher(engine, network, clock).run_once()
    by_channel = {r.channel_id: r for r in rows(db)}
    assert by_channel[a].status == S.PENDING and by_channel[b].status == S.DELIVERED
    assert tg.texts == {CHAT_B: ["FIRING: fw"]}


async def test_slow_channel_does_not_delay_the_evaluator(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    cid = add_channel(db)
    db.add(AlertRule(name="Gateway", expr="up == 0", severity="critical", channel_ids=[cid]))
    db.commit()
    queue(db, clock, [cid], "FIRING: earlier")
    tg.hold = asyncio.Event()  # Telegram never answers until released
    d = make_dispatcher(engine, network, clock)
    d.start()
    try:
        await asyncio.wait_for(tg.entered.wait(), 5)
        prom = FakeProm()
        prom.vector = [{"vantage_instance": "fw"}]
        ev = AlertEvaluator(
            session_factory=make_session_factory(engine),
            prometheus=prom.client,
            hub=EventHub(),
            clock=clock,
        )
        await asyncio.wait_for(ev.tick(), 5)  # returns while the send still hangs
        statuses = [(r.text, r.status) for r in rows(db)]
        assert statuses == [
            ("FIRING: earlier", S.SENDING),
            ("FIRING: Gateway (critical): fw", S.PENDING),
        ]
    finally:
        tg.hold.set()
        await d.stop()


async def test_at_most_four_sends_in_flight(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    ids = [add_channel(db, f"-100{i:010d}") for i in range(6)]
    for cid in ids:
        queue(db, clock, [cid], "FIRING: x")
    tg.hold = asyncio.Event()
    d = make_dispatcher(engine, network, clock)
    d.start()
    try:
        await tg.wait_for_calls(4)
        for _ in range(5):  # a few more passes of the loop: no fifth send starts
            d.wake()
            await asyncio.sleep(0)
        assert tg.calls == 4 and len(d._in_flight) == 4
        assert sum(r.status == S.SENDING for r in rows(db)) == 4
        tg.hold.set()
        await tg.wait_for_calls(6)
    finally:
        tg.hold.set()
        await d.stop()


# --- the API, channels and secrets -------------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        # Only this test's dispatcher (fake Telegram) may send.
        c.portal.call(c.app.state.delivery.stop)  # type: ignore[attr-defined,union-attr]
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        yield c


def create_channel(client: TestClient, chat: str = CHAT_A) -> int:
    body = {"type": "telegram", "name": "ops", "config": {"chat_id": chat, "bot_token": TOKEN}}
    r = client.post(f"{API}/channels", json=body)
    assert r.status_code == 201
    cid: int = r.json()["id"]
    return cid


async def test_deliveries_show_on_events_without_secrets(
    admin: TestClient,
    engine: Engine,
    db: Session,
    network: FakeNetwork,
    tg: FakeTelegram,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    cid = create_channel(admin)
    event = add_event(db)
    tg.answers[CHAT_A] = [telegram(401, {"ok": False, "description": f"/bot{TOKEN}/x {TOKEN}"})]
    queue(db, clock, [cid], "FIRING: fw", event_id=event.id)
    await make_dispatcher(engine, network, clock).run_once()

    r = admin.get(f"{API}/alerts/events")
    (got,) = r.json()
    (entry,) = got["deliveries"]
    assert entry["channel_id"] == cid and entry["channel_name"] == "ops"
    assert (entry["kind"], entry["status"]) == ("firing", "failed")
    assert (entry["attempts"], entry["error_kind"]) == (1, "unauthorized")
    assert entry["finished_at"] is not None
    stored = one(db)
    for text in (r.text, caplog.text, stored.error_kind or "", stored.text):
        assert TOKEN not in text and "example-token" not in text


async def test_banner_counts_stuck_messages(
    admin: TestClient, engine: Engine, db: Session, clock: Clock
) -> None:
    cid = create_channel(admin)
    queue(db, clock, [cid], "FIRING: fw")
    assert admin.get(f"{API}/alerts/delivery-status").json()["stuck"] == 0
    row = one(db)
    row.created_at = utcnow() - timedelta(minutes=16)
    db.commit()
    assert admin.get(f"{API}/alerts/delivery-status").json()["stuck"] == 1
    # A failed row counts at once, for 24 h after it failed.
    queue(db, clock, [cid], "FIRING: other")
    failed = rows(db)[1]
    failed.status, failed.finished_at = S.FAILED, utcnow()
    db.commit()
    assert admin.get(f"{API}/alerts/delivery-status").json()["stuck"] == 2
    failed.finished_at = utcnow() - timedelta(hours=25)
    db.commit()
    assert admin.get(f"{API}/alerts/delivery-status").json()["stuck"] == 1
    admin.cookies.clear()
    assert admin.get(f"{API}/alerts/delivery-status").status_code == 401


async def test_deleted_channel_cancels_its_pending_rows(
    admin: TestClient, engine: Engine, db: Session, network: FakeNetwork, clock: Clock
) -> None:
    cid = create_channel(admin)
    queue(db, clock, [cid], "FIRING: a")
    queue(db, clock, [cid], "RESOLVED: a", NotificationKind.RESOLVED)
    assert admin.delete(f"{API}/channels/{cid}").status_code == 204
    final = rows(db)
    assert {(r.status, r.channel_id) for r in final} == {(S.CANCELLED, None)}
    assert all(r.finished_at is not None for r in final)
    assert await make_dispatcher(engine, network, clock).run_once() == 0


def test_metrics_report_pending_and_failed(engine: Engine, db: Session, clock: Clock) -> None:
    cid = add_channel(db)
    queue(db, clock, [cid], "FIRING: x")
    d = DeliveryDispatcher(
        session_factory=make_session_factory(engine), secret_box=SecretBox(TEST_MASTER_KEY)
    )
    d.failed_total = 2
    got = {m.name: m.samples[0].value for m in DeliveryCollector(d).collect()}
    assert got == {
        "vantage_alert_notifications_pending": 1.0,
        "vantage_alert_notifications_failed": 2.0,
    }


def test_backoff_schedule() -> None:
    got = [delivery.backoff_seconds(n) for n in range(1, 9)]
    assert got == [10, 30, 60, 120, 300, 600, 600, 600]


def test_overflow_summary_counts_no_data(db: Session, clock: Clock) -> None:
    cid = add_channel(db)
    overflow(db, clock, cid)
    for i in range(3):
        enqueue(
            db,
            channel_ids=[cid],
            kind=NotificationKind.NO_DATA,
            text=f"NO DATA: device {i}",
            now=clock.now,
        )
    db.commit()
    (summary,) = [r for r in rows(db) if r.kind == NotificationKind.SUMMARY]
    assert summary.text == (
        "103 more alert changes while Telegram was unreachable: 50 firing, 3 no data, 50 resolved"
    )


async def test_row_folded_into_a_queued_folded_carrier_settles_with_the_summary(
    engine: Engine, db: Session, network: FakeNetwork, tg: FakeTelegram, clock: Clock
) -> None:
    """A system alert's FIRING row stored folded into an overflow summary carries a later
    NO DATA row: it settles when the summary does, never staying unfinished."""
    cid = add_channel(db)
    summary = overflow(db, clock, cid, n=OVERFLOW + 1)
    queue(db, clock, [cid], "SYSTEM ALERT: Prometheus unreachable")
    firing = rows(db)[-1]
    assert (firing.status, firing.folded_into) == (S.FOLDED, summary.id)
    # Some of the backlog went out meanwhile: the NO DATA row is queued normally ...
    early = [r.id for r in rows(db)[:10]]
    db.execute(
        update(AlertNotification)
        .where(AlertNotification.id.in_(early))
        .values(status=S.DELIVERED, finished_at=clock.now)
    )
    queue(db, clock, [cid], "NO DATA: fw", kind=NotificationKind.NO_DATA)
    no_data = rows(db)[-1]
    assert no_data.status == S.PENDING
    delivery.fold_into(db, no_data, firing, clock.now)  # ... and folded, as the evaluator does
    db.commit()
    assert (no_data.status, no_data.folded_into) == (S.FOLDED, summary.id)

    await make_dispatcher(engine, network, clock).run_once()
    db.expire_all()
    assert all(r.status in FINAL and r.finished_at is not None for r in rows(db))
    assert db.get(AlertNotification, no_data.id).status == S.FOLDED  # type: ignore[union-attr]

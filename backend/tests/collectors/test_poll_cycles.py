"""Poll failure tolerance (P0-7): cycles, the one retry, down after N failed cycles, and
seeding last_success_at from Prometheus. A fake clock and sleep: no real waiting."""

import asyncio
import json
import logging
import uuid
from dataclasses import replace
from typing import Any, ClassVar

import httpx
import pytest
from prometheus_client import generate_latest
from sqlalchemy import Engine

from app.collectors.scheduler import PollScheduler, PollSpec
from app.collectors.seed import seed_last_success
from app.collectors.store import MetricsStore, make_registry
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.core.net_guard import NetGuard, NetGuardError, NetPolicy
from app.integrations import instance_config as ic
from app.integrations.base import InstanceContext, Integration, Sample
from app.integrations.errors import PollFailure
from app.integrations.registry import Registry
from app.query.proxy import PrometheusClient
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.fake import FakeIntegration

INTERVAL = 60


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


CLOCK = FakeClock()


class Scripted(FakeIntegration):
    """Each attempt takes the next step of ``script``: ``(seconds it takes, outcome)``,
    where the outcome is ``"ok"``, ``"hang"`` (outlives the attempt's cut-off) or an
    exception to raise. Records the clock time at which every attempt started."""

    type_id = "scripted"
    script: ClassVar[list[tuple[float, Any]]] = []
    starts: ClassVar[list[float]] = []

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        Scripted.starts.append(CLOCK.now)
        seconds, outcome = Scripted.script.pop(0) if Scripted.script else (0.0, "ok")
        CLOCK.now += seconds
        if outcome == "hang":
            await asyncio.sleep(10)  # cut off by the (tiny, real) whole-attempt timeout
        if isinstance(outcome, BaseException):
            raise outcome
        return [Sample(name="vantage_scripted_x", value=1, type="gauge")]

    def poll_interval_seconds(self) -> int:
        return INTERVAL


def fake_guards(cls: type[Integration]) -> NetGuard:
    return NetGuard(NetPolicy(own_addresses=frozenset()), cloud_hosts=cls.cloud_hosts)


@pytest.fixture
def store() -> MetricsStore:
    return MetricsStore(down_after_failed_cycles=3)


@pytest.fixture
def scheduler(engine: Engine, store: MetricsStore) -> PollScheduler:
    CLOCK.now = 0.0
    Scripted.script = []
    Scripted.starts = []
    return PollScheduler(
        session_factory=make_session_factory(engine),
        registry=Registry([Scripted]),
        secret_box=SecretBox(TEST_MASTER_KEY),
        net_guard_factory=fake_guards,
        store=store,
        max_poll_timeout_seconds=0.01,  # real seconds: only "hang" ever reaches it
        clock=CLOCK,
        sleep=CLOCK.sleep,
    )


@pytest.fixture
def spec(store: MetricsStore) -> PollSpec:
    instance_id = uuid.uuid4()
    made = PollSpec(
        instance_id=instance_id,
        type_id="scripted",
        name="sw-1",
        site="lab",
        updated_at=utcnow(),
        config={"host": "10.0.0.10", "port": 443},
        secrets=ic.encrypt(SecretBox(TEST_MASTER_KEY), instance_id, {"api_token": "example-token"}),
    )
    store.track(made.instance_id, made.labels)
    return made


def exposition(store: MetricsStore) -> str:
    return generate_latest(make_registry(store)).decode()


def metric(store: MetricsStore, name: str) -> float | None:
    for line in exposition(store).splitlines():
        if line.startswith(name + "{"):
            return float(line.rsplit(" ", 1)[1])
    return None


async def cycle(scheduler: PollScheduler, spec: PollSpec) -> None:
    """One cycle starting now, as the poll loop runs it."""
    await scheduler.poll_once(spec, Scripted(), next_start=CLOCK.now + INTERVAL)


def refused() -> Exception:
    return PollFailure("connection refused by 10.0.0.10")


async def test_failed_cycles_below_n_never_go_down(
    scheduler: PollScheduler, spec: PollSpec, store: MetricsStore
) -> None:
    Scripted.script = [(0, "ok")]
    await cycle(scheduler, spec)
    for failed in (1, 2):
        Scripted.script = [(1, refused()), (1, refused())]  # attempt and retry fail
        await cycle(scheduler, spec)
        status = store.status(spec.instance_id)
        assert status is not None and status.state == "up"
        assert status.error == "connection refused by 10.0.0.10"  # shown, while still up
        assert metric(store, "vantage_instance_up") == 1.0  # never an up = 0 sample
        assert metric(store, "vantage_poll_consecutive_failed_cycles") == failed
        assert "vantage_scripted_x{" not in exposition(store)  # samples dropped at once
    Scripted.script = [(0, "ok")]
    await cycle(scheduler, spec)
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "up" and status.error is None
    assert metric(store, "vantage_poll_consecutive_failed_cycles") == 0


async def test_down_at_the_end_of_the_nth_failed_cycle(
    scheduler: PollScheduler, spec: PollSpec, store: MetricsStore
) -> None:
    for n in (1, 2, 3):
        Scripted.script = [(1, refused()), (1, refused())]
        await cycle(scheduler, spec)
        status = store.status(spec.instance_id)
        assert status is not None
        # Never answered: pending (no up/down sample) until down.
        assert status.state == ("down" if n == 3 else "pending")
        assert status.error == "connection refused by 10.0.0.10"
    assert metric(store, "vantage_instance_up") == 0.0
    # Two attempts per cycle, but the gauge counts cycles.
    assert len(Scripted.starts) == 6
    assert metric(store, "vantage_poll_consecutive_failed_cycles") == 3.0
    Scripted.script = [(0, "ok")]
    await cycle(scheduler, spec)  # one good cycle: up again
    assert getattr(store.status(spec.instance_id), "state", "") == "up"


async def test_retry_follows_a_fast_failure_and_can_save_the_cycle(
    scheduler: PollScheduler, spec: PollSpec, store: MetricsStore
) -> None:
    Scripted.script = [(2, httpx.ConnectTimeout("no reply")), (1, "ok")]
    await cycle(scheduler, spec)
    assert Scripted.starts == [0.0, 12.0]  # 10 s after the failed attempt ended
    assert getattr(store.status(spec.instance_id), "state", "") == "up"
    assert metric(store, "vantage_poll_consecutive_failed_cycles") == 0


@pytest.mark.parametrize(
    "outcome",
    ["hang", NetGuardError("address 127.0.0.1 is not allowed")],
    ids=["whole_attempt_timeout", "net_guard_refusal"],
)
async def test_no_retry_after_a_hang_or_a_guard_refusal(
    scheduler: PollScheduler, spec: PollSpec, store: MetricsStore, outcome: Any
) -> None:
    Scripted.script = [(0, outcome), (0, "ok")]
    await cycle(scheduler, spec)
    assert len(Scripted.starts) == 1
    assert metric(store, "vantage_poll_consecutive_failed_cycles") == 1


async def test_no_retry_after_an_unreadable_stored_config(
    scheduler: PollScheduler, spec: PollSpec, store: MetricsStore
) -> None:
    broken = replace(spec, config={"port": "not-a-port"})
    await scheduler.poll_once(broken, Scripted(), next_start=CLOCK.now + INTERVAL)
    assert CLOCK.now == 0.0  # no 10 s wait for a retry
    assert getattr(store.status(spec.instance_id), "error", "") == (
        "stored config is not valid for this integration"
    )


async def test_no_retry_with_less_than_20_s_left(scheduler: PollScheduler, spec: PollSpec) -> None:
    Scripted.script = [(41, refused()), (0, "ok")]  # 19 s left when it fails
    await cycle(scheduler, spec)
    assert Scripted.starts == [0.0]
    Scripted.script = [(40, refused()), (0, "ok")]  # exactly 20 s left: retried
    start = CLOCK.now
    await cycle(scheduler, spec)
    assert Scripted.starts[1:] == [start, start + 50]


async def test_cycles_stay_on_schedule_after_retries(
    scheduler: PollScheduler, spec: PollSpec, store: MetricsStore
) -> None:
    Scripted.script = [
        (5, refused()), (30, refused()),  # cycle 1: attempt at 0, retry at 15
        (0, "ok"),  # cycle 2 at 60
        (5, refused()), (3, "ok"),  # cycle 3 at 120, retry at 135
        (0, "ok"),  # cycle 4 at 180
    ]  # fmt: skip
    task = asyncio.create_task(scheduler._run(spec))
    try:
        async with asyncio.timeout(5):
            while len(Scripted.starts) < 6:  # noqa: ASYNC110 (driven by the fake clock)
                await asyncio.sleep(0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert Scripted.starts == [0.0, 15.0, 60.0, 120.0, 135.0, 180.0]


async def test_seeding_fills_last_success_at(store: MetricsStore) -> None:
    tracked, later = uuid.uuid4(), uuid.uuid4()
    store.track(tracked, {})
    seen: list[str] = []

    def prom(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        result = [
            {"metric": {"vantage_instance_id": str(i)}, "value": [1767225600, ts]}
            for i, ts in ((tracked, "1767225000"), (later, "1767225300"))
        ]
        body = {"status": "success", "data": {"resultType": "vector", "result": result}}
        return httpx.Response(200, content=json.dumps(body))

    client = PrometheusClient(
        httpx.AsyncClient(base_url="http://10.0.0.20:9090", transport=httpx.MockTransport(prom))
    )
    assert await seed_last_success(client, store, "30d") == 2
    assert "max_over_time" in seen[0] and "30d" in seen[0]
    status = store.status(tracked)
    assert status is not None and status.state == "pending"  # only the time is filled
    assert status.last_success_at is not None
    assert status.last_success_at.timestamp() == 1767225000
    assert "vantage_instance_up{" not in exposition(store)
    store.track(later, {})  # tracked after seeding: gets its value then
    assert getattr(store.status(later), "last_success_at", None) is not None


async def test_seeding_failure_leaves_it_empty(
    store: MetricsStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.track(uuid.uuid4(), {})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def hangs(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        raise AssertionError("not reached")

    with caplog.at_level(logging.INFO):
        for handler, timeout in ((down, 5.0), (hangs, 0.01)):
            client = PrometheusClient(
                httpx.AsyncClient(
                    base_url="http://10.0.0.20:9090", transport=httpx.MockTransport(handler)
                )
            )
            assert await seed_last_success(client, store, "30d", timeout_seconds=timeout) == 0
    assert all(s.last_success_at is None for s in store.statuses().values())
    assert caplog.text.count("not seeded") == 2

"""Poll scheduler (M2.4): per-instance polling, isolation of failures, secret hygiene."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from typing import Any, ClassVar

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.scheduler import (
    MAX_FAILURE_TEXT,
    MAX_POLL_INTERVAL_SECONDS,
    PollScheduler,
    polls,
)
from app.collectors.store import MetricsStore, make_registry
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.core.net_guard import NetGuard, NetGuardError, NetPolicy, PingError
from app.integrations import instance_config as ic
from app.integrations.base import InstanceContext, Integration, Sample
from app.integrations.errors import PollFailure
from app.integrations.registry import Registry
from app.models import Instance
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.fake import FakeConfig, FakeIntegration

TOKEN = "example-token-poll-3e9d1a"
INTERVAL = 0.05


async def fake_resolver(host: str, port: int) -> Sequence[str]:
    return ["10.0.0.10"]


def fake_guards(cls: type[Integration]) -> NetGuard:
    return NetGuard(
        NetPolicy(own_addresses=frozenset()), cloud_hosts=cls.cloud_hosts, resolver=fake_resolver
    )


class Poller(FakeIntegration):
    """Polls successfully, recording every context it gets."""

    type_id = "poller"
    seen: ClassVar[list[InstanceContext]] = []

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        Poller.seen.append(ctx)
        return [Sample(name="vantage_poller_temp", value=40.0, type="gauge", labels={"s": "cpu"})]

    def poll_interval_seconds(self) -> int:
        return 0  # clamped up to the scheduler's minimum


class Raiser(FakeIntegration):
    type_id = "raiser"

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        token = ctx.config_as(FakeConfig).api_token.get_secret_value()
        raise RuntimeError(f"login failed for token {token}")


class Hanger(FakeIntegration):
    type_id = "hanger"

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        await asyncio.sleep(60)
        return []

    def poll_interval_seconds(self) -> int:
        return 0  # the poll is cut off after min(interval, 60 s)


class HttpTimeout(FakeIntegration):
    type_id = "http_timeout"

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        token = ctx.config_as(FakeConfig).api_token.get_secret_value()
        raise httpx.ReadTimeout(f"timed out at https://10.0.0.10/?token={token}")


class Guarded(FakeIntegration):
    type_id = "guarded"

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        await ctx.net_guard.resolve("127.0.0.1", 443)
        return []


class PingFails(FakeIntegration):
    type_id = "ping_fails"

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        # What the guarded ping client raises for a broadcast address.
        raise PingError("the system refused to ping 10.0.0.255 (is it a broadcast address?)")


class Unanswered(PollFailure):
    pass


class PollFails(FakeIntegration):
    """Raises a PollFailure: its message, written to be shown, becomes the status."""

    type_id = "poll_fails"

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        message = {"long": "x" * 1000, "empty": ""}.get(ctx.name, "no answer from 10.0.0.10")
        raise Unanswered(message)


# Every way a poll can fail, each with every config and secret value in its text (or its
# return value), to check that none of them reaches the status, the log or /metrics.
SECRETS = {
    "api_token": TOKEN,
    "snmp_community": "example-community-6b1f0e",
    "client_secret": "example-client-secret-0c4e9a",
}
CONFIG = {"host": "nas-7f3c2d.example.internal", "port": 8443, "verify_tls": False}
EVERYTHING = " ".join([*SECRETS.values(), *(str(v) for v in CONFIG.values())])
BRANCHES: dict[str, Any] = {
    "other_exception": RuntimeError(EVERYTHING),
    "timeout": TimeoutError(EVERYTHING),
    "http_timeout": httpx.ReadTimeout(EVERYTHING),
    "guard": NetGuardError(f"address 127.0.0.1 is not allowed {' '.join(SECRETS.values())}"),
    "ping": PingError(f"the system refused to ping {' '.join(SECRETS.values())}"),
    "poll_failure": Unanswered(f"no answer {' '.join(SECRETS.values())}"),
}


class Blabber(FakeIntegration):
    """Fails in the branch named by the instance; garbage returns its config as text."""

    type_id = "blabber"

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        if ctx.name == "garbage":
            return EVERYTHING  # type: ignore[return-value]
        raise BRANCHES[ctx.name]


class Garbage(FakeIntegration):
    type_id = "garbage"

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        return "not a list"  # type: ignore[return-value]


class Leaky(FakeIntegration):
    """Buggy: puts its secret in a label value."""

    type_id = "leaky"

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        token = ctx.config_as(FakeConfig).api_token.get_secret_value()
        return [
            Sample(name="vantage_leaky_x", value=1, type="gauge", labels={"t": f"tok={token}"}),
            Sample(name="vantage_leaky_y", value=2, type="gauge"),
        ]


class BadInterval(Poller):
    type_id = "bad_interval"

    def poll_interval_seconds(self) -> int:
        raise ValueError("broken")


class ScrapeOnly(FakeIntegration):
    """Doesn't override poll(): never scheduled."""

    type_id = "scrape_only"
    poll = Integration.poll  # type: ignore[assignment]


class OtherConfig(BaseModel):
    url: str
    api_token: SecretStr


REGISTRY = Registry(
    [
        Poller,
        Raiser,
        Hanger,
        HttpTimeout,
        Guarded,
        PingFails,
        PollFails,
        Blabber,
        Garbage,
        Leaky,
        BadInterval,
        ScrapeOnly,
    ]
)


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(TEST_MASTER_KEY)


@pytest.fixture
def store() -> MetricsStore:
    return MetricsStore()


@pytest.fixture
def scheduler(factory: sessionmaker[Session], box: SecretBox, store: MetricsStore) -> PollScheduler:
    Poller.seen.clear()
    return PollScheduler(
        session_factory=factory,
        registry=REGISTRY,
        secret_box=box,
        net_guard_factory=fake_guards,
        store=store,
        refresh_seconds=INTERVAL,
        min_interval_seconds=INTERVAL,
    )


def add(
    factory: sessionmaker[Session],
    box: SecretBox,
    type_id: str,
    name: str,
    *,
    enabled: bool = True,
    config: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> uuid.UUID:
    instance_id = uuid.uuid4()
    with factory() as db:
        db.add(
            Instance(
                id=instance_id,
                type_id=type_id,
                name=name,
                site="lab",
                enabled=enabled,
                config=config if config is not None else {"host": "10.0.0.10", "port": 443},
                secrets=ic.encrypt(
                    box, instance_id, secrets if secrets is not None else {"api_token": TOKEN}
                ),
            )
        )
        db.commit()
    return instance_id


async def wait_for(condition: Any, limit_seconds: float = 5.0) -> None:
    async with asyncio.timeout(limit_seconds):
        while not condition():  # noqa: ASYNC110 (polling a plain condition is fine in tests)
            await asyncio.sleep(0.01)


def exposition(store: MetricsStore) -> str:
    from prometheus_client import generate_latest

    return generate_latest(make_registry(store)).decode()


def test_polls_detects_override() -> None:
    assert polls(Poller)
    assert polls(FakeIntegration)
    assert not polls(ScrapeOnly)


async def test_sync_schedules_only_enabled_pollable_instances(
    scheduler: PollScheduler, factory: sessionmaker[Session], box: SecretBox
) -> None:
    polled = add(factory, box, "poller", "a")
    add(factory, box, "poller", "disabled", enabled=False)
    add(factory, box, "scrape_only", "scraped")
    add(factory, box, "not_registered", "unknown")
    try:
        await scheduler.sync()
        assert scheduler.running() == {polled}
    finally:
        await scheduler.stop()


async def test_successful_poll_gets_context_and_standard_labels(
    scheduler: PollScheduler, factory: sessionmaker[Session], box: SecretBox, store: MetricsStore
) -> None:
    instance_id = add(factory, box, "poller", "nas-1")
    try:
        await scheduler.sync()
        await wait_for(lambda: len(Poller.seen) >= 2)  # polled repeatedly on its interval
    finally:
        await scheduler.stop()
    ctx = Poller.seen[0]
    assert ctx.instance_id == instance_id
    assert ctx.name == "nas-1"
    assert ctx.config_as(FakeConfig).api_token.get_secret_value() == TOKEN
    assert dict(ctx.labels) == {
        "vantage_instance_id": str(instance_id),
        "vantage_instance": "nas-1",
        "vantage_integration": "poller",
        "vantage_site": "lab",
    }
    status = store.status(instance_id)
    assert status is not None
    assert status.state == "up"
    assert status.error is None
    text = exposition(store)
    assert "vantage_poller_temp{" in text
    assert f'vantage_instance_id="{instance_id}"' in text


@pytest.mark.parametrize(
    ("type_id", "expected"),
    [
        ("raiser", "poll failed (RuntimeError)"),
        ("hanger", "poll timed out"),
        ("http_timeout", "poll timed out"),
        ("guarded", "refused by the network guard"),
        (
            "ping_fails",
            "ping failed: the system refused to ping 10.0.0.255 (is it a broadcast address?)",
        ),
        ("poll_fails", "no answer from 10.0.0.10"),
        ("garbage", "poll returned something other than a list of samples"),
    ],
)
async def test_failures_mark_instance_down_without_secrets(
    scheduler: PollScheduler,
    factory: sessionmaker[Session],
    box: SecretBox,
    store: MetricsStore,
    caplog: pytest.LogCaptureFixture,
    type_id: str,
    expected: str,
) -> None:
    instance_id = add(factory, box, type_id, "broken")
    with caplog.at_level(logging.DEBUG):
        try:
            await scheduler.sync()
            await wait_for(lambda: getattr(store.status(instance_id), "state", "") == "down")
        finally:
            await scheduler.stop()
    status = store.status(instance_id)
    assert status is not None
    assert status.error is not None
    assert status.error.startswith(expected)
    assert TOKEN not in status.error
    assert TOKEN not in caplog.text
    assert TOKEN not in exposition(store)
    assert "} 0.0" in exposition(store)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("long", "x" * MAX_FAILURE_TEXT), ("empty", "poll failed (Unanswered)")],
)
async def test_poll_failure_text_is_capped_or_falls_back_to_type(
    scheduler: PollScheduler,
    factory: sessionmaker[Session],
    box: SecretBox,
    store: MetricsStore,
    name: str,
    expected: str,
) -> None:
    instance_id = add(factory, box, "poll_fails", name)
    try:
        await scheduler.sync()
        await wait_for(lambda: getattr(store.status(instance_id), "state", "") == "down")
    finally:
        await scheduler.stop()
    assert getattr(store.status(instance_id), "error", None) == expected


@pytest.mark.parametrize("branch", [*BRANCHES, "garbage", "invalid_config"])
async def test_poll_status_never_contains_config_or_secret_values(
    scheduler: PollScheduler,
    factory: sessionmaker[Session],
    box: SecretBox,
    store: MetricsStore,
    caplog: pytest.LogCaptureFixture,
    branch: str,
) -> None:
    # The status shows exception text only for NetGuardError, PingError and PollFailure,
    # trusting their messages to be secret-free; check that decrypted secrets are scrubbed
    # from those and that every other branch shows no config or secret value at all.
    config: dict[str, Any] = (
        {**CONFIG, "port": "not-a-port"} if branch == "invalid_config" else CONFIG
    )
    instance_id = add(factory, box, "blabber", branch, config=config, secrets=SECRETS)
    with caplog.at_level(logging.DEBUG):
        try:
            await scheduler.sync()
            await wait_for(lambda: getattr(store.status(instance_id), "state", "") == "down")
        finally:
            await scheduler.stop()
    status = store.status(instance_id)
    assert status is not None and status.error
    shown = [status.error, caplog.text, exposition(store)]
    for value in SECRETS.values():
        assert all(value not in text for text in shown), branch
    for value in [*(str(v) for v in config.values()), "not-a-port"]:
        assert value not in status.error, branch


async def test_one_failing_instance_does_not_affect_others(
    scheduler: PollScheduler, factory: sessionmaker[Session], box: SecretBox, store: MetricsStore
) -> None:
    good = add(factory, box, "poller", "good")
    bad = [add(factory, box, t, t) for t in ("raiser", "hanger", "garbage", "guarded")]
    try:
        await scheduler.sync()
        await wait_for(lambda: all(getattr(store.status(i), "state", "") == "down" for i in bad))
        before = len(Poller.seen)
        await wait_for(lambda: len(Poller.seen) >= before + 2)  # still polling
        assert scheduler.running() == {good, *bad}
        assert all(not job.task.done() for job in scheduler._jobs.values())
    finally:
        await scheduler.stop()
    assert getattr(store.status(good), "state", "") == "up"


async def test_undecryptable_secrets_and_invalid_config(
    scheduler: PollScheduler, factory: sessionmaker[Session], box: SecretBox, store: MetricsStore
) -> None:
    wrong_key = add(factory, SecretBox(b"another-fake-key-for-tests-only!"), "poller", "wrong")
    invalid = add(factory, box, "poller", "invalid", config={"port": "not-a-port"})
    try:
        await scheduler.sync()
        await wait_for(
            lambda: all(
                getattr(store.status(i), "state", "") == "down" for i in (wrong_key, invalid)
            )
        )
    finally:
        await scheduler.stop()
    assert "can't be decrypted" in (store.status(wrong_key).error or "")  # type: ignore[union-attr]
    assert "not valid" in (store.status(invalid).error or "")  # type: ignore[union-attr]


async def test_sync_follows_edits_disables_and_deletes(
    scheduler: PollScheduler, factory: sessionmaker[Session], box: SecretBox, store: MetricsStore
) -> None:
    instance_id = add(factory, box, "poller", "before")
    try:
        await scheduler.sync()
        first_task = scheduler._jobs[instance_id].task
        await wait_for(lambda: getattr(store.status(instance_id), "state", "") == "up")

        with factory() as db:  # rename: the task restarts with the new labels
            row = db.get(Instance, instance_id)
            assert row is not None
            row.name = "after"
            row.updated_at = utcnow()
            db.commit()
        await scheduler.sync()
        assert scheduler._jobs[instance_id].task is not first_task
        await wait_for(lambda: any(c.name == "after" for c in Poller.seen))
        await wait_for(lambda: 'vantage_instance="after"' in exposition(store))

        with factory() as db:  # disable: stop polling and drop its metrics
            row = db.get(Instance, instance_id)
            assert row is not None
            row.enabled = False
            db.commit()
        await scheduler.sync()
        assert scheduler.running() == frozenset()
        assert store.status(instance_id) is None
        assert "vantage_instance_up{" not in exposition(store)
    finally:
        await scheduler.stop()


async def test_supervisor_picks_up_new_instances(
    scheduler: PollScheduler, factory: sessionmaker[Session], box: SecretBox, store: MetricsStore
) -> None:
    scheduler.start()
    try:
        instance_id = add(factory, box, "poller", "late")
        await wait_for(lambda: getattr(store.status(instance_id), "state", "") == "up")
    finally:
        await scheduler.stop()
    assert scheduler.running() == frozenset()


async def test_supervisor_survives_db_errors(
    scheduler: PollScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def broken() -> list[Any]:
        nonlocal calls
        calls += 1
        raise RuntimeError("database is down")

    monkeypatch.setattr(scheduler, "_load_specs", broken)
    scheduler.start()
    try:
        await wait_for(lambda: calls >= 2)
    finally:
        await scheduler.stop()


def test_interval_clamping(scheduler: PollScheduler) -> None:
    assert scheduler.interval_for(Poller()) == INTERVAL  # 0 -> minimum
    assert scheduler.interval_for(BadInterval()) == INTERVAL  # raising -> minimum

    class Slow(Poller):
        def poll_interval_seconds(self) -> int:
            return 10**9

    assert scheduler.interval_for(Slow()) == MAX_POLL_INTERVAL_SECONDS


async def test_secret_in_label_value_is_dropped(
    scheduler: PollScheduler, factory: sessionmaker[Session], box: SecretBox, store: MetricsStore
) -> None:
    instance_id = add(factory, box, "leaky", "leaky")
    try:
        await scheduler.sync()
        await wait_for(lambda: getattr(store.status(instance_id), "state", "") == "up")
    finally:
        await scheduler.stop()
    text = exposition(store)
    assert TOKEN not in text
    assert "vantage_leaky_x{" not in text
    assert "vantage_leaky_y{" in text


async def test_failure_to_store_results_marks_down_and_keeps_polling(
    scheduler: PollScheduler,
    factory: sessionmaker[Session],
    box: SecretBox,
    store: MetricsStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = add(factory, box, "poller", "nas-1")
    real = store.record_success
    calls = 0

    def flaky(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError(f"bad sample with {TOKEN}")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "record_success", flaky)
    try:
        await scheduler.sync()
        await wait_for(lambda: calls >= 1)
        status = store.status(instance_id)
        assert status is not None
        assert status.state == "down"
        assert status.error == "storing poll results failed (ValueError)"
        task = scheduler._jobs[instance_id].task
        await wait_for(lambda: getattr(store.status(instance_id), "state", "") == "up")
        assert scheduler._jobs[instance_id].task is task  # same task, still running
        assert not task.done()
    finally:
        await scheduler.stop()


async def test_sync_restarts_a_task_that_stopped(
    scheduler: PollScheduler, factory: sessionmaker[Session], box: SecretBox, store: MetricsStore
) -> None:
    instance_id = add(factory, box, "poller", "nas-1")
    try:
        await scheduler.sync()
        dead = scheduler._jobs[instance_id].task
        dead.cancel()  # stands in for a task that died for any reason
        await asyncio.gather(dead, return_exceptions=True)
        assert dead.done()
        await scheduler.sync()  # same fingerprint, but the task is done: restarted
        task = scheduler._jobs[instance_id].task
        assert task is not dead
        assert not task.done()
        before = len(Poller.seen)
        await wait_for(lambda: len(Poller.seen) > before)
    finally:
        await scheduler.stop()

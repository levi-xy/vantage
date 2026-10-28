"""Ping check (M2.7): schema, connection test, poll parsing, collector end to end, and the
security assertions: only the checked private address is pinged, only echo requests are
sent, no sockets in integration code.

No real network: ``fake_icmp`` keeps the real SSRF guard (and the real ping client) and
fakes only the ICMP socket layer underneath.
"""

import ast
import inspect
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from pydantic import ValidationError
from sqlalchemy import Engine

from app.collectors.scheduler import PollScheduler, PollSpec
from app.collectors.store import MetricsStore, make_registry
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.core.net_guard import (
    MAX_PING_COUNT,
    MAX_PING_REPLY_TIMEOUT,
    MAX_PING_TOTAL_TIMEOUT,
    PING_INTERVAL,
    PING_RESOLVE_TIMEOUT,
    NetGuardError,
    PingError,
)
from app.integrations import builtin
from app.integrations import instance_config as ic
from app.integrations.base import TestResult
from app.integrations.builtin.ping_check import NoReply, PingCheck, PingCheckConfig
from app.integrations.builtin.ping_check import integration as ping_module
from app.integrations.registry import Registry
from app.integrations.schema import config_json_schema
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import FakeIcmp, drop, fake_guards, make_ctx
from tests.test_auth import do_setup, make_client

HOST = "ping.example.internal"
ADDRESS = "10.0.0.30"
ICMP_HINT = {
    "protocol": "icmp",
    "port": None,
    "destination": HOST,
    "reason": (
        "The Vantage backend pings this host: allow ICMP echo requests to it and its echo "
        "replies back."
    ),
}


def config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"host": HOST, "count": 1}
    cfg.update(overrides)
    return cfg


async def run_test(**overrides: Any) -> TestResult:
    return await PingCheck().test_connection(make_ctx(PingCheck, config(**overrides)))


def assert_read_only(icmp: FakeIcmp) -> None:
    """Only ICMP echo requests, only to the checked address: nothing that changes state."""
    assert icmp.sent
    for packet in icmp.sent:
        assert (packet.type, packet.code) in {(8, 0), (128, 0)}
        assert packet.address == ADDRESS


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults() -> None:
    cfg = PingCheckConfig.model_validate({"host": " Ping.Example.Internal. "})
    assert cfg.host == "ping.example.internal"
    assert (cfg.count, cfg.timeout_seconds) == (3, 1)


@pytest.mark.parametrize(
    ("host", "stored"),
    [
        ("10.0.0.30", "10.0.0.30"),
        ("[fd00::30]", "fd00::30"),
        ("fd00::30", "fd00::30"),
        (HOST, HOST),
        ("xn--e1afmkfd.example", "xn--e1afmkfd.example"),  # IDN in its ASCII form
    ],
)
def test_schema_accepts(host: str, stored: str) -> None:
    assert PingCheckConfig.model_validate({"host": host}).host == stored


@pytest.mark.parametrize(
    "host",
    [
        "",
        "2130706433",  # ambiguous numeric
        "0x7f.1",
        "10.1",
        "http://10.0.0.30/",  # a URL is not a host
        "admin:example-password@10.0.0.30",  # no credentials in the host field
        "10.0.0.30:80",
        "fe80::1%eth0",
        "пример.example",  # Unicode IDN: the guard only takes the ASCII (punycode) form
        "a" * 254,
    ],
)
def test_schema_rejects_host(host: str) -> None:
    with pytest.raises(ValidationError) as info:
        PingCheckConfig.model_validate({"host": host})
    for error in info.value.errors():
        assert "example-password" not in error["msg"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"count": 0},
        {"count": 11},
        {"timeout_seconds": 0},
        {"timeout_seconds": 6},
        # Packets must fit in 15 s minus the 5 s DNS may take.
        {"count": 3, "timeout_seconds": 5},
        {"count": 4, "timeout_seconds": 3},
        {"count": 10, "timeout_seconds": 2},
    ],
)
def test_schema_rejects_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PingCheckConfig.model_validate(config(**overrides))


def test_schema_largest_settings_fit_the_time_cap() -> None:
    PingCheckConfig.model_validate(config(count=10, timeout_seconds=1))
    PingCheckConfig.model_validate(config(count=2, timeout_seconds=5))
    PingCheckConfig.model_validate(config(count=3, timeout_seconds=3))


def test_every_accepted_config_survives_the_slowest_dns() -> None:
    """A slow lookup must never make a slow reply count as lost: for every config the
    schema accepts, the slowest DNS the client allows plus the packets' worst case still
    fits in the client's total limit."""
    accepted = 0
    for count in range(1, MAX_PING_COUNT + 1):
        for timeout in range(1, int(MAX_PING_REPLY_TIMEOUT) + 1):
            try:
                cfg = PingCheckConfig.model_validate(config(count=count, timeout_seconds=timeout))
            except ValidationError:
                continue
            accepted += 1
            assert PING_RESOLVE_TIMEOUT + cfg.worst_case_seconds() <= MAX_PING_TOTAL_TIMEOUT
            # worst_case_seconds really is the client's worst case: every packet but the
            # last waits for the later of its timeout and the next send.
            last = (count - 1) * max(PING_INTERVAL, timeout) + timeout
            assert last <= cfg.worst_case_seconds()
    assert accepted > 10
    # The timing itself (slow DNS, then slow replies all counted) is tested on the client:
    # test_net_guard.py::test_ping_slow_dns_does_not_shorten_reply_windows.


def test_ping_check_uses_the_clients_interval() -> None:
    ctx = make_ctx(PingCheck, config())
    client = ctx.ping_client(HOST)
    assert client._interval == PING_INTERVAL


def test_host_is_a_host_field_and_nothing_is_secret() -> None:
    stored, secrets = ic.split(PingCheckConfig.model_validate(config()))
    assert secrets == {}
    # The SSRF pre-check on save finds the host by its field name, in the same normal
    # (ASCII) form the guard pings.
    assert ic.configured_hosts(stored) == [("host", HOST, 443, None)]


def test_host_description_says_it_is_plaintext() -> None:
    served = config_json_schema(PingCheckConfig)["properties"]["host"]["description"]
    assert "plaintext" in served


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_icmp: FakeIcmp) -> None:
    fake_icmp.answer(ADDRESS)
    result = await run_test()
    assert result.ok
    assert result.message.startswith(f"1/1 replies from {HOST}, avg ")
    assert result.message.endswith(", 0% loss")
    assert result.hint is None
    # Went to the address the guard checked, and only there.
    assert fake_icmp.addresses == [ADDRESS]
    assert_read_only(fake_icmp)


async def test_connection_partial_loss_is_ok(fake_icmp: FakeIcmp) -> None:
    fake_icmp.answer(ADDRESS, drop(0))
    result = await run_test(count=2)
    assert result.ok
    assert result.message.startswith(f"1/2 replies from {HOST}")
    assert "50% loss" in result.message


async def test_connection_no_reply(fake_icmp: FakeIcmp) -> None:
    result = await run_test()
    assert not result.ok
    assert result.message == f"no reply from {HOST} (1 packets sent, ICMP may be blocked)"
    assert result.hint is not None
    assert result.hint.model_dump() == ICMP_HINT


async def test_connection_refused_by_guard(fake_icmp: FakeIcmp) -> None:
    result = await run_test(host="metadata.example.internal")
    assert not result.ok
    assert result.message.startswith("refused by the network guard:")
    assert "169.254.169.254" in result.message
    assert fake_icmp.opened == []  # never opened a socket


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "169.254.169.254", "192.0.2.30"])
async def test_connection_denied_literals(fake_icmp: FakeIcmp, host: str) -> None:
    fake_icmp.answer(host)
    result = await run_test(host=host)
    assert not result.ok and "network guard" in result.message
    assert fake_icmp.opened == []


async def test_connection_unknown_host(fake_icmp: FakeIcmp) -> None:
    result = await run_test(host="missing.example.internal")
    assert not result.ok
    assert "could not resolve missing.example.internal" in result.message


async def test_connection_icmp_not_permitted(fake_icmp: FakeIcmp) -> None:
    fake_icmp.open_error = PermissionError(13, "Permission denied")
    result = await run_test()
    assert not result.ok
    assert result.message.startswith("ping failed: ICMP is not permitted")
    assert "ping_group_range" in result.message
    assert result.hint is None  # a local permission problem, not a firewall


async def test_connection_broadcast_refused(fake_icmp: FakeIcmp) -> None:
    fake_icmp.connect_error = PermissionError(13, "Permission denied")
    result = await run_test(host="10.0.0.255")
    assert not result.ok
    assert result.message == (
        "ping failed: the system refused to ping 10.0.0.255 (is it a broadcast address?)"
    )
    assert result.hint is None
    assert fake_icmp.sent == []


async def test_unrecognised_errors_propagate(fake_icmp: FakeIcmp) -> None:
    # Left to the instance API, which reports only the exception type.
    fake_icmp.open_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await run_test()


# --- poll ------------------------------------------------------------------------------


async def poll(**overrides: Any) -> dict[str, float]:
    samples = await PingCheck().poll(make_ctx(PingCheck, config(**overrides)))
    for sample in samples:
        assert sample.type == "gauge"
        assert sample.labels == {}  # the collector adds the standard labels
    return {s.name: s.value for s in samples}


async def test_poll_success(fake_icmp: FakeIcmp) -> None:
    fake_icmp.answer(ADDRESS)
    values = await poll(count=2)
    assert values.keys() == {
        "vantage_ping_check_packet_loss_ratio",
        "vantage_ping_check_packets_sent",
        "vantage_ping_check_packets_received",
        "vantage_ping_check_rtt_min_seconds",
        "vantage_ping_check_rtt_avg_seconds",
        "vantage_ping_check_rtt_max_seconds",
    }
    assert values["vantage_ping_check_packet_loss_ratio"] == 0
    assert values["vantage_ping_check_packets_sent"] == 2
    assert values["vantage_ping_check_packets_received"] == 2
    rtt_min = values["vantage_ping_check_rtt_min_seconds"]
    rtt_avg = values["vantage_ping_check_rtt_avg_seconds"]
    rtt_max = values["vantage_ping_check_rtt_max_seconds"]
    assert 0 < rtt_min <= rtt_avg <= rtt_max < 1
    assert_read_only(fake_icmp)
    assert all(s.closed for s in fake_icmp.opened)


async def test_poll_partial_loss(fake_icmp: FakeIcmp) -> None:
    fake_icmp.answer(ADDRESS, drop(1))
    values = await poll(count=2)
    assert values["vantage_ping_check_packet_loss_ratio"] == 0.5
    assert values["vantage_ping_check_packets_received"] == 1


async def test_poll_no_reply_raises(fake_icmp: FakeIcmp) -> None:
    with pytest.raises(NoReply) as caught:
        await poll(count=2)
    # The same text test_connection gives, so the poll status reads the same.
    assert str(caught.value) == f"no reply from {HOST} (2 packets sent, ICMP may be blocked)"


async def test_poll_refused_by_guard(fake_icmp: FakeIcmp) -> None:
    with pytest.raises(NetGuardError):
        await poll(host="metadata.example.internal")
    assert fake_icmp.opened == []


async def test_poll_icmp_not_permitted(fake_icmp: FakeIcmp) -> None:
    fake_icmp.open_error = PermissionError(13, "Permission denied")
    with pytest.raises(PingError):
        await poll()


# --- through the poll collector --------------------------------------------------------


async def collect(
    engine: Engine, cfg: dict[str, Any], *, site: str | None
) -> tuple[MetricsStore, PollSpec]:
    box = SecretBox(TEST_MASTER_KEY)
    instance_id = uuid.uuid4()
    stored, secrets = ic.split(PingCheckConfig.model_validate(cfg))
    spec = PollSpec(
        instance_id=instance_id,
        type_id="ping_check",
        name="gw-1",
        site=site,
        updated_at=utcnow(),
        config=stored,
        secrets=ic.encrypt(box, instance_id, secrets),
    )
    store = MetricsStore()
    scheduler = PollScheduler(
        session_factory=make_session_factory(engine),
        registry=Registry([PingCheck]),
        secret_box=box,
        net_guard_factory=fake_guards,
        store=store,
    )
    store.track(spec.instance_id, spec.labels)
    await scheduler.poll_once(spec, PingCheck())
    return store, spec


@pytest.mark.parametrize(("site", "label"), [("lab", "lab"), (None, "")])
async def test_collector_exposes_samples_with_standard_labels(
    fake_icmp: FakeIcmp, engine: Engine, site: str | None, label: str
) -> None:
    fake_icmp.answer(ADDRESS)
    store, spec = await collect(engine, config(), site=site)
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "up"
    text = generate_latest(make_registry(store)).decode()
    # A site-less instance gets vantage_site="" (absent to Prometheus), never a placeholder.
    labels = (
        f'vantage_instance="gw-1",vantage_instance_id="{spec.instance_id}",'
        f'vantage_integration="ping_check",vantage_site="{label}"'
    )
    assert f"vantage_ping_check_packet_loss_ratio{{{labels}}} 0.0" in text
    assert f"vantage_ping_check_packets_received{{{labels}}} 1.0" in text
    assert f"vantage_instance_up{{{labels}}} 1.0" in text


async def test_collector_marks_silent_host_down(
    fake_icmp: FakeIcmp, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store, spec = await collect(engine, config(), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error == f"no reply from {HOST} (1 packets sent, ICMP may be blocked)"
    assert status.error == (await run_test()).message  # reads as test_connection does
    assert fake_icmp.sent  # echo requests went out (to the fake socket), none answered
    text = generate_latest(make_registry(store)).decode()
    assert "vantage_ping_check_packet_loss_ratio" not in text
    assert 'vantage_instance_up{vantage_instance="gw-1"' in text


async def test_collector_broadcast_status_says_why(
    fake_icmp: FakeIcmp, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    # The poll status carries the same secret-free PingError text as test_connection.
    caplog.set_level(logging.DEBUG)
    fake_icmp.connect_error = PermissionError(13, "Permission denied")
    store, spec = await collect(engine, config(host="10.0.0.255"), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error == (
        "ping failed: the system refused to ping 10.0.0.255 (is it a broadcast address?)"
    )
    assert fake_icmp.sent == []
    assert "Traceback" not in caplog.text


async def test_collector_icmp_not_permitted_status(fake_icmp: FakeIcmp, engine: Engine) -> None:
    fake_icmp.open_error = PermissionError(13, "Permission denied")
    store, spec = await collect(engine, config(), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error is not None
    assert status.error.startswith("ping failed: ICMP is not permitted")
    assert "ping_group_range" in status.error


async def test_collector_degrades_config_made_invalid_by_upgrade(
    fake_icmp: FakeIcmp, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Upgrade note: 3 packets x 5 s was accepted before the DNS share was reserved and
    is refused now. A stored instance with it is revalidated on every poll, so it goes
    ``down`` with a fixed message (no crash, no ping sent), and other instances keep
    polling."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValidationError):
        PingCheckConfig.model_validate(config(count=3, timeout_seconds=5))
    box = SecretBox(TEST_MASTER_KEY)
    store = MetricsStore()
    scheduler = PollScheduler(
        session_factory=make_session_factory(engine),
        registry=Registry([PingCheck]),
        secret_box=box,
        net_guard_factory=fake_guards,
        store=store,
    )

    def spec(name: str, stored: dict[str, Any]) -> PollSpec:
        made = PollSpec(
            instance_id=uuid.uuid4(),
            type_id="ping_check",
            name=name,
            site="lab",
            updated_at=utcnow(),
            config=stored,  # as stored in the database by the older version
            secrets=None,
        )
        store.track(made.instance_id, made.labels)
        return made

    old = spec("old-gw", {"host": HOST, "count": 3, "timeout_seconds": 5})
    good = spec("gw-2", {"host": HOST, "count": 1, "timeout_seconds": 1})
    fake_icmp.answer(ADDRESS)
    await scheduler.poll_once(old, PingCheck())
    await scheduler.poll_once(good, PingCheck())

    old_status = store.status(old.instance_id)
    assert old_status is not None and old_status.state == "down"
    assert old_status.error == "stored config is not valid for this integration"
    good_status = store.status(good.instance_id)
    assert good_status is not None and good_status.state == "up"
    # Only the valid instance pinged; the invalid one never reached the network.
    assert len(fake_icmp.opened) == 1
    text = generate_latest(make_registry(store)).decode()
    assert 'vantage_ping_check_packets_received{vantage_instance="gw-2"' in text
    assert 'vantage_instance_up{vantage_instance="old-gw"' in text
    assert 'vantage_ping_check_packets_received{vantage_instance="old-gw"' not in text
    assert "timeout_seconds" not in caplog.text  # the validation error isn't echoed


async def test_collector_reports_guard_refusal(fake_icmp: FakeIcmp, engine: Engine) -> None:
    store, spec = await collect(engine, config(host="metadata.example.internal"), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error is not None and status.error.startswith("refused by the network guard")
    assert fake_icmp.opened == []


# --- through the instance API ----------------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_icmp: FakeIcmp) -> Iterator[TestClient]:
    # The built-in registry, as in production; only DNS and the ICMP sockets are fake.
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def instance_body(**overrides: Any) -> dict[str, Any]:
    return {"type_id": "ping_check", "name": "gw-1", "config": config(**overrides)}


def test_api_create_test_and_read(admin: TestClient, fake_icmp: FakeIcmp) -> None:
    fake_icmp.answer(ADDRESS)
    r = admin.post("/api/v1/instances/test", json={"type_id": "ping_check", "config": config()})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    created = admin.post("/api/v1/instances", json=instance_body())
    assert created.status_code == 201, created.text
    assert created.json()["config"] == {"host": HOST, "count": 1, "timeout_seconds": 1}
    assert created.json()["site"] is None
    instance_id = created.json()["id"]
    tested = admin.post(f"/api/v1/instances/{instance_id}/test")
    assert tested.status_code == 200 and tested.json()["ok"] is True
    cards = admin.get(f"/api/v1/instances/{instance_id}/cards")
    assert cards.json() == [
        {"card_type": "ping-check", "title": "Ping check", "default_size": [2, 2]}
    ]
    assert set(fake_icmp.addresses) == {ADDRESS}


@pytest.mark.parametrize("host", ["metadata.example.internal", "127.0.0.1", "192.0.2.30"])
def test_api_refuses_denied_host_on_save(admin: TestClient, fake_icmp: FakeIcmp, host: str) -> None:
    r = admin.post("/api/v1/instances", json=instance_body(host=host))
    assert r.status_code == 422, r.text
    assert fake_icmp.opened == []


def test_api_refuses_unicode_host(admin: TestClient) -> None:
    r = admin.post("/api/v1/instances", json=instance_body(host="пример.example"))
    assert r.status_code == 422


def test_api_serves_icmp_hint_without_port(admin: TestClient, fake_icmp: FakeIcmp) -> None:
    r = admin.post("/api/v1/instances/test", json={"type_id": "ping_check", "config": config()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["hint"] == ICMP_HINT  # "port": null for icmp


# --- static parts ----------------------------------------------------------------------


def test_firewall_hints_and_cards() -> None:
    ctx = make_ctx(PingCheck, config())
    assert [h.model_dump() for h in PingCheck().firewall_hints(ctx)] == [ICMP_HINT]
    [hint] = PingCheck().firewall_hints(make_ctx(PingCheck, config(host="[fd00::30]")))
    assert (hint.protocol, hint.port, hint.destination) == ("icmp", None, "fd00::30")
    assert PingCheck().poll_interval_seconds() == 60
    assert [c.card_type for c in PingCheck().cards()] == ["ping-check"]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_no_sockets_in_integration_code() -> None:
    """Contract 5: raw sockets never appear in integration code; every built-in
    integration reaches the network only through its context's guarded clients."""
    root = Path(inspect.getfile(builtin)).parent
    files = sorted(root.rglob("*.py"))
    assert Path(inspect.getfile(ping_module)) in files
    for path in files:
        imported = _imported_modules(path)
        assert not imported & {"socket", "asyncio_dgram", "icmplib", "scapy"}, path
        assert "SOCK_RAW" not in path.read_text(encoding="utf-8"), path

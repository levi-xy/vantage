"""The Integration contract (Contract 1) and ``InstanceContext``."""

import uuid
from typing import Any, get_args

import pytest
from pydantic import SecretStr, ValidationError

from app.core import net_guard
from app.core.net_guard import (
    MAX_SNMP_RETRIES,
    MAX_SNMP_TIMEOUT,
    GuardedTransport,
    NetGuard,
    NetGuardError,
    SnmpV1,
    SnmpV2c,
)
from app.integrations.base import (
    CardSpec,
    Category,
    FirewallHint,
    InstanceContext,
    Integration,
    Sample,
    ScrapeTarget,
    TestResult,
)
from tests.integrations.fake import BrokenAbstract, FakeConfig, FakeIntegration

FAKE_TOKEN = "example-token-7e6f"
FAKE_COMMUNITY = "example-community-3b2a"


async def loopback_resolver(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


def make_ctx(**overrides: Any) -> InstanceContext:
    values: dict[str, Any] = {
        "instance_id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
        "name": "lab-device",
        "config": FakeConfig(host="192.0.2.10", api_token=SecretStr(FAKE_TOKEN)),
        "labels": {"vantage_site": "lab"},
        "net_guard": NetGuard(resolver=loopback_resolver),
    }
    values.update(overrides)
    return InstanceContext(**values)


def test_category_literal_matches_contract() -> None:
    assert set(get_args(Category)) == {
        "firewall",
        "hypervisor",
        "storage",
        "switch",
        "tunnel",
        "cloud",
        "media",
        "host",
        "generic",
    }


def test_models_and_defaults() -> None:
    hint = FirewallHint(protocol="tcp", port=443, destination="192.0.2.10", reason="API")
    assert TestResult(ok=False, message="timed out", hint=hint).hint == hint
    assert TestResult(ok=True, message="ok").hint is None
    target = ScrapeTarget(address="192.0.2.10:9100")
    assert (target.metrics_path, target.scheme) == ("/metrics", "http")
    assert CardSpec(card_type="host", title="Host", default_size=(2, 1)).default_size == (2, 1)
    sample = Sample(name="vantage_fake_device_up", value=1, type="counter")
    assert sample.labels == {}
    with pytest.raises(ValidationError):
        Sample(name="vantage_fake_device_up", value=1, type="histogram")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        FirewallHint(protocol="sctp", port=1, destination="192.0.2.10", reason="x")  # type: ignore[arg-type]


def test_firewall_hint_protocols_match_contract() -> None:
    assert set(get_args(FirewallHint.model_fields["protocol"].annotation)) == {
        "tcp",
        "udp",
        "icmp",
    }


def test_icmp_hint_has_no_port() -> None:
    hint = FirewallHint(protocol="icmp", port=None, destination="10.0.0.30", reason="ping")
    assert hint.model_dump()["port"] is None
    for port in (0, 7, 443):
        with pytest.raises(ValidationError, match="icmp hint has no port"):
            FirewallHint(protocol="icmp", port=port, destination="10.0.0.30", reason="x")


@pytest.mark.parametrize("protocol", ["tcp", "udp"])
def test_tcp_and_udp_hints_need_a_port(protocol: Any) -> None:
    with pytest.raises(ValidationError, match="needs a port"):
        FirewallHint(protocol=protocol, port=None, destination="10.0.0.30", reason="x")
    with pytest.raises(ValidationError):  # port is required, not defaulted
        FirewallHint(protocol=protocol, destination="10.0.0.30", reason="x")  # type: ignore[call-arg]
    assert FirewallHint(protocol=protocol, port=161, destination="10.0.0.30", reason="x").port


async def test_integration_defaults() -> None:
    integration = FakeIntegration()
    ctx = make_ctx()
    assert integration.scrape_targets(ctx) == []
    assert Integration.poll_interval_seconds(integration) == 60
    assert FakeIntegration.cloud_hosts == ()
    assert await Integration.poll(integration, ctx) == []


def test_required_methods_are_abstract() -> None:
    assert Integration.__abstractmethods__ == {"test_connection", "cards", "firewall_hints"}
    with pytest.raises(TypeError):
        BrokenAbstract()  # type: ignore[abstract]


def test_config_as_checks_the_type() -> None:
    ctx = make_ctx()
    assert ctx.config_as(FakeConfig).host == "192.0.2.10"
    assert FakeIntegration().firewall_hints(ctx)[0].destination == "192.0.2.10"
    with pytest.raises(TypeError):
        ctx.config_as(Sample)


def test_context_repr_has_no_config_or_secrets() -> None:
    text = repr(make_ctx())
    assert FAKE_TOKEN not in text
    assert "192.0.2.10" not in text
    assert "lab-device" in text


async def test_http_client_goes_through_net_guard() -> None:
    ctx = make_ctx()
    async with ctx.http_client(base_url="http://device.example.com") as client:
        assert isinstance(client._transport, GuardedTransport)
        assert client.follow_redirects is False
        with pytest.raises(NetGuardError):  # resolves to loopback: refused before connecting
            await client.get("/")
        with pytest.raises(NetGuardError):
            await client.get("http://127.0.0.1/")


async def test_snmp_client_goes_through_net_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def get_cmd(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return None, 0, 0, []

    monkeypatch.setattr(net_guard, "get_cmd", get_cmd)
    client = make_ctx().snmp_client("127.0.0.1", SnmpV2c(community=SecretStr(FAKE_COMMUNITY)))
    with pytest.raises(NetGuardError) as exc:
        await client.get(["1.3.6.1.2.1.1.1.0"])
    assert FAKE_COMMUNITY not in str(exc.value)
    assert calls == []


async def test_snmp_client_timeouts_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    targets: list[Any] = []

    async def get_cmd(*args: Any, **kwargs: Any) -> Any:
        targets.append(args[2])
        return None, 0, 0, []

    async def private_resolver(host: str, port: int) -> list[str]:
        return ["10.0.0.20"]

    monkeypatch.setattr(net_guard, "get_cmd", get_cmd)
    ctx = make_ctx(net_guard=NetGuard(resolver=private_resolver))
    auth = SnmpV1(community=SecretStr(FAKE_COMMUNITY))
    client = ctx.snmp_client("10.0.0.20", auth, port=1161, timeout=1.5, retries=0)
    try:
        await client.get(["1.3.6.1.2.1.1.1.0"])
    finally:
        client.close()
    assert (targets[0].timeout, targets[0].retries) == (1.5, 0)
    assert targets[0].transport_address == ("10.0.0.20", 1161)
    for bad in (
        {"timeout": MAX_SNMP_TIMEOUT + 0.1},
        {"timeout": 0},
        {"retries": MAX_SNMP_RETRIES + 1},
        {"retries": -1},
    ):
        with pytest.raises(ValueError):
            ctx.snmp_client("10.0.0.20", auth, **bad)

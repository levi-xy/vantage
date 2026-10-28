"""cloudflared metrics endpoint (M4f): schema, scrape targets, connection test, and the
path to Prometheus: created through the API, served by /sd/targets with the standard
labels, and left out when the SSRF guard refuses it.

No real network: ``fake_net`` keeps the real SSRF guard and fakes only the sockets.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpcore
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from pydantic import ValidationError
from sqlalchemy import Engine

from app.collectors.scheduler import polls
from app.core.db import make_session_factory
from app.core.prometheus_auth import PrometheusToken
from app.integrations import instance_config as ic
from app.integrations.base import TestResult
from app.integrations.builtin.cloudflared import Cloudflared, CloudflaredConfig
from app.integrations.builtin.cloudflared import integration as cloudflared_module
from app.integrations.builtin.cloudflared.integration import describe_cloudflared
from app.models import Instance
from tests.integrations.netfake import FakeNetwork, fake_guards, hang, http_response, make_ctx
from tests.test_auth import do_setup, make_client

FIXTURE = (Path(__file__).parent / "fixtures" / "cloudflared.prom").read_bytes()
ADDRESS = ("10.0.0.20", 20241)
PROM_TOKEN = "example-prometheus-token-0123456789abcdef"


async def run_test(**config: Any) -> TestResult:
    cfg = {"host": "node.example.internal", **config}
    return await Cloudflared().test_connection(make_ctx(Cloudflared, cfg))


def assert_read_only(net: FakeNetwork) -> None:
    """Only GET /metrics: nothing that could change the host's state."""
    assert net.requests
    for request in net.requests:
        assert (request.method, request.target) == ("GET", "/metrics")


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults() -> None:
    cfg = CloudflaredConfig.model_validate({"host": "10.0.0.20"})
    assert (cfg.host, cfg.port, cfg.scheme) == ("10.0.0.20", 20241, "http")


@pytest.mark.parametrize(
    ("host", "normal"),
    [
        (" Node.Example.Internal. ", "node.example.internal"),
        ("10.0.0.20", "10.0.0.20"),
        ("[fd00::20]", "fd00::20"),
        ("FD00:0:0::20", "fd00::20"),
    ],
)
def test_schema_normalises_host(host: str, normal: str) -> None:
    assert CloudflaredConfig.model_validate({"host": host}).host == normal


@pytest.mark.parametrize(
    "config",
    [
        {"host": ""},
        {"host": "http://10.0.0.20"},
        {"host": "10.0.0.20:20241"},
        {"host": "node example"},
        {"host": "0x7f.1"},
        {"host": "fe80::1%eth0"},
        {"host": "10.0.0.20", "port": 0},
        {"host": "10.0.0.20", "port": 65536},
        {"host": "10.0.0.20", "scheme": "ftp"},
        {},
    ],
)
def test_schema_rejects(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CloudflaredConfig.model_validate(config)


def test_no_secrets_and_host_is_checked_on_save() -> None:
    stored, secrets = ic.split(CloudflaredConfig.model_validate({"host": "node.example.internal"}))
    assert secrets == {}
    assert ic.configured_hosts(stored) == [("host", "node.example.internal", 20241, None)]


# --- scrape targets --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "address", "scheme"),
    [
        ({"host": "10.0.0.20"}, "10.0.0.20:20241", "http"),
        ({"host": "node.example.internal", "port": 20242}, "node.example.internal:20242", "http"),
        ({"host": "fd00::20", "scheme": "https"}, "[fd00::20]:20241", "https"),
    ],
)
def test_scrape_targets(config: dict[str, Any], address: str, scheme: str) -> None:
    [target] = Cloudflared().scrape_targets(make_ctx(Cloudflared, config))
    assert (target.address, target.metrics_path, target.scheme) == (address, "/metrics", scheme)


def test_scrape_only() -> None:
    # No poll(): the collector never schedules it, and its status is "unknown".
    assert not polls(Cloudflared)


# --- parsing ---------------------------------------------------------------------------


def test_describe_cloudflared() -> None:
    assert describe_cloudflared(FIXTURE.decode()) == "2024.6.1"


def test_fixture_has_the_metrics_the_card_reads() -> None:
    names = {f.name for f in text_string_to_metric_families(FIXTURE.decode())}
    assert {
        "cloudflared_tunnel_ha_connections",
        "cloudflared_tunnel_total_requests",
        "cloudflared_tunnel_request_errors",
        "cloudflared_proxy_connect_latency",
    } <= names


@pytest.mark.parametrize(
    "text",
    [
        "<html><body>Not metrics</body></html>",
        '# TYPE node_exporter_build_info gauge\nnode_exporter_build_info{version="1.8.2"} 1\n',
        "go_goroutines 42\n",
        "",
    ],
)
def test_describe_rejects_other_pages(text: str) -> None:
    assert describe_cloudflared(text) is None


def test_describe_without_version() -> None:
    assert describe_cloudflared("cloudflared_tunnel_ha_connections 1\n") == "?"


def test_describe_sanitises_device_text() -> None:
    weird = "x\x1b[31mé" + "a" * 100
    text = f'cloudflared_tunnel_ha_connections 1\nbuild_info{{version="{weird}"}} 1\n'
    version = describe_cloudflared(text) or ""
    assert version.isascii() and version.isprintable()
    assert len(version) == 64


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    result = await run_test()
    assert result.ok
    assert result.message == "cloudflared 2024.6.1"
    assert fake_net.connects == [ADDRESS]  # the address the guard checked
    assert fake_net.requests[0].headers["host"] == "node.example.internal:20241"
    assert "authorization" not in fake_net.requests[0].headers
    assert_read_only(fake_net)


async def test_connection_https_verifies_tls(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    assert (await run_test(scheme="https")).ok
    [(server_name, context)] = fake_net.tls
    assert server_name == "node.example.internal"
    assert context.check_hostname


async def test_connection_not_cloudflared(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, b"<html>router admin page</html>"))
    result = await run_test()
    assert not result.ok
    assert (
        result.message == "node.example.internal:20241 answered, but not with cloudflared metrics"
    )
    assert result.hint is None


async def test_connection_http_error(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(404, b"secret-looking body", reason="Not Found"))
    result = await run_test()
    assert not result.ok
    assert result.message == "node.example.internal:20241/metrics answered HTTP 404"


async def test_connection_body_too_large(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cloudflared_module, "MAX_BODY_BYTES", 100)
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    result = await run_test()
    assert not result.ok and "too large" in result.message


@pytest.mark.parametrize(
    "error", [httpcore.ConnectTimeout("timed out"), httpcore.ConnectError("refused")]
)
async def test_connection_failure_gives_firewall_hint(
    fake_net: FakeNetwork, error: Exception
) -> None:
    fake_net.fail(*ADDRESS, error)
    result = await run_test()
    assert not result.ok
    assert "node.example.internal:20241" in result.message
    assert result.hint is not None
    assert (result.hint.protocol, result.hint.port, result.hint.destination) == (
        "tcp",
        20241,
        "node.example.internal",
    )


async def test_connection_own_timeout_gives_firewall_hint(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The integration's asyncio.timeout fires before the guarded client's 15 s cap.
    monkeypatch.setattr(cloudflared_module, "TIMEOUT_SECONDS", 0.2)
    fake_net.serve(*ADDRESS, hang)
    result = await run_test()
    assert not result.ok
    assert result.message == "timed out waiting for node.example.internal:20241"
    assert result.hint is not None and result.hint.port == 20241


async def test_connection_slow_exporter_gives_firewall_hint(fake_net: FakeNetwork) -> None:
    # Connects, then never answers: the guarded client's total limit (15 s, shortened
    # here) ends the request.
    fake_net.serve(*ADDRESS, hang)
    fake_net.total_timeout = 0.2
    result = await run_test()
    assert not result.ok
    assert result.message == "timed out waiting for node.example.internal:20241"
    assert result.hint is not None and result.hint.port == 20241


async def test_connection_refused_by_guard(fake_net: FakeNetwork) -> None:
    result = await run_test(host="metadata.example.internal")
    assert not result.ok
    assert result.message.startswith("refused by the network guard:")
    assert result.hint is None
    assert fake_net.connects == []


def test_firewall_hints_and_cards() -> None:
    ctx = make_ctx(Cloudflared, {"host": "10.0.0.20", "port": 20242})
    [hint] = Cloudflared().firewall_hints(ctx)
    assert (hint.protocol, hint.port, hint.destination) == ("tcp", 20242, "10.0.0.20")
    assert [c.card_type for c in Cloudflared().cards()] == ["cloudflared"]


# --- end to end: API → /sd/targets -----------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_net: FakeNetwork) -> Iterator[TestClient]:
    # The built-in registry, as in production; only DNS and sockets are fake.
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        client.app.state.prometheus_token = PrometheusToken(PROM_TOKEN)  # type: ignore[attr-defined]
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def store_directly(engine: Engine, name: str, host: str) -> None:
    """Bypass the API's host check, as an old row or a changed DNS answer would."""
    with make_session_factory(engine)() as db:
        db.add(
            Instance(
                id=uuid.uuid4(),
                type_id="cloudflared",
                name=name,
                site=None,
                config={"host": host, "port": 20241, "scheme": "http"},
                secrets=None,
            )
        )
        db.commit()


def test_api_to_service_discovery(admin: TestClient, engine: Engine, fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    cfg = {"host": "node.example.internal"}
    tested = admin.post("/api/v1/instances/test", json={"type_id": "cloudflared", "config": cfg})
    assert tested.json()["ok"] is True
    created = admin.post(
        "/api/v1/instances",
        json={"type_id": "cloudflared", "name": "lab-tunnel-1", "site": "lab", "config": cfg},
    )
    assert created.status_code == 201, created.text
    instance_id = created.json()["id"]
    assert created.json()["config"] == {
        "host": "node.example.internal",
        "port": 20241,
        "scheme": "http",
    }

    # Left out of service discovery: it resolves to the metadata address.
    store_directly(engine, "sneaky", "metadata.example.internal")

    r = admin.get("/sd/targets", headers={"Authorization": f"Bearer {PROM_TOKEN}"})
    assert r.status_code == 200, r.text
    assert r.json() == [
        {
            "targets": ["node.example.internal:20241"],
            "labels": {
                "__metrics_path__": "/metrics",
                "__scheme__": "http",
                "vantage_instance_id": instance_id,
                "vantage_instance": "lab-tunnel-1",
                "vantage_integration": "cloudflared",
                "vantage_site": "lab",
            },
        }
    ]
    assert PROM_TOKEN not in r.text
    # Service discovery itself never connects to targets; only the connection test did.
    assert fake_net.connects == [ADDRESS]


def test_api_refuses_denied_host(admin: TestClient, fake_net: FakeNetwork) -> None:
    for host in ("127.0.0.1", "169.254.169.254", "metadata.example.internal"):
        r = admin.post(
            "/api/v1/instances",
            json={"type_id": "cloudflared", "name": f"bad-{host}", "config": {"host": host}},
        )
        assert r.status_code == 422, host
    assert fake_net.connects == []

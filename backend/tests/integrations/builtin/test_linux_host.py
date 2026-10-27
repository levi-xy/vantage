"""Linux host via node_exporter (M2.5): schema, scrape targets, connection test, and the
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
from pydantic import ValidationError
from sqlalchemy import Engine

from app.collectors.scheduler import polls
from app.core.db import make_session_factory
from app.core.prometheus_auth import PrometheusToken
from app.integrations import instance_config as ic
from app.integrations.base import TestResult
from app.integrations.builtin.linux_host import LinuxHost, LinuxHostConfig
from app.integrations.builtin.linux_host import integration as linux_host_module
from app.integrations.builtin.linux_host.integration import describe_node_exporter
from app.models import Instance
from tests.integrations.netfake import FakeNetwork, fake_guards, hang, http_response, make_ctx
from tests.test_auth import do_setup, make_client

FIXTURE = (Path(__file__).parent / "fixtures" / "node_exporter.prom").read_bytes()
ADDRESS = ("10.0.0.20", 9100)
PROM_TOKEN = "example-prometheus-token-0123456789abcdef"


async def run_test(**config: Any) -> TestResult:
    cfg = {"host": "node.example.internal", **config}
    return await LinuxHost().test_connection(make_ctx(LinuxHost, cfg))


def assert_read_only(net: FakeNetwork) -> None:
    """Only GET /metrics: nothing that could change the host's state."""
    assert net.requests
    for request in net.requests:
        assert (request.method, request.target) == ("GET", "/metrics")


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults() -> None:
    cfg = LinuxHostConfig.model_validate({"host": "10.0.0.20"})
    assert (cfg.host, cfg.port, cfg.scheme) == ("10.0.0.20", 9100, "http")


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
    assert LinuxHostConfig.model_validate({"host": host}).host == normal


@pytest.mark.parametrize(
    "config",
    [
        {"host": ""},
        {"host": "http://10.0.0.20"},
        {"host": "10.0.0.20:9100"},
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
        LinuxHostConfig.model_validate(config)


def test_no_secrets_and_host_is_checked_on_save() -> None:
    stored, secrets = ic.split(LinuxHostConfig.model_validate({"host": "node.example.internal"}))
    assert secrets == {}
    assert ic.configured_hosts(stored) == [("host", "node.example.internal", 9100, None)]


# --- scrape targets --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "address", "scheme"),
    [
        ({"host": "10.0.0.20"}, "10.0.0.20:9100", "http"),
        ({"host": "node.example.internal", "port": 9101}, "node.example.internal:9101", "http"),
        ({"host": "fd00::20", "scheme": "https"}, "[fd00::20]:9100", "https"),
    ],
)
def test_scrape_targets(config: dict[str, Any], address: str, scheme: str) -> None:
    [target] = LinuxHost().scrape_targets(make_ctx(LinuxHost, config))
    assert (target.address, target.metrics_path, target.scheme) == (address, "/metrics", scheme)


def test_scrape_only() -> None:
    # No poll(): the collector never schedules it, and its status is "unknown".
    assert not polls(LinuxHost)


# --- parsing ---------------------------------------------------------------------------


def test_describe_node_exporter() -> None:
    assert describe_node_exporter(FIXTURE.decode()) == ("1.8.2", "lab-host-1")


@pytest.mark.parametrize(
    "text",
    [
        "<html><body>Not metrics</body></html>",
        '# TYPE windows_cpu_time_total counter\nwindows_cpu_time_total{core="0,0"} 1\n',
        "",
    ],
)
def test_describe_rejects_other_pages(text: str) -> None:
    assert describe_node_exporter(text) is None


def test_describe_sanitises_device_text() -> None:
    weird = "x\x1b[31mé" + "a" * 100
    text = (
        f'node_exporter_build_info{{version="1.8.2\\n"}} 1\n'
        f'node_uname_info{{nodename="{weird}"}} 1\n'
    )
    version, nodename = describe_node_exporter(text) or ("", "")
    assert version == "1.8.2"
    assert nodename.isascii() and nodename.isprintable()
    assert len(nodename) == 64


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    result = await run_test()
    assert result.ok
    assert result.message == "node_exporter 1.8.2 on lab-host-1"
    assert fake_net.connects == [ADDRESS]  # the address the guard checked
    assert fake_net.requests[0].headers["host"] == "node.example.internal:9100"
    assert "authorization" not in fake_net.requests[0].headers
    assert_read_only(fake_net)


async def test_connection_https_verifies_tls(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    assert (await run_test(scheme="https")).ok
    [(server_name, context)] = fake_net.tls
    assert server_name == "node.example.internal"
    assert context.check_hostname


async def test_connection_not_node_exporter(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, b"<html>router admin page</html>"))
    result = await run_test()
    assert not result.ok
    assert (
        result.message == "node.example.internal:9100 answered, but not with node_exporter metrics"
    )
    assert result.hint is None


async def test_connection_http_error(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(404, b"secret-looking body", reason="Not Found"))
    result = await run_test()
    assert not result.ok
    assert result.message == "node.example.internal:9100/metrics answered HTTP 404"


async def test_connection_body_too_large(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linux_host_module, "MAX_BODY_BYTES", 100)
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
    assert "node.example.internal:9100" in result.message
    assert result.hint is not None
    assert (result.hint.protocol, result.hint.port, result.hint.destination) == (
        "tcp",
        9100,
        "node.example.internal",
    )


async def test_connection_slow_exporter_gives_firewall_hint(fake_net: FakeNetwork) -> None:
    # Connects, then never answers: the guarded client's total limit (15 s, shortened
    # here) ends the request.
    fake_net.serve(*ADDRESS, hang)
    fake_net.total_timeout = 0.2
    result = await run_test()
    assert not result.ok
    assert result.message == "timed out waiting for node.example.internal:9100"
    assert result.hint is not None and result.hint.port == 9100


async def test_connection_refused_by_guard(fake_net: FakeNetwork) -> None:
    result = await run_test(host="metadata.example.internal")
    assert not result.ok
    assert result.message.startswith("refused by the network guard:")
    assert result.hint is None
    assert fake_net.connects == []


def test_firewall_hints_and_cards() -> None:
    ctx = make_ctx(LinuxHost, {"host": "10.0.0.20", "port": 9101})
    [hint] = LinuxHost().firewall_hints(ctx)
    assert (hint.protocol, hint.port, hint.destination) == ("tcp", 9101, "10.0.0.20")
    assert [c.card_type for c in LinuxHost().cards()] == ["linux-host"]


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
                type_id="linux_host",
                name=name,
                site=None,
                config={"host": host, "port": 9100, "scheme": "http"},
                secrets=None,
            )
        )
        db.commit()


def test_api_to_service_discovery(admin: TestClient, engine: Engine, fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    cfg = {"host": "node.example.internal"}
    tested = admin.post("/api/v1/instances/test", json={"type_id": "linux_host", "config": cfg})
    assert tested.json()["ok"] is True
    created = admin.post(
        "/api/v1/instances",
        json={"type_id": "linux_host", "name": "lab-host-1", "site": "lab", "config": cfg},
    )
    assert created.status_code == 201, created.text
    instance_id = created.json()["id"]
    assert created.json()["config"] == {
        "host": "node.example.internal",
        "port": 9100,
        "scheme": "http",
    }

    # Left out of service discovery: it resolves to the metadata address.
    store_directly(engine, "sneaky", "metadata.example.internal")

    r = admin.get("/sd/targets", headers={"Authorization": f"Bearer {PROM_TOKEN}"})
    assert r.status_code == 200, r.text
    assert r.json() == [
        {
            "targets": ["node.example.internal:9100"],
            "labels": {
                "__metrics_path__": "/metrics",
                "__scheme__": "http",
                "vantage_instance_id": instance_id,
                "vantage_instance": "lab-host-1",
                "vantage_integration": "linux_host",
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
            json={"type_id": "linux_host", "name": f"bad-{host}", "config": {"host": host}},
        )
        assert r.status_code == 422, host
    assert fake_net.connects == []

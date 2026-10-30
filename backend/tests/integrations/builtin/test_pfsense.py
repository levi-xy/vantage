"""pfSense via Telegraf (M4c): schema, scrape targets, parsing, connection test, and the
path API → /sd/targets. No real network: ``fake_net`` keeps the real SSRF guard and fakes
only DNS and sockets.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpcore
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine

from app.collectors.scheduler import polls
from app.core.prometheus_auth import PrometheusToken
from app.integrations import instance_config as ic
from app.integrations.base import TestResult
from app.integrations.builtin.pfsense import PfSense, PfSenseConfig
from app.integrations.builtin.pfsense import integration as pfsense_module
from app.integrations.builtin.pfsense.integration import describe_telegraf
from tests.integrations.netfake import FakeNetwork, fake_guards, hang, http_response, make_ctx
from tests.test_auth import do_setup, make_client

FIXTURE = (Path(__file__).parent / "fixtures" / "telegraf_pfsense.prom").read_bytes()
LIVE_FIXTURE = (Path(__file__).parent / "fixtures" / "telegraf_pfsense_live.prom").read_bytes()
ADDRESS = ("10.0.0.1", 9273)
PROM_TOKEN = "example-prometheus-token-0123456789abcdef"


async def run_test(**config: Any) -> TestResult:
    cfg = {"host": "fw.example.internal", **config}
    return await PfSense().test_connection(make_ctx(PfSense, cfg))


def assert_read_only(net: FakeNetwork) -> None:
    """Only GET /metrics: nothing that could change the firewall's state."""
    assert net.requests
    for request in net.requests:
        assert (request.method, request.target) == ("GET", "/metrics")


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults() -> None:
    cfg = PfSenseConfig.model_validate({"host": "10.0.0.1"})
    assert (cfg.host, cfg.port, cfg.scheme) == ("10.0.0.1", 9273, "http")
    assert (cfg.interface_names, cfg.hidden_interfaces) == ([], [])


def test_interface_settings_are_cleaned() -> None:
    cfg = PfSenseConfig.model_validate(
        {
            "host": "10.0.0.1",
            "interface_names": [" igc0 =  WAN  uplink ", "lagg0.10=IoT VLAN"],
            "hidden_interfaces": [" igc3 "],
        }
    )
    assert cfg.interface_names == ["igc0=WAN uplink", "lagg0.10=IoT VLAN"]
    assert cfg.hidden_interfaces == ["igc3"]


@pytest.mark.parametrize(
    "config",
    [
        {"host": ""},
        {"host": "http://10.0.0.1"},
        {"host": "10.0.0.1:9273"},
        {"host": "0x7f.1"},
        {"host": "10.0.0.1", "port": 0},
        {"host": "10.0.0.1", "port": 65536},
        {"host": "10.0.0.1", "scheme": "ftp"},
        {"host": "10.0.0.1", "interface_names": ["igc0"]},
        {"host": "10.0.0.1", "interface_names": ["igc0="]},
        {"host": "10.0.0.1", "interface_names": ["igc 0=WAN"]},
        {"host": "10.0.0.1", "interface_names": ["igc0=" + "x" * 33]},
        {"host": "10.0.0.1", "hidden_interfaces": ["igc0; rm"]},
        {},
    ],
)
def test_schema_rejects(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PfSenseConfig.model_validate(config)


def test_no_secrets_and_host_is_checked_on_save() -> None:
    stored, secrets = ic.split(PfSenseConfig.model_validate({"host": "fw.example.internal"}))
    assert secrets == {}
    assert ic.configured_hosts(stored) == [("host", "fw.example.internal", 9273, None)]


# --- scrape targets --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "address", "scheme"),
    [
        ({"host": "10.0.0.1"}, "10.0.0.1:9273", "http"),
        ({"host": "fw.example.internal", "port": 9274}, "fw.example.internal:9274", "http"),
        ({"host": "fd00::1", "scheme": "https"}, "[fd00::1]:9273", "https"),
    ],
)
def test_scrape_targets(config: dict[str, Any], address: str, scheme: str) -> None:
    [target] = PfSense().scrape_targets(make_ctx(PfSense, config))
    assert (target.address, target.metrics_path, target.scheme) == (address, "/metrics", scheme)


def test_scrape_only() -> None:
    assert not polls(PfSense)


# --- parsing ---------------------------------------------------------------------------


def test_describe_telegraf() -> None:
    assert describe_telegraf(FIXTURE.decode()) == (2, 2)


def test_describe_live_capture_counts_down_gateway() -> None:
    # 3 ping targets, one with 100% loss and therefore no RTT series.
    down = [line for line in LIVE_FIXTURE.decode().splitlines() if "192.0.2.254" in line]
    assert down and not any(line.startswith("ping_average_response_ms") for line in down)
    assert any(
        line.startswith('cpu_usage_idle{cpu="cpu-total"')
        for line in LIVE_FIXTURE.decode().splitlines()
    )
    assert describe_telegraf(LIVE_FIXTURE.decode()) == (1, 3)


# Telegraf's ping input for a target that never answered: no RTT families, no ping_ttl.
DOWN_GATEWAY_ONLY = (
    "# TYPE ping_packets_received untyped\n"
    'ping_packets_received{host="fw-example",url="192.0.2.254"} 0\n'
    "# TYPE ping_packets_transmitted untyped\n"
    'ping_packets_transmitted{host="fw-example",url="192.0.2.254"} 3\n'
    "# TYPE ping_percent_packet_loss untyped\n"
    'ping_percent_packet_loss{host="fw-example",url="192.0.2.254"} 100\n'
    "# TYPE ping_result_code untyped\n"
    'ping_result_code{host="fw-example",url="192.0.2.254"} 2\n'
)


def test_describe_ignores_ping_without_url() -> None:
    page = '# TYPE ping_result_code untyped\nping_result_code{url=""} 1\nping_result_code 1\n'
    assert describe_telegraf(page) == (0, 0)


def test_describe_only_down_gateway() -> None:
    assert describe_telegraf(DOWN_GATEWAY_ONLY) == (0, 1)


@pytest.mark.parametrize(
    "family", ["ping_percent_packet_loss", "ping_packets_transmitted", "ping_result_code"]
)
def test_describe_gateway_from_any_always_present_family(family: str) -> None:
    page = f'# TYPE {family} untyped\n{family}{{url="192.0.2.254"}} 1\n'
    assert describe_telegraf(page) == (0, 1)


@pytest.mark.parametrize(
    "text",
    [
        "<html><body>Not metrics</body></html>",
        "# TYPE node_load1 gauge\nnode_load1 0.42\n",
        "",
    ],
)
def test_describe_rejects_other_pages(text: str) -> None:
    assert describe_telegraf(text) is None


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    result = await run_test()
    assert result.ok
    assert result.message == "Telegraf metrics: 2 interfaces, 2 gateways"
    assert fake_net.connects == [ADDRESS]
    assert "authorization" not in fake_net.requests[0].headers
    assert_read_only(fake_net)


async def test_connection_live_capture(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, LIVE_FIXTURE))
    result = await run_test()
    assert result.ok
    assert result.message == "Telegraf metrics: 1 interfaces, 3 gateways"
    assert "192.0.2" not in result.message and "vtnet0" not in result.message
    assert_read_only(fake_net)


async def test_connection_single_down_gateway(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, DOWN_GATEWAY_ONLY.encode()))
    result = await run_test()
    assert result.ok
    assert result.message == "Telegraf metrics: 0 interfaces, 1 gateways"


async def test_connection_never_quotes_device_text(fake_net: FakeNetwork) -> None:
    page = (
        b"# TYPE net_bytes_recv counter\n"
        b'net_bytes_recv{interface="example-token"} 1\n'
        b"# TYPE ping_average_response_ms gauge\n"
        b'ping_average_response_ms{url="example-secret"} 1\n'
    )
    fake_net.serve(*ADDRESS, http_response(200, page))
    result = await run_test()
    assert result.ok
    assert result.message == "Telegraf metrics: 1 interfaces, 1 gateways"
    assert "example-token" not in result.model_dump_json()
    assert "example-secret" not in result.model_dump_json()


async def test_connection_https_verifies_tls(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    assert (await run_test(scheme="https")).ok
    [(server_name, context)] = fake_net.tls
    assert server_name == "fw.example.internal"
    assert context.check_hostname


async def test_connection_not_telegraf(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, b"<html>router admin page</html>"))
    result = await run_test()
    assert not result.ok
    assert result.message == (
        "fw.example.internal:9273 answered, but not with Telegraf firewall metrics"
    )
    assert result.hint is None


async def test_connection_http_error(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(404, b"secret-looking body", reason="Not Found"))
    result = await run_test()
    assert not result.ok
    assert result.message == "fw.example.internal:9273/metrics answered HTTP 404"
    assert "secret-looking" not in result.message


async def test_connection_body_too_large(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pfsense_module, "MAX_BODY_BYTES", 100)
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
    assert "fw.example.internal:9273" in result.message
    assert result.hint is not None
    assert (result.hint.protocol, result.hint.port, result.hint.destination) == (
        "tcp",
        9273,
        "fw.example.internal",
    )


async def test_connection_own_timeout_gives_firewall_hint(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pfsense_module, "TIMEOUT_SECONDS", 0.2)
    fake_net.serve(*ADDRESS, hang)
    result = await run_test()
    assert not result.ok
    assert result.message == "timed out waiting for fw.example.internal:9273"
    assert result.hint is not None and result.hint.port == 9273


async def test_connection_refused_by_guard(fake_net: FakeNetwork) -> None:
    result = await run_test(host="metadata.example.internal")
    assert not result.ok
    assert result.message.startswith("refused by the network guard:")
    assert result.hint is None
    assert fake_net.connects == []


def test_firewall_hints_and_cards() -> None:
    ctx = make_ctx(PfSense, {"host": "10.0.0.1", "port": 9274})
    [hint] = PfSense().firewall_hints(ctx)
    assert (hint.protocol, hint.port, hint.destination) == ("tcp", 9274, "10.0.0.1")
    assert "Prometheus" in hint.reason
    assert [c.card_type for c in PfSense().cards()] == [
        "pfsense-overview",
        "pfsense-interfaces",
        "pfsense-gateways",
    ]


# --- end to end: API → /sd/targets -----------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_net: FakeNetwork) -> Iterator[TestClient]:
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        client.app.state.prometheus_token = PrometheusToken(PROM_TOKEN)  # type: ignore[attr-defined]
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def test_api_to_service_discovery(admin: TestClient, fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, FIXTURE))
    cfg = {"host": "fw.example.internal"}
    tested = admin.post("/api/v1/instances/test", json={"type_id": "pfsense", "config": cfg})
    assert tested.json()["ok"] is True
    created = admin.post(
        "/api/v1/instances",
        json={"type_id": "pfsense", "name": "edge-fw", "site": "lab", "config": cfg},
    )
    assert created.status_code == 201, created.text
    instance_id = created.json()["id"]

    r = admin.get("/sd/targets", headers={"Authorization": f"Bearer {PROM_TOKEN}"})
    assert r.status_code == 200, r.text
    assert r.json() == [
        {
            "targets": ["fw.example.internal:9273"],
            "labels": {
                "__metrics_path__": "/metrics",
                "__scheme__": "http",
                "vantage_instance_id": instance_id,
                "vantage_instance": "edge-fw",
                "vantage_integration": "pfsense",
                "vantage_site": "lab",
            },
        }
    ]
    assert fake_net.connects == [ADDRESS]  # only the connection test connected


def test_api_refuses_denied_host(admin: TestClient, fake_net: FakeNetwork) -> None:
    for host in ("127.0.0.1", "169.254.169.254", "metadata.example.internal"):
        r = admin.post(
            "/api/v1/instances",
            json={"type_id": "pfsense", "name": f"bad-{host}", "config": {"host": host}},
        )
        assert r.status_code == 422, host
    assert fake_net.connects == []

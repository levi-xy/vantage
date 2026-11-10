"""OPNsense: schema, connection test, poll parsing, partial failure, read-only paths, no secret.

Requests go through the real SSRF guard and guarded client; only the socket layer is fake.
Answers are hand-written ``fixtures/opnsense_*.json`` files modelled on the documented
response shapes, with example values only. Fixtures only: not yet checked against a real
OPNsense.
"""

import base64
import inspect
import json
import logging
import ssl
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpcore
import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from pydantic import ValidationError
from sqlalchemy import Engine

from app.collectors.scheduler import PollScheduler, PollSpec
from app.collectors.store import MetricsStore, make_registry
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.integrations import instance_config as ic
from app.integrations.base import Sample, TestResult
from app.integrations.builtin.opnsense import (
    Opnsense,
    OpnsenseAuthFailed,
    OpnsenseConfig,
    OpnsenseNoAccess,
    OpnsenseTimeout,
)
from app.integrations.builtin.opnsense import integration as opn
from app.integrations.registry import Registry
from app.integrations.schema import config_json_schema
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.builtin.test_proxmox import make_ca_pem
from tests.integrations.netfake import (
    FakeNetwork,
    FakeRequest,
    fake_guards,
    http_response,
    make_ctx,
)
from tests.test_auth import do_setup, make_client

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "fw.example.internal"
ADDRESS = "10.0.0.1"
PORT = 443
API_KEY = "example-key-AAAAAAAAAAAAAAAAAAAAAAAA"
API_SECRET = "example-secret-BBBBBBBBBBBBBBBBBBBBBBBB"
AUTH_HEADER = "Basic " + base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
PREFIX = "vantage_opnsense"
TCP_HINT = {
    "protocol": "tcp",
    "port": PORT,
    "destination": HOST,
    "reason": "The Vantage backend calls the OPNsense API (HTTPS) on this port.",
}

P_INFO = "/api/diagnostics/system/system_information"
P_TIME = "/api/diagnostics/system/system_time"
P_RES = "/api/diagnostics/system/system_resources"
P_PF = "/api/diagnostics/firewall/pf_statistics/info"
P_IF = "/api/diagnostics/interface/get_interface_statistics"
P_NAMES = "/api/diagnostics/interface/get_interface_names"
P_GW = "/api/routes/gateway/status"
P_SVC = "/api/core/service/search"


def cfg(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"host": HOST, "api_key": API_KEY, "api_secret": API_SECRET}
    config.update(overrides)
    return config


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"opnsense_{name}.json").read_text())


def closing(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\r\nConnection: close\r\n", 1)


def body(data: Any) -> bytes:
    return http_response(
        200, json.dumps(data).encode(), headers=[("Content-Type", "application/json")]
    )


def status_answer(code: int) -> bytes:
    return http_response(code, b'{"status":"error"}', reason="Error")


def default_answers() -> dict[str, bytes]:
    return {
        P_INFO: body(fixture("system_information")),
        P_TIME: body(fixture("system_time")),
        P_RES: body(fixture("system_resources")),
        P_PF: body(fixture("pf_statistics_info")),
        P_IF: body(fixture("interface_statistics")),
        P_NAMES: body(fixture("interface_names")),
        P_GW: body(fixture("gateway_status")),
        P_SVC: body(fixture("service_search")),
    }


def serve(fake_net: FakeNetwork, **overrides: bytes | Callable[[FakeRequest], Any]) -> None:
    answers: dict[str, Any] = dict(default_answers())
    answers.update(overrides)

    async def handler(request: FakeRequest) -> bytes:
        answer = answers.get(request.target)
        if answer is None:
            return http_response(404, b"{}", reason="Not Found")
        return closing(answer if isinstance(answer, bytes) else answer(request))

    fake_net.serve(ADDRESS, PORT, handler)


async def run_test(config: dict[str, Any]) -> TestResult:
    return await Opnsense().test_connection(make_ctx(Opnsense, config))


async def run_poll(config: dict[str, Any]) -> list[Sample]:
    return await Opnsense().poll(make_ctx(Opnsense, config))


def assert_read_only(fake_net: FakeNetwork) -> None:
    """Only GET, only allowlisted paths, only to the checked address, auth in the header."""
    assert fake_net.requests
    for request in fake_net.requests:
        assert request.method == "GET", request
        assert request.target in opn.ALLOWED_PATHS, request.target
        assert (request.address, request.port) == (ADDRESS, PORT)
        assert request.headers["authorization"] == AUTH_HEADER


def assert_no_secret(*texts: object) -> None:
    for text in texts:
        assert API_KEY not in str(text) and API_SECRET not in str(text)
        assert "example-key" not in str(text) and "example-secret" not in str(text)


def value(samples: list[Sample], metric: str, **labels: str) -> float:
    for s in samples:
        if s.name == f"{PREFIX}_{metric}" and s.labels == labels:
            return s.value
    raise AssertionError(f"no {metric} {labels}")


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults_and_secrets() -> None:
    config = OpnsenseConfig.model_validate(cfg())
    assert config.port == 443 and config.tls_verify() is True  # Hard rule 9
    assert ic.secret_field_keys(OpnsenseConfig) == {"api_key", "api_secret"}
    stored, secrets = ic.split(config)
    assert secrets == {"api_key": API_KEY, "api_secret": API_SECRET}
    assert "api_key" not in stored and "api_secret" not in stored
    schema = config_json_schema(OpnsenseConfig)
    for field in ("api_key", "api_secret"):
        assert schema["properties"][field].get("writeOnly") is True
        assert "default" not in schema["properties"][field]
    assert schema["properties"]["tls_insecure_skip_verify"]["default"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "https://10.0.0.1"},
        {"port": 0},
        {"api_key": "has:colon"},
        {"api_key": "has space"},
        {"api_secret": "example-secret\r\nX-Injected: 1"},
        {"timeout_seconds": 16},
        {"tls_ca_certificate": "not a certificate"},
    ],
)
def test_schema_rejects_without_echoing(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as info:
        OpnsenseConfig.model_validate(cfg(**overrides))
    messages = str(info.value.errors(include_input=False, include_url=False))
    for secret in ("has:colon", "has space", "X-Injected"):
        assert secret not in messages


def test_tls_options() -> None:
    pem = make_ca_pem("OPNsense example")
    pinned = OpnsenseConfig.model_validate(cfg(tls_ca_certificate=pem)).tls_verify()
    assert isinstance(pinned, ssl.SSLContext)
    assert pinned.verify_mode == ssl.CERT_REQUIRED and pinned.check_hostname
    assert OpnsenseConfig.model_validate(cfg(tls_insecure_skip_verify=True)).tls_verify() is False
    with pytest.raises(ValidationError):
        OpnsenseConfig.model_validate(cfg(tls_ca_certificate=pem, tls_insecure_skip_verify=True))


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net)
    result = await run_test(cfg())
    assert result.ok, result
    assert result.message == "OPNsense 24.7.5-amd64: read 8 of 8 API endpoints"
    assert result.hint is None
    assert_read_only(fake_net)
    for server_name, context in fake_net.tls:
        assert server_name == HOST
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert_no_secret(result, caplog.text)


async def test_connection_names_the_missing_privileges(fake_net: FakeNetwork) -> None:
    serve(fake_net, **{P_GW: status_answer(403), P_PF: status_answer(403)})
    result = await run_test(cfg())
    assert result.ok
    assert "read 6 of 8" in result.message
    assert "System: Gateways" in result.message
    assert "Diagnostics: Firewall statistics" in result.message
    assert "Status: Services" not in result.message


async def test_connection_401(fake_net: FakeNetwork) -> None:
    serve(fake_net, **{P_INFO: status_answer(401)})
    result = await run_test(cfg())
    assert not result.ok and "refused the API key (HTTP 401)" in result.message
    assert_no_secret(result)


async def test_connection_403_everywhere(fake_net: FakeNetwork) -> None:
    serve(fake_net, **{path: status_answer(403) for path in opn.ALLOWED_PATHS})
    result = await run_test(cfg())
    assert not result.ok and "can't read any page" in result.message


async def test_connection_not_the_api(fake_net: FakeNetwork) -> None:
    serve(fake_net, **{path: http_response(200, b"<html>") for path in opn.ALLOWED_PATHS})
    result = await run_test(cfg())
    assert not result.ok and "not like the OPNsense API" in result.message


@pytest.mark.parametrize(
    "error", [httpcore.ConnectTimeout("timed out"), httpcore.ConnectError("refused")]
)
async def test_connection_failure_gives_tcp_hint(fake_net: FakeNetwork, error: Exception) -> None:
    fake_net.fail(ADDRESS, PORT, error)
    result = await run_test(cfg())
    assert not result.ok and f"{HOST}:443" in result.message
    assert result.hint is not None and result.hint.model_dump() == TCP_HINT


async def test_connection_refused_by_guard(fake_net: FakeNetwork) -> None:
    result = await run_test(cfg(host="metadata.example.internal"))
    assert not result.ok and result.message.startswith("refused by the network guard")
    assert fake_net.connects == []


# --- poll ------------------------------------------------------------------------------


async def test_poll_parses_the_fixtures(
    fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net)
    samples = await run_poll(cfg())
    assert_read_only(fake_net)
    assert len(fake_net.requests) == 8  # one request per endpoint
    assert value(samples, "uptime_seconds") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert [value(samples, m) for m in ("load1", "load5", "load15")] == [0.25, 0.5, 0.75]
    assert value(samples, "memory_total_bytes") == 4294967296
    assert value(samples, "memory_used_bytes") == 1073741824
    assert value(samples, "pf_states") == 1234
    assert value(samples, "info", version="OPNsense 24.7.5-amd64") == 1
    assert value(samples, "interface_up", interface="vtnet0") == 1  # UP + RUNNING
    assert value(samples, "interface_up", interface="vtnet1") == 0  # no link
    assert value(samples, "interface_receive_bytes", interface="vtnet0") == 1234567
    assert value(samples, "interface_transmit_bytes", interface="vtnet0") == 7654321
    assert value(samples, "interface_info", interface="vtnet0", interface_name="WAN") == 1
    assert not any(s.labels.get("interface") == "lo0" for s in samples)
    assert value(samples, "interfaces") == 2
    assert value(samples, "gateway_up", gateway="WAN_DHCP") == 1
    assert value(samples, "gateway_up", gateway="BACKUP_GW") == 0
    assert value(samples, "gateway_delay_seconds", gateway="WAN_DHCP") == pytest.approx(0.0043)
    assert value(samples, "gateway_loss_percent", gateway="BACKUP_GW") == 100
    assert not any("delay" in s.name and s.labels.get("gateway") == "BACKUP_GW" for s in samples)
    assert value(samples, "gateways") == 2  # the badly named one is skipped
    assert value(samples, "service_running", service="unbound") == 1
    assert value(samples, "service_running", service="dhcpd") == 0
    assert value(samples, "services") == 3
    assert all(
        value(samples, "endpoint_ok", endpoint=k) == 1 for k in (e.key for e in opn.ENDPOINTS)
    )
    # Free text (host name, descriptions) never becomes a label except the capped info ones.
    assert all(s.labels.get("gateway") != "bad name; drop" for s in samples)
    assert_no_secret(caplog.text)


async def test_poll_partial_failure_still_reports_the_rest(
    fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net, **{P_GW: status_answer(403), P_PF: http_response(200, b"nope")})
    samples = await run_poll(cfg())
    assert value(samples, "endpoint_ok", endpoint="gateways") == 0
    assert value(samples, "endpoint_ok", endpoint="pf") == 0
    assert value(samples, "endpoint_ok", endpoint="services") == 1
    assert value(samples, "service_running", service="unbound") == 1
    assert not any(s.name.endswith(("gateway_up", "pf_states")) for s in samples)
    assert "endpoints left out: gateways=403, pf=None" in caplog.text
    assert_no_secret(caplog.text)


async def test_poll_caps(fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opn, "MAX_SERVICES", 1)
    serve(fake_net)
    samples = await run_poll(cfg())
    assert len([s for s in samples if s.name == f"{PREFIX}_service_running"]) == 1
    assert value(samples, "services") == 3
    assert value(samples, "items_omitted", kind="services") == 2


async def test_poll_401_fails(fake_net: FakeNetwork) -> None:
    serve(fake_net, **{P_GW: status_answer(401)})
    with pytest.raises(OpnsenseAuthFailed) as info:
        await run_poll(cfg())
    assert_no_secret(info.value)


async def test_poll_403_everywhere_fails(fake_net: FakeNetwork) -> None:
    serve(fake_net, **{path: status_answer(403) for path in opn.ALLOWED_PATHS})
    with pytest.raises(OpnsenseNoAccess):
        await run_poll(cfg())


async def test_poll_timeout(fake_net: FakeNetwork) -> None:
    fake_net.fail(ADDRESS, PORT, httpcore.ConnectTimeout("timed out"))
    with pytest.raises(OpnsenseTimeout):
        await run_poll(cfg())


# --- read-only -------------------------------------------------------------------------


async def test_get_refuses_a_path_off_the_allowlist(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    config = OpnsenseConfig.model_validate(cfg())
    async with opn._client(make_ctx(Opnsense, cfg()), config) as client:
        for path in (
            "/api/core/firmware/check",
            "/api/core/firmware/update",
            "/api/core/service/restart/unbound",
            "/api/diagnostics/system/system_time?x=1",
        ):
            with pytest.raises(ValueError, match="allowlisted"):
                await opn._get_json(client, path, 5)
    assert fake_net.requests == [] and fake_net.connects == []


def test_allowlist_has_no_state_changing_path() -> None:
    for path in opn.ALLOWED_PATHS:
        assert path.startswith("/api/")
        for word in (
            "start",
            "stop",
            "restart",
            "reconfigure",
            "/set",
            "/add",
            "/del",
            "flush",
            "kill",
            "firmware",
            "stream",
            "reboot",
            "halt",
        ):
            assert word not in path, path
    source = inspect.getsource(opn)
    for word in (
        '"POST"',
        '"PUT"',
        '"DELETE"',
        '"PATCH"',
        ".post(",
        ".put(",
        ".delete(",
        ".patch(",
        ".request(",
    ):
        assert word not in source, word


# --- through the poll collector and the instance API -----------------------------------


async def test_collector_exposes_samples_with_standard_labels(
    fake_net: FakeNetwork, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net)
    box = SecretBox(TEST_MASTER_KEY)
    instance_id = uuid.uuid4()
    stored, secrets = ic.split(OpnsenseConfig.model_validate(cfg()))
    spec = PollSpec(
        instance_id=instance_id,
        type_id="opnsense",
        name="fw-1",
        site="lab",
        updated_at=utcnow(),
        config=stored,
        secrets=ic.encrypt(box, instance_id, secrets),
    )
    store = MetricsStore(down_after_failed_cycles=1)
    scheduler = PollScheduler(
        session_factory=make_session_factory(engine),
        registry=Registry([Opnsense]),
        secret_box=box,
        net_guard_factory=fake_guards,
        store=store,
    )
    store.track(spec.instance_id, spec.labels)
    await scheduler.poll_once(spec, Opnsense(), next_start=0.0)
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "up", status
    text = generate_latest(make_registry(store)).decode()
    std = (
        f'vantage_instance="fw-1",vantage_instance_id="{instance_id}",'
        f'vantage_integration="opnsense",vantage_site="lab"'
    )
    assert f'{PREFIX}_gateway_up{{gateway="WAN_DHCP",{std}}} 1.0' in text
    assert f"# TYPE {PREFIX}_interface_receive_bytes_total counter" in text
    assert_no_secret(text, caplog.text)


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_net: FakeNetwork) -> Iterator[TestClient]:
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def test_api_create_test_and_read_hold_no_secret(admin: TestClient, fake_net: FakeNetwork) -> None:
    serve(fake_net)
    bodies = []
    r = admin.post("/api/v1/instances/test", json={"type_id": "opnsense", "config": cfg()})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    bodies.append(r.text)
    created = admin.post(
        "/api/v1/instances", json={"type_id": "opnsense", "name": "fw-1", "config": cfg()}
    )
    assert created.status_code == 201, created.text
    bodies.append(created.text)
    instance = created.json()
    assert "api_key" not in instance["config"] and "api_secret" not in instance["config"]
    assert instance["secrets_set"] == {"api_key": True, "api_secret": True}
    for path in (
        "/api/v1/instances",
        f"/api/v1/instances/{instance['id']}",
        "/api/v1/integrations",
        "/api/v1/audit",
    ):
        r = admin.get(path)
        assert r.status_code == 200
        bodies.append(r.text)
    cards = admin.get(f"/api/v1/instances/{instance['id']}/cards").json()
    assert [c["card_type"] for c in cards] == [
        "opnsense-overview",
        "opnsense-interfaces",
        "opnsense-gateways",
        "opnsense-services",
    ]
    assert_no_secret(*bodies)
    assert_read_only(fake_net)


def test_api_serves_tcp_hint_on_timeout(admin: TestClient, fake_net: FakeNetwork) -> None:
    fake_net.fail(ADDRESS, PORT, httpcore.ConnectTimeout("timed out"))
    r = admin.post("/api/v1/instances/test", json={"type_id": "opnsense", "config": cfg()})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["hint"] == TCP_HINT
    assert_no_secret(r.text)


def test_firewall_hints_and_static_parts() -> None:
    ctx = make_ctx(Opnsense, cfg(port=8443))
    [hint] = Opnsense().firewall_hints(ctx)
    assert (hint.destination, hint.port) == (HOST, 8443)
    assert Opnsense.category == "firewall" and Opnsense().poll_interval_seconds() == 60

"""HTTP check (M2.5): schema, connection test, poll parsing, collector end to end, and the
security assertions: GET only, every request through the guard, the token never shown.

No real network: ``fake_net`` keeps the real SSRF guard and fakes only the sockets.
"""

import json
import logging
import ssl
import uuid
from collections.abc import Iterator
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
from app.core.net_guard import NetGuardError
from app.integrations import instance_config as ic
from app.integrations.base import TestResult
from app.integrations.builtin.http_check import HttpCheck, HttpCheckConfig
from app.integrations.registry import Registry
from app.integrations.schema import config_json_schema
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import (
    FakeNetwork,
    fake_guards,
    hang,
    http_response,
    make_ctx,
)
from tests.test_auth import do_setup, make_client

TOKEN = "example-token-http-check-7f3a"
URL = "http://web.example.internal/health?probe=1"
ADDRESS = ("10.0.0.10", 80)


def config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"url": URL, "bearer_token": TOKEN}
    cfg.update(overrides)
    return cfg


async def run_test(**overrides: Any) -> TestResult:
    return await HttpCheck().test_connection(make_ctx(HttpCheck, config(**overrides)))


def assert_no_token(*texts: str) -> None:
    for text in texts:
        assert TOKEN not in text
        assert "**********" not in text


def assert_read_only(net: FakeNetwork) -> None:
    """Only GET, only the configured path: nothing that could change device state."""
    for request in net.requests:
        assert request.method == "GET"
        assert request.target == "/health?probe=1"


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults() -> None:
    cfg = HttpCheckConfig.model_validate({"url": " http://10.0.0.10/ "})
    assert cfg.url == "http://10.0.0.10/"
    assert cfg.expected_status is None
    assert cfg.verify_tls is True
    assert cfg.timeout_seconds == 10
    assert cfg.bearer_token is None


@pytest.mark.parametrize(
    "url",
    [
        "http://[fd00::10]:8080/status",
        "https://web.example.internal/",
        "http://xn--e1afmkfd.example/",
    ],
)
def test_schema_accepts(url: str) -> None:
    assert HttpCheckConfig.model_validate({"url": url}).url == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://10.0.0.10/",
        "file:///etc/passwd",
        "http:///nohost",
        "not a url",
        "http://0x7f.1/",  # ambiguous numeric host
        "http://2130706433/",
        "http://admin:example-password@10.0.0.10/",
        "http://admin@10.0.0.10/",
        "http://" + "a" * 2050 + ".example/",
    ],
)
def test_schema_rejects_url(url: str) -> None:
    with pytest.raises(ValidationError) as info:
        HttpCheckConfig.model_validate({"url": url})
    # The validator's own message never repeats the URL (it could carry a password).
    for error in info.value.errors():
        assert "example-password" not in error["msg"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_status": 99},
        {"expected_status": 600},
        {"timeout_seconds": 0},
        {"timeout_seconds": 16},
        {"bearer_token": 123},
    ],
)
def test_schema_rejects_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        HttpCheckConfig.model_validate(config(**overrides))


def test_url_description_warns_it_is_stored_in_plaintext() -> None:
    # Query strings are allowed, so the form must tell the admin where credentials go.
    served = config_json_schema(HttpCheckConfig)["properties"]["url"]["description"]
    assert "plaintext" in served
    assert "query string" in served
    assert "bearer token" in served
    # And query strings really are accepted.
    url = "http://10.0.0.10/health?verbose=1"
    assert HttpCheckConfig.model_validate({"url": url}).url == url


def test_token_is_a_secret_and_url_is_a_host_field() -> None:
    model = HttpCheckConfig.model_validate(config())
    stored, secrets = ic.split(model)
    assert secrets == {"bearer_token": TOKEN}
    assert "bearer_token" not in stored
    # The SSRF pre-check on save finds the URL by its field name.
    assert ic.configured_hosts(stored) == [("url", "web.example.internal", 80, "http")]


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200, b"<html>large page</html>"))
    result = await run_test()
    assert result.ok
    assert result.message.startswith("HTTP 200 in ")
    assert result.hint is None
    # Went to the address the guard checked, with the configured Host and the token.
    assert fake_net.connects == [ADDRESS]
    [request] = fake_net.requests
    assert request.headers["host"] == "web.example.internal"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["user-agent"] == "Vantage"
    assert_read_only(fake_net)


async def test_connection_without_token_sends_no_authorization(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(204))
    assert (await run_test(bearer_token=None)).ok
    assert "authorization" not in fake_net.requests[0].headers


async def test_connection_unexpected_status(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(503, reason="Service Unavailable"))
    result = await run_test()
    assert not result.ok
    assert result.message == "HTTP 503, expected 2xx or 3xx"
    assert result.hint is None  # it answered: not a firewall problem


async def test_connection_expected_status(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(401, reason="Unauthorized"))
    assert (await run_test(expected_status=401)).ok
    result = await run_test(expected_status=200)
    assert not result.ok
    assert result.message == "HTTP 401, expected 200"


async def test_connection_https_verifies_tls_by_default(fake_net: FakeNetwork) -> None:
    fake_net.serve("10.0.0.10", 443, http_response(200))
    assert (await run_test(url="https://web.example.internal/health?probe=1")).ok
    [(server_name, context)] = fake_net.tls
    assert server_name == "web.example.internal"
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


async def test_connection_verify_tls_off(fake_net: FakeNetwork) -> None:
    fake_net.serve("10.0.0.10", 443, http_response(200))
    assert (await run_test(url="https://10.0.0.10/health?probe=1", verify_tls=False)).ok
    [(_, context)] = fake_net.tls
    assert context.verify_mode == ssl.CERT_NONE


async def test_connection_tls_failure(fake_net: FakeNetwork) -> None:
    fake_net.serve("10.0.0.10", 443, http_response(200))
    # What httpcore raises for a failed handshake: the ssl module's text, no cause.
    fake_net.tls_error = httpcore.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate (_ssl.c:1000)"
    )
    result = await run_test(url="https://web.example.internal/health?probe=1")
    assert not result.ok
    assert "TLS handshake with web.example.internal:443 failed" in result.message
    assert result.hint is None


@pytest.mark.parametrize(
    "error", [httpcore.ConnectTimeout("timed out"), httpcore.ConnectError("refused")]
)
async def test_connection_failure_gives_firewall_hint(
    fake_net: FakeNetwork, error: Exception
) -> None:
    fake_net.fail(*ADDRESS, error)
    result = await run_test()
    assert not result.ok
    assert "web.example.internal:80" in result.message
    assert result.hint is not None
    assert result.hint.model_dump() == {
        "protocol": "tcp",
        "port": 80,
        "destination": "web.example.internal",
        "reason": "The Vantage backend requests the checked URL on this port.",
    }


async def test_connection_overall_timeout_gives_firewall_hint(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, hang)
    result = await run_test(timeout_seconds=1)
    assert not result.ok
    assert result.message == "timed out waiting for web.example.internal:80"
    assert result.hint is not None and result.hint.port == 80


async def test_connection_refused_by_guard(fake_net: FakeNetwork) -> None:
    result = await run_test(url="http://metadata.example.internal/latest/meta-data")
    assert not result.ok
    assert result.message.startswith("refused by the network guard:")
    assert "169.254.169.254" in result.message
    assert result.hint is None
    assert fake_net.connects == []  # never connected


async def test_connection_loopback_literal_refused(fake_net: FakeNetwork) -> None:
    result = await run_test(url="http://127.0.0.1:8080/")
    assert not result.ok and "network guard" in result.message
    assert fake_net.connects == []


async def test_connection_unknown_host(fake_net: FakeNetwork) -> None:
    result = await run_test(url="http://missing.example.internal/")
    assert not result.ok
    assert "could not resolve missing.example.internal" in result.message


async def test_connection_protocol_error(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, b"this is not HTTP\r\n\r\n")
    result = await run_test()
    assert not result.ok
    assert result.message.startswith("request to web.example.internal:80 failed (")


async def test_unrecognised_errors_propagate(fake_net: FakeNetwork) -> None:
    # Anything connection_failure doesn't know is left to the instance API, which
    # reports only the exception type.
    fake_net.fail(*ADDRESS, RuntimeError(f"boom {TOKEN}"))
    with pytest.raises(RuntimeError):
        await run_test()


async def test_messages_never_echo_error_text(fake_net: FakeNetwork) -> None:
    # Even when a response body or an exception's text holds the token, a message
    # never repeats it: messages are built from fixed text, host and port only.
    results: list[TestResult] = []
    fake_net.serve(*ADDRESS, http_response(500, body=TOKEN.encode()))
    results.append(await run_test())
    fake_net.serve(*ADDRESS, b"garbage " + TOKEN.encode())
    results.append(await run_test())
    fake_net.fail(*ADDRESS, httpcore.ConnectTimeout(f"timeout {TOKEN}"))
    results.append(await run_test())
    fake_net.fail(*ADDRESS, httpcore.ConnectError(f"refused {TOKEN}"))
    results.append(await run_test())
    assert_no_token(*(r.model_dump_json() for r in results))


async def test_token_never_logged(fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture) -> None:
    # DEBUG includes httpx/httpcore request tracing: the Authorization header is never in it.
    caplog.set_level(logging.DEBUG)
    fake_net.serve(*ADDRESS, http_response(200))
    await run_test()
    await poll()
    fake_net.fail(*ADDRESS, httpcore.ConnectError("refused"))
    await run_test()
    await run_test(url="http://metadata.example.internal/")
    assert caplog.records  # the tracing really was captured
    assert_no_token(caplog.text)


# --- poll ------------------------------------------------------------------------------


async def poll(**overrides: Any) -> dict[str, float]:
    samples = await HttpCheck().poll(make_ctx(HttpCheck, config(**overrides)))
    for sample in samples:
        assert sample.type == "gauge"
        assert sample.labels == {}  # the collector adds the standard labels
    return {s.name: s.value for s in samples}


async def test_poll_success(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(200))
    values = await poll()
    assert values.keys() == {
        "vantage_http_check_success",
        "vantage_http_check_status_code",
        "vantage_http_check_duration_seconds",
    }
    assert values["vantage_http_check_success"] == 1
    assert values["vantage_http_check_status_code"] == 200
    assert 0 <= values["vantage_http_check_duration_seconds"] < 5
    assert_read_only(fake_net)


async def test_poll_wrong_status_is_a_result(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, http_response(503, reason="Service Unavailable"))
    values = await poll()
    assert values["vantage_http_check_success"] == 0
    assert values["vantage_http_check_status_code"] == 503


async def test_poll_does_not_follow_redirects(fake_net: FakeNetwork) -> None:
    # The redirect target would get the token if it were followed.
    fake_net.serve(
        *ADDRESS, http_response(302, reason="Found", headers=[("Location", "http://10.0.0.99/")])
    )
    fake_net.serve("10.0.0.99", 80, http_response(200))
    values = await poll()
    assert values["vantage_http_check_status_code"] == 302
    assert values["vantage_http_check_success"] == 1  # 3xx counts by default
    assert fake_net.connects == [ADDRESS]
    assert (await poll(expected_status=200))["vantage_http_check_success"] == 0


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (httpcore.ConnectError("refused"), "ConnectError"),
        (httpcore.ConnectTimeout("timed out"), "ConnectTimeout"),
    ],
)
async def test_poll_raises_without_a_response(
    fake_net: FakeNetwork, error: Exception, raised: str
) -> None:
    fake_net.fail(*ADDRESS, error)
    with pytest.raises(Exception) as info:
        await poll()
    assert type(info.value).__name__ == raised


async def test_poll_refused_by_guard(fake_net: FakeNetwork) -> None:
    with pytest.raises(NetGuardError):
        await poll(url="http://metadata.example.internal/")
    assert fake_net.connects == []


async def test_poll_times_out(fake_net: FakeNetwork) -> None:
    fake_net.serve(*ADDRESS, hang)
    with pytest.raises(TimeoutError):
        await poll(timeout_seconds=1)


# --- through the poll collector --------------------------------------------------------


def spec_for(cfg: dict[str, Any]) -> tuple[PollSpec, SecretBox]:
    box = SecretBox(TEST_MASTER_KEY)
    instance_id = uuid.uuid4()
    stored, secrets = ic.split(HttpCheckConfig.model_validate(cfg))
    spec = PollSpec(
        instance_id=instance_id,
        type_id="http_check",
        name="web-1",
        site="lab",
        updated_at=utcnow(),
        config=stored,
        secrets=ic.encrypt(box, instance_id, secrets),
    )
    return spec, box


async def collect(engine: Engine, cfg: dict[str, Any]) -> tuple[MetricsStore, PollSpec]:
    spec, box = spec_for(cfg)
    store = MetricsStore()
    scheduler = PollScheduler(
        session_factory=make_session_factory(engine),
        registry=Registry([HttpCheck]),
        secret_box=box,
        net_guard_factory=fake_guards,
        store=store,
    )
    store.track(spec.instance_id, spec.labels)
    await scheduler.poll_once(spec, HttpCheck())
    return store, spec


async def test_collector_exposes_samples_with_standard_labels(
    fake_net: FakeNetwork, engine: Engine
) -> None:
    fake_net.serve(*ADDRESS, http_response(200))
    store, spec = await collect(engine, config())
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "up"
    text = generate_latest(make_registry(store)).decode()
    labels = (
        f'vantage_instance="web-1",vantage_instance_id="{spec.instance_id}",'
        'vantage_integration="http_check",vantage_site="lab"'
    )
    assert f"vantage_http_check_success{{{labels}}} 1.0" in text
    assert f"vantage_http_check_status_code{{{labels}}} 200.0" in text
    assert f"vantage_instance_up{{{labels}}} 1.0" in text
    assert_no_token(text)
    # The decrypted token reached the device, and only the device.
    assert fake_net.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


async def test_collector_marks_unreachable_check_down(
    fake_net: FakeNetwork, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake_net.fail(*ADDRESS, httpcore.ConnectError("refused"))
    store, spec = await collect(engine, config())
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error is not None
    text = generate_latest(make_registry(store)).decode()
    assert "vantage_http_check_success" not in text
    assert 'vantage_instance_up{vantage_instance="web-1"' in text
    assert_no_token(text, status.error, caplog.text)


# --- through the instance API ----------------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_net: FakeNetwork) -> Iterator[TestClient]:
    # The built-in registry, as in production; only DNS and sockets are fake.
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def instance_body(**overrides: Any) -> dict[str, Any]:
    return {"type_id": "http_check", "name": "web-1", "site": "lab", "config": config(**overrides)}


def test_api_create_test_and_read_never_show_the_token(
    admin: TestClient, fake_net: FakeNetwork
) -> None:
    fake_net.serve(*ADDRESS, http_response(200))
    r = admin.post("/api/v1/instances/test", json={"type_id": "http_check", "config": config()})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    created = admin.post("/api/v1/instances", json=instance_body())
    assert created.status_code == 201, created.text
    instance_id = created.json()["id"]
    texts = [r.text, created.text]
    for path in ("/api/v1/instances", f"/api/v1/instances/{instance_id}"):
        texts.append(admin.get(path).text)
    tested = admin.post(f"/api/v1/instances/{instance_id}/test")
    assert tested.status_code == 200 and tested.json()["ok"] is True
    texts.append(tested.text)
    cards = admin.get(f"/api/v1/instances/{instance_id}/cards")
    assert cards.json() == [
        {"card_type": "http-check", "title": "HTTP check", "default_size": [2, 2]}
    ]
    texts.append(admin.get("/api/v1/integrations").text)
    assert_no_token(*texts)
    assert json.loads(created.text)["config"]["url"] == URL


def test_api_refuses_url_pointing_at_metadata(admin: TestClient, fake_net: FakeNetwork) -> None:
    r = admin.post("/api/v1/instances", json=instance_body(url="http://metadata.example.internal/"))
    assert r.status_code == 422
    assert fake_net.connects == []
    assert_no_token(r.text)


def test_api_refuses_url_with_credentials(admin: TestClient) -> None:
    r = admin.post(
        "/api/v1/instances", json=instance_body(url="http://admin:example-password@10.0.0.10/")
    )
    assert r.status_code == 422
    assert "example-password" not in r.text


# --- static parts ----------------------------------------------------------------------


def test_firewall_hints_and_cards() -> None:
    ctx = make_ctx(HttpCheck, config(url="https://[fd00::10]:8443/x"))
    [hint] = HttpCheck().firewall_hints(ctx)
    assert (hint.protocol, hint.port, hint.destination) == ("tcp", 8443, "fd00::10")
    assert HttpCheck().poll_interval_seconds() == 60
    assert [c.card_type for c in HttpCheck().cards()] == ["http-check"]

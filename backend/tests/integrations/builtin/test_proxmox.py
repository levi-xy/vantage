"""Proxmox VE: schema, connection test, poll parsing, read-only paths, no secret anywhere.

Requests go through the real SSRF guard and the real guarded client; only the socket
layer is fake (``fake_net``). Answers are the hand-written ``fixtures/proxmox_*.json``
files, in the shape of Proxmox VE 8 responses, with example values only.
"""

import asyncio
import datetime as dt
import inspect
import json
import logging
import ssl
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpcore
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine

from app.collectors.scheduler import MAX_POLL_TIMEOUT_SECONDS, PollScheduler, PollSpec
from app.collectors.store import MetricsStore, make_registry
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.core.net_guard import DEFAULT_TOTAL_TIMEOUT, NetGuardError
from app.integrations import instance_config as ic
from app.integrations.base import Sample, TestResult
from app.integrations.builtin.proxmox import (
    Proxmox,
    ProxmoxAuthFailed,
    ProxmoxBadAnswer,
    ProxmoxConfig,
    ProxmoxForbidden,
    ProxmoxPollTooSlow,
    ProxmoxRequestFailed,
    ProxmoxTimeout,
    ProxmoxTlsUntrusted,
    ProxmoxUnreachable,
)
from app.integrations.builtin.proxmox import integration as pve
from app.integrations.registry import Registry
from app.integrations.schema import config_json_schema
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import (
    FakeNetwork,
    FakeRequest,
    fake_guards,
    hang,
    http_response,
    make_ctx,
)
from tests.test_auth import do_setup, make_client

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "pve.example.internal"
ADDRESS = "10.0.0.50"
PORT = 8006
TOKEN_ID = "vantage@pve!monitoring"
TOKEN_SECRET = "example-token-00000000-0000-0000-0000-000000000000"
AUTH_HEADER = f"PVEAPIToken={TOKEN_ID}={TOKEN_SECRET}"
PREFIX = "vantage_proxmox"
GUEST_TEXT = ("example-web", "example-db", "example-dns", "example-new", "example-template")
TCP_HINT = {
    "protocol": "tcp",
    "port": PORT,
    "destination": HOST,
    "reason": "The Vantage backend calls the Proxmox VE API (HTTPS) on this port.",
}
OWN_LABELS = {"node", "vmid", "guest_type", "storage"}
# The maintainer's label exception (INTEGRATIONS.md section 9): device text, info series only.
FREE_TEXT_LABELS = {"sys_name", "sys_descr", "if_descr", "if_alias", "guest_name"}

V = "/api2/json"


def cfg(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"host": HOST, "token_id": TOKEN_ID, "token_secret": TOKEN_SECRET}
    config.update(overrides)
    return config


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"proxmox_{name}.json").read_text())


def closing(raw: bytes) -> bytes:
    """The fake network answers one request per connection: tell the client so."""
    return raw.replace(b"\r\n", b"\r\nConnection: close\r\n", 1)


def body(data: Any) -> bytes:
    return http_response(
        200, json.dumps(data).encode(), headers=[("Content-Type", "application/json")]
    )


def default_answers() -> dict[str, bytes]:
    status = fixture("node_status")
    storage = fixture("node_storage")
    return {
        f"{V}/version": body(fixture("version")),
        f"{V}/nodes": body(fixture("nodes")),
        f"{V}/cluster/resources?type=vm": body(fixture("cluster_resources_vm")),
        f"{V}/nodes/pve-example/status": body(status),
        f"{V}/nodes/pve-example/storage": body(storage),
        f"{V}/nodes/pve-example-2/status": body(status),
        f"{V}/nodes/pve-example-2/storage": body(storage),
    }


Answer = bytes | Callable[[FakeRequest], Any]


def serve(fake_net: FakeNetwork, **overrides: Answer) -> dict[str, Answer]:
    """Serve the API on the checked address. ``overrides`` replace answers by path,
    keyed by the path's last parts (``nodes``, ``version``, ...) or the full target."""
    answers: dict[str, Answer] = dict(default_answers())
    for key, value in overrides.items():
        answers[_target(key)] = value

    async def handler(request: FakeRequest) -> bytes:
        answer = answers.get(request.target)
        if answer is None:
            return http_response(501, b'{"data":null}', reason="Not Implemented")
        if isinstance(answer, bytes):
            return closing(answer)
        result = answer(request)
        return closing(result if isinstance(result, bytes) else await result)

    fake_net.serve(ADDRESS, PORT, handler)
    return answers


def _target(key: str) -> str:
    return {
        "version": f"{V}/version",
        "nodes": f"{V}/nodes",
        "guests": f"{V}/cluster/resources?type=vm",
        "status1": f"{V}/nodes/pve-example/status",
        "storage1": f"{V}/nodes/pve-example/storage",
        "status2": f"{V}/nodes/pve-example-2/status",
        "storage2": f"{V}/nodes/pve-example-2/storage",
    }.get(key, key)


def status_answer(code: int) -> bytes:
    return http_response(code, b'{"data":null}', reason="Error")


async def run_test(config: dict[str, Any]) -> TestResult:
    return await Proxmox().test_connection(make_ctx(Proxmox, config))


async def run_poll(config: dict[str, Any]) -> list[Sample]:
    return await Proxmox().poll(make_ctx(Proxmox, config))


def assert_read_only(fake_net: FakeNetwork) -> None:
    """Only GET, only allowlisted paths, only to the checked address, token in the
    header and nowhere else."""
    assert fake_net.requests
    for request in fake_net.requests:
        assert request.method == "GET", request
        assert pve.allowed_path(request.target), request.target
        assert (request.address, request.port) == (ADDRESS, PORT)
        assert request.headers["authorization"] == AUTH_HEADER
        assert TOKEN_SECRET not in request.target


def assert_no_secret(*texts: object) -> None:
    for text in texts:
        assert TOKEN_SECRET not in str(text)
        assert "example-token" not in str(text)


def index(samples: list[Sample]) -> dict[tuple[str, tuple[tuple[str, str], ...]], Sample]:
    return {
        (s.name.removeprefix(PREFIX + "_"), tuple(sorted(s.labels.items()))): s for s in samples
    }


def value(samples: list[Sample], metric: str, **labels: str) -> float:
    key = (metric, tuple(sorted(labels.items())))
    return index(samples)[key].value


def make_ca_pem(common_name: str = "Proxmox Virtual Environment") -> str:
    """A throwaway self-signed CA, generated per test run: no key is ever stored."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults() -> None:
    config = ProxmoxConfig.model_validate(cfg())
    assert config.port == 8006
    assert config.tls_ca_certificate is None
    assert config.tls_insecure_skip_verify is False  # Hard rule 9: verification on
    assert config.tls_verify() is True
    assert config.timeout_seconds == 10
    assert config.max_guests == 250 and config.max_storages == 100


def test_the_only_secret_is_the_token_secret() -> None:
    assert ic.secret_field_keys(ProxmoxConfig) == {"token_secret"}
    stored, secrets = ic.split(ProxmoxConfig.model_validate(cfg()))
    assert secrets == {"token_secret": TOKEN_SECRET}
    assert "token_secret" not in stored and stored["token_id"] == TOKEN_ID


def test_served_schema_flags_the_secret_write_only() -> None:
    schema = config_json_schema(ProxmoxConfig)
    secret = schema["properties"]["token_secret"]
    assert secret.get("writeOnly") is True
    for key in ("default", "examples", "example", "const", "enum"):
        assert key not in secret
    assert "token_secret" in schema["required"]
    insecure = schema["properties"]["tls_insecure_skip_verify"]
    assert insecure["default"] is False
    assert insecure["title"].startswith("INSECURE")


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"host": "10.0.0.50", "port": 443},
        {"host": "fd00::50"},
        {"token_id": "root@pam!vantage"},
        {"token_id": "monitor@example-ldap!read.only_1"},
        {"tls_insecure_skip_verify": True},
        {"tls_ca_certificate": ""},
        {"timeout_seconds": 15, "max_guests": 500, "max_storages": 200},
    ],
)
def test_schema_accepts(overrides: dict[str, Any]) -> None:
    ProxmoxConfig.model_validate(cfg(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "https://10.0.0.50"},
        {"port": 0},
        {"token_id": "vantage@pve"},  # no token name
        {"token_id": "vantage!monitoring"},  # no realm
        {"token_id": "van tage@pve!monitoring"},
        {"token_id": "vantäge@pve!monitoring"},
        {"token_id": "vantage@pve!monitoring=extra"},
        {"token_secret": "example token with spaces"},
        {"token_secret": "example-token\r\nX-Injected: 1"},
        {"token_secret": "x" * 257},
        {"timeout_seconds": 16},
        {"max_guests": 501},
        {"max_storages": 0},
        {"tls_ca_certificate": "not a certificate"},
    ],
)
def test_schema_rejects(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as info:
        ProxmoxConfig.model_validate(cfg(**overrides))
    secret = overrides.get("token_secret")
    if secret is not None:
        # What the API serves: the messages, never the input (pydantic's own str()
        # shows the input; the instance API leaves it out, see the API test below).
        messages = str(info.value.errors(include_input=False, include_url=False))
        assert secret not in messages
        assert "printable ASCII without spaces" in messages


def test_ca_certificate_is_normalised_and_pinned() -> None:
    pem = make_ca_pem()
    messy = "\r\n  " + pem.replace("\n", "\r\n") + "\r\n\r\n"
    config = ProxmoxConfig.model_validate(cfg(tls_ca_certificate=messy))
    assert config.tls_ca_certificate is not None
    assert config.tls_ca_certificate.startswith("-----BEGIN CERTIFICATE-----\n")
    assert "\r" not in config.tls_ca_certificate
    context = config.tls_verify()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    # Only the pasted certificate is trusted, not the system store.
    assert len(context.get_ca_certs()) == 1
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN CERTIFICATE-----" + " " * 16_000,
        "-----BEGIN CERTIFICATE-----\n" + " \n" * 8_000,
        "-----BEGIN CERTIFICATE-----" + "\t \r\n" * 4_000,
        "-----BEGIN CERTIFICATE-----\n" + "A" * 16_000,
        "-----BEGIN CERTIFICATE-----\nQUFB\n" + " " * 16_000 + "-----END CERTIFICATE",
    ],
)
def test_ca_parse_is_linear_on_hostile_input(text: str) -> None:
    """A BEGIN line followed by a long run of whitespace and no END used to backtrack for
    minutes. The line-by-line parse refuses it at once."""
    assert len(text) <= pve.MAX_CA_PEM_CHARS
    started = time.perf_counter()
    with pytest.raises(ValidationError):
        ProxmoxConfig.model_validate(cfg(tls_ca_certificate=text))
    assert time.perf_counter() - started < 0.2


def test_ca_field_is_capped() -> None:
    schema = config_json_schema(ProxmoxConfig)["properties"]["tls_ca_certificate"]
    assert pve.MAX_CA_PEM_CHARS == 16_384
    assert 16_384 in {branch.get("maxLength") for branch in schema.get("anyOf", [schema])}
    started = time.perf_counter()
    with pytest.raises(ValidationError):
        ProxmoxConfig.model_validate(
            cfg(tls_ca_certificate="-----BEGIN CERTIFICATE-----" + " " * 100_000)
        )
    assert time.perf_counter() - started < 0.2


def test_pinned_context_is_reused_across_validations() -> None:
    # The collector validates the stored config on every poll.
    pem = make_ca_pem()
    first = ProxmoxConfig.model_validate(cfg(tls_ca_certificate=pem)).tls_verify()
    again = ProxmoxConfig.model_validate(cfg(tls_ca_certificate=pem)).tls_verify()
    other = ProxmoxConfig.model_validate(cfg(tls_ca_certificate=make_ca_pem())).tls_verify()
    assert isinstance(first, ssl.SSLContext)
    assert first is again
    assert other is not first


def test_ca_field_takes_several_certificates() -> None:
    both = make_ca_pem("Example CA 1") + make_ca_pem("Example CA 2")
    context = ProxmoxConfig.model_validate(cfg(tls_ca_certificate=both)).tls_verify()
    assert isinstance(context, ssl.SSLContext)
    assert len(context.get_ca_certs()) == 2


def test_ca_field_refuses_a_private_key_without_echoing_it() -> None:
    # base64 of "example-key"; the marker is split so secret scanners don't flag a fake key.
    marker = "PRIVATE" + " KEY"
    fake_key = f"-----BEGIN {marker}-----\nZXhhbXBsZS1rZXk=\n-----END {marker}-----\n"
    with pytest.raises(ValidationError) as info:
        ProxmoxConfig.model_validate(cfg(tls_ca_certificate=make_ca_pem() + fake_key))
    assert "never a private key" in str(info.value)
    assert "ZXhhbXBsZS1rZXk" not in str(info.value)


def test_ca_field_refuses_a_broken_certificate() -> None:
    broken = "-----BEGIN CERTIFICATE-----\nZXhhbXBsZQ==\n-----END CERTIFICATE-----\n"
    with pytest.raises(ValidationError) as info:
        ProxmoxConfig.model_validate(cfg(tls_ca_certificate=broken))
    assert "not a valid PEM certificate" in str(info.value)


def test_ca_and_insecure_cannot_be_combined() -> None:
    with pytest.raises(ValidationError) as info:
        ProxmoxConfig.model_validate(
            cfg(tls_ca_certificate=make_ca_pem(), tls_insecure_skip_verify=True)
        )
    assert "not both" in str(info.value)


def test_insecure_turns_verification_off() -> None:
    config = ProxmoxConfig.model_validate(cfg(tls_insecure_skip_verify=True))
    assert config.tls_verify() is False


def test_repr_holds_no_secret() -> None:
    config = ProxmoxConfig.model_validate(cfg())
    assert_no_secret(repr(config), str(config), repr(make_ctx(Proxmox, cfg())))


# --- the path allowlist ----------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        f"{V}/version",
        f"{V}/nodes",
        f"{V}/cluster/resources?type=vm",
        f"{V}/nodes/pve-example/status",
        f"{V}/nodes/pve-example-2/storage",
    ],
)
def test_allowlist_accepts(target: str) -> None:
    assert pve.allowed_path(target)


@pytest.mark.parametrize(
    "target",
    [
        f"{V}/cluster/resources",
        f"{V}/cluster/resources?type=vm&x=1",
        f"{V}/cluster/status",
        f"{V}/access/ticket",
        f"{V}/nodes/pve-example/qemu/100/vncproxy",
        f"{V}/nodes/pve-example/qemu/100/termproxy",
        f"{V}/nodes/pve-example/qemu/100/status/stop",
        f"{V}/nodes/pve-example/lxc/200/status/current",
        f"{V}/nodes/pve-example/execute",
        f"{V}/nodes/pve-example/termproxy",
        f"{V}/nodes/pve-example/status/../execute",
        f"{V}/nodes/../status",
        f"{V}/nodes/pve example/status",
        f"{V}/nodes/pve-example/storage/local/content",
        "/api2/extjs/version",
        f"{V}/version?x=1",
    ],
)
def test_allowlist_refuses(target: str) -> None:
    assert not pve.allowed_path(target)


async def test_get_refuses_a_path_off_the_allowlist(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    config = ProxmoxConfig.model_validate(cfg())
    async with pve._client(make_ctx(Proxmox, cfg()), config) as client:
        with pytest.raises(ValueError, match="allowlisted"):
            await pve._get_json(client, f"{V}/nodes/pve-example/qemu/100/vncproxy", 5)
    assert fake_net.requests == [] and fake_net.connects == []


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net)
    result = await run_test(cfg())
    assert result.ok, result
    assert result.message == "Proxmox VE 8.2.4: 3 nodes visible, 2 online"
    assert result.hint is None
    assert [r.target for r in fake_net.requests] == [f"{V}/version", f"{V}/nodes"]
    assert_read_only(fake_net)
    # TLS with verification against the system store, SNI for the configured name.
    assert fake_net.tls
    for server_name, context in fake_net.tls:
        assert server_name == HOST
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert_no_secret(result, caplog.text)


async def test_connection_with_pinned_ca(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    result = await run_test(cfg(tls_ca_certificate=make_ca_pem()))
    assert result.ok
    assert fake_net.tls
    for _, context in fake_net.tls:
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert len(context.get_ca_certs()) == 1


async def test_connection_insecure_skips_verification(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    assert (await run_test(cfg(tls_insecure_skip_verify=True))).ok
    assert fake_net.tls
    assert all(context.verify_mode == ssl.CERT_NONE for _, context in fake_net.tls)


async def test_connection_ipv6_host_and_custom_port(fake_net: FakeNetwork) -> None:
    fake_net.serve(
        "fd00::50",
        8443,
        lambda request: closing(default_answers().get(request.target, status_answer(501))),
    )
    result = await run_test(cfg(host="fd00::50", port=8443))
    assert result.ok, result


async def test_connection_single_node(fake_net: FakeNetwork) -> None:
    nodes = fixture("nodes")
    nodes["data"] = nodes["data"][:1]
    serve(fake_net, nodes=body(nodes))
    assert (await run_test(cfg())).message == "Proxmox VE 8.2.4: 1 node visible, 1 online"


async def test_connection_401(fake_net: FakeNetwork) -> None:
    serve(fake_net, version=status_answer(401))
    result = await run_test(cfg())
    assert not result.ok and result.hint is None
    assert result.message == (
        f"Proxmox VE API at {HOST}:8006 refused the API token (HTTP 401): check the token ID "
        "and secret, and that the token hasn't expired or been removed"
    )
    assert_no_secret(result)


async def test_connection_403(fake_net: FakeNetwork) -> None:
    serve(fake_net, nodes=status_answer(403))
    result = await run_test(cfg())
    assert not result.ok and result.hint is None
    assert "lacks permission" in result.message and "PVEAuditor" in result.message
    assert_no_secret(result)


async def test_connection_token_sees_no_node(fake_net: FakeNetwork) -> None:
    # Proxmox filters what a token may not read instead of answering 403: a token
    # without PVEAuditor gets /version and an empty node list.
    serve(fake_net, nodes=body({"data": []}))
    result = await run_test(cfg())
    assert not result.ok
    assert result.message.startswith("Proxmox VE 8.2.4 accepted the token, but it can't see")
    assert "PVEAuditor" in result.message


async def test_connection_other_status(fake_net: FakeNetwork) -> None:
    serve(fake_net, version=status_answer(500))
    result = await run_test(cfg())
    assert not result.ok and result.hint is None
    assert result.message == (
        f"Proxmox VE API at {HOST}:8006 answered HTTP 500 (is this the Proxmox VE API port?)"
    )


@pytest.mark.parametrize(
    "answer",
    [
        http_response(200, b"<html>not the api</html>"),
        http_response(200, b'{"errors": "nope"}'),
        http_response(200, b'["data"]'),
    ],
)
async def test_connection_not_the_api(fake_net: FakeNetwork, answer: bytes) -> None:
    serve(fake_net, version=answer)
    result = await run_test(cfg())
    assert not result.ok and result.hint is None
    assert "not like the Proxmox VE API" in result.message


async def test_connection_body_too_large(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pve, "MAX_BODY_BYTES", 64)
    serve(fake_net)
    result = await run_test(cfg())
    assert not result.ok and "not like the Proxmox VE API" in result.message


async def test_connection_tls_not_trusted(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    # What httpcore raises for a failed handshake: the ssl module's text, no cause.
    fake_net.tls_error = httpcore.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate in certificate chain (_ssl.c:1000)"
    )
    result = await run_test(cfg())
    assert not result.ok
    assert result.hint is None  # a firewall rule won't help
    assert result.message.startswith(f"certificate not trusted: the TLS certificate of {HOST}")
    assert "Trusted certificate" in result.message
    assert "_ssl.c" not in result.message
    assert fake_net.requests == []  # the token was never sent


@pytest.mark.parametrize(
    "error", [httpcore.ConnectTimeout("timed out"), httpcore.ConnectError("refused")]
)
async def test_connection_failure_gives_tcp_hint(fake_net: FakeNetwork, error: Exception) -> None:
    fake_net.fail(ADDRESS, PORT, error)
    result = await run_test(cfg())
    assert not result.ok
    assert f"{HOST}:8006" in result.message
    assert result.hint is not None and result.hint.model_dump() == TCP_HINT


async def test_connection_hung_api_times_out_with_hint(fake_net: FakeNetwork) -> None:
    serve(fake_net, version=hang)
    started = time.monotonic()
    result = await asyncio.wait_for(run_test(cfg(timeout_seconds=1)), 5)
    assert time.monotonic() - started < 3
    assert not result.ok
    assert result.message == f"timed out waiting for {HOST}:8006"
    assert result.hint is not None and result.hint.model_dump() == TCP_HINT


async def test_connection_hint_uses_configured_port(fake_net: FakeNetwork) -> None:
    fake_net.fail(ADDRESS, 8443, httpcore.ConnectTimeout("timed out"))
    result = await run_test(cfg(port=8443))
    assert result.hint is not None and result.hint.port == 8443


@pytest.mark.parametrize("host", ["metadata.example.internal", "127.0.0.1", "192.0.2.50"])
async def test_connection_refused_by_guard(fake_net: FakeNetwork, host: str) -> None:
    result = await run_test(cfg(host=host))
    assert not result.ok
    assert result.message.startswith("refused by the network guard")
    assert result.hint is None
    assert fake_net.connects == []


async def test_connection_odd_version_text_is_cleaned(fake_net: FakeNetwork) -> None:
    serve(fake_net, version=body({"data": {"version": "8.2\n\x00<b>" + "9" * 100}}))
    result = await run_test(cfg())
    assert result.ok
    assert "\n" not in result.message and "\x00" not in result.message
    assert len(result.message) < 100


# --- poll ------------------------------------------------------------------------------


async def test_poll_parses_the_fixtures(
    fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net)
    samples = await run_poll(cfg())
    assert_read_only(fake_net)
    # The offline node gets no per-node requests; the online ones get status and storage.
    assert [r.target for r in fake_net.requests] == [
        f"{V}/nodes",
        f"{V}/cluster/resources?type=vm",
        f"{V}/nodes/pve-example/status",
        f"{V}/nodes/pve-example/storage",
        f"{V}/nodes/pve-example-2/status",
        f"{V}/nodes/pve-example-2/storage",
    ]
    node = {"node": "pve-example"}
    assert value(samples, "node_up", **node) == 1.0
    assert value(samples, "node_up", node="pve-example-2") == 1.0
    assert value(samples, "node_up", node="pve-example-3") == 0.0
    assert value(samples, "node_cpu_ratio", **node) == 0.0412
    assert value(samples, "node_cpus", **node) == 8
    assert value(samples, "node_memory_used_bytes", **node) == 12884901888
    assert value(samples, "node_memory_total_bytes", **node) == 33554432000
    assert value(samples, "node_root_disk_used_bytes", **node) == 9663676416
    assert value(samples, "node_root_disk_total_bytes", **node) == 101083820032
    assert value(samples, "node_uptime_seconds", **node) == 864000
    assert value(samples, "node_load1", **node) == 0.42
    assert value(samples, "node_load5", **node) == 0.35
    assert value(samples, "node_load15", **node) == 0.30
    # The offline node has only node_up.
    assert [s.name for s in samples if s.labels.get("node") == "pve-example-3"] == [
        f"{PREFIX}_node_up"
    ]

    web = {"node": "pve-example", "vmid": "100", "guest_type": "qemu"}
    assert value(samples, "guest_running", **web) == 1.0
    assert value(samples, "guest_cpu_ratio", **web) == 0.0215
    assert value(samples, "guest_cpus", **web) == 2
    assert value(samples, "guest_memory_used_bytes", **web) == 2147483648
    assert value(samples, "guest_memory_max_bytes", **web) == 4294967296
    assert value(samples, "guest_disk_used_bytes", **web) == 0
    assert value(samples, "guest_disk_max_bytes", **web) == 34359738368
    assert value(samples, "guest_network_receive_bytes", **web) == 1073741824
    assert value(samples, "guest_network_transmit_bytes", **web) == 536870912
    assert value(samples, "guest_disk_read_bytes", **web) == 5368709120
    assert value(samples, "guest_disk_written_bytes", **web) == 2147483648
    assert value(samples, "guest_uptime_seconds", **web) == 86400
    dns = {"node": "pve-example-2", "vmid": "200", "guest_type": "lxc"}
    assert value(samples, "guest_disk_used_bytes", **dns) == 1073741824

    by_name = {s.name: s.type for s in samples}
    for counter in ("network_receive", "network_transmit", "disk_read", "disk_written"):
        assert by_name[f"{PREFIX}_guest_{counter}_bytes"] == "counter"
    assert by_name[f"{PREFIX}_guest_memory_used_bytes"] == "gauge"

    local = {"node": "pve-example", "storage": "local-lvm"}
    assert value(samples, "storage_active", **local) == 1.0
    assert value(samples, "storage_used_bytes", **local) == 75161927680
    assert value(samples, "storage_total_bytes", **local) == 375809638400
    # An inactive storage has no sizes: only its active flag.
    nfs = [s for s in samples if s.labels.get("storage") == "nfs-backup"]
    assert {(s.name, s.value) for s in nfs} == {(f"{PREFIX}_storage_active", 0.0)}

    assert value(samples, "nodes") == 3 and value(samples, "nodes_omitted") == 0
    assert value(samples, "guests") == 4 and value(samples, "guests_omitted") == 0
    assert value(samples, "storages") == 6 and value(samples, "storages_omitted") == 0
    assert_no_secret(samples, caplog.text)


async def test_poll_stopped_guest(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg())
    db = {"node": "pve-example", "vmid": "101", "guest_type": "qemu"}
    assert value(samples, "guest_running", **db) == 0.0
    assert value(samples, "guest_cpu_ratio", **db) == 0.0
    assert value(samples, "guest_uptime_seconds", **db) == 0.0
    assert value(samples, "guest_memory_max_bytes", **db) == 8589934592


async def test_poll_guest_with_missing_fields(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg())
    new = [s for s in samples if s.labels.get("vmid") == "201"]
    # Only what the entry has: no zero is invented for a missing field.
    assert {s.name.removeprefix(PREFIX + "_"): s.value for s in new} == {
        "guest_running": 0.0,
        "guest_memory_max_bytes": 268435456,
        "guest_disk_max_bytes": 4294967296,
        "guest_info": 1.0,
    }


async def test_poll_skips_templates(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg())
    assert not [s for s in samples if s.labels.get("vmid") == "9000"]


async def test_poll_guest_cap(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg(max_guests=2))
    assert {s.labels["vmid"] for s in samples if "vmid" in s.labels} == {"100", "101"}
    assert value(samples, "guests") == 4
    assert value(samples, "guests_omitted") == 2


async def test_poll_storage_cap(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg(max_storages=4))
    seen = {(s.labels["node"], s.labels["storage"]) for s in samples if "storage" in s.labels}
    # Sorted by node, then storage id.
    assert seen == {
        ("pve-example", "local"),
        ("pve-example", "local-lvm"),
        ("pve-example", "nfs-backup"),
        ("pve-example-2", "local"),
    }
    assert value(samples, "storages") == 6 and value(samples, "storages_omitted") == 2


async def test_poll_node_cap(fake_net: FakeNetwork) -> None:
    many = {"data": [{"node": f"pve-example-{i:02d}", "status": "online"} for i in range(40)]}
    serve(fake_net, nodes=body(many))
    samples = await run_poll(cfg())
    per_node = [r for r in fake_net.requests if r.target.endswith(("/status", "/storage"))]
    assert len(per_node) == 2 * pve.MAX_NODES
    assert value(samples, "nodes") == 40
    assert value(samples, "nodes_omitted") == 40 - pve.MAX_NODES
    assert len([s for s in samples if s.name == f"{PREFIX}_node_up"]) == pve.MAX_NODES


async def test_poll_sample_count_stays_inside_the_collector_limit(fake_net: FakeNetwork) -> None:
    guests = [
        {**fixture("cluster_resources_vm")["data"][0], "vmid": 100 + i, "id": f"qemu/{100 + i}"}
        for i in range(pve.MAX_GUESTS + 50)
    ]
    many_nodes = {"data": [{"node": f"pve-example-{i:02d}", "status": "online"} for i in range(40)]}
    storages = {
        "data": [{"storage": f"store{i}", "active": 1, "total": 10, "used": 1} for i in range(20)]
    }
    answers = {
        f"{V}/nodes": body(many_nodes),
        f"{V}/cluster/resources?type=vm": body({"data": guests}),
    }

    def handler(request: FakeRequest) -> bytes:
        if request.target.endswith("/storage"):
            return closing(body(storages))
        if request.target.endswith("/status"):
            return closing(body(fixture("node_status")))
        return closing(answers[request.target])

    fake_net.serve(ADDRESS, PORT, handler)
    samples = await run_poll(cfg(max_guests=pve.MAX_GUESTS, max_storages=pve.MAX_STORAGES))
    assert value(samples, "guests_omitted") == 50
    # Storages of the polled nodes only.
    assert value(samples, "storages_omitted") == pve.MAX_NODES * 20 - pve.MAX_STORAGES
    assert len(samples) < 10_000


async def test_poll_per_node_error_leaves_only_that_node_out(
    fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net, status2=status_answer(403), storage2=status_answer(595))
    samples = await run_poll(cfg())
    assert value(samples, "node_up", node="pve-example-2") == 1.0
    assert not [
        s for s in samples if s.labels.get("node") == "pve-example-2" and "storage" in s.labels
    ]
    assert value(samples, "node_load1", node="pve-example") == 0.42
    assert "2 per-node requests were answered with an error" in caplog.text
    assert_no_secret(caplog.text)


async def test_poll_per_node_401_fails_the_poll(fake_net: FakeNetwork) -> None:
    serve(fake_net, status1=status_answer(401))
    with pytest.raises(ProxmoxAuthFailed):
        await run_poll(cfg())


@pytest.mark.parametrize(
    ("key", "answer", "error", "start"),
    [
        ("nodes", status_answer(401), ProxmoxAuthFailed, "Proxmox VE API at"),
        ("guests", status_answer(403), ProxmoxForbidden, "the API token lacks permission"),
        ("nodes", status_answer(500), ProxmoxRequestFailed, "Proxmox VE API at"),
        ("nodes", http_response(200, b"<html/>"), ProxmoxBadAnswer, f"{HOST}:8006 answered"),
        ("guests", body({"data": {"not": "a list"}}), ProxmoxBadAnswer, f"{HOST}:8006"),
        ("nodes", body({"data": "nope"}), ProxmoxBadAnswer, f"{HOST}:8006"),
    ],
)
async def test_poll_failures_raise(
    fake_net: FakeNetwork, key: str, answer: bytes, error: type[Exception], start: str
) -> None:
    serve(fake_net, **{key: answer})
    with pytest.raises(error) as info:
        await run_poll(cfg())
    assert str(info.value).startswith(start)
    assert_no_secret(info.value)


async def test_poll_tls_failure(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    fake_net.tls_error = httpcore.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] verify failed")
    with pytest.raises(ProxmoxTlsUntrusted) as info:
        await run_poll(cfg())
    assert str(info.value).startswith("certificate not trusted")
    assert fake_net.requests == []


async def test_poll_unreachable(fake_net: FakeNetwork) -> None:
    with pytest.raises(ProxmoxUnreachable) as info:
        await run_poll(cfg())
    assert str(info.value) == f"could not connect to {HOST}:8006 (refused or unreachable)"


async def test_poll_timeout(fake_net: FakeNetwork) -> None:
    serve(fake_net, guests=hang)
    with pytest.raises(ProxmoxTimeout) as info:
        await asyncio.wait_for(run_poll(cfg(timeout_seconds=1)), 5)
    assert str(info.value) == f"timed out waiting for {HOST}:8006"


async def test_poll_refused_by_guard(fake_net: FakeNetwork) -> None:
    with pytest.raises(NetGuardError):
        await run_poll(cfg(host="metadata.example.internal"))
    assert fake_net.connects == []


def test_poll_budget_fits_the_collector_timeout() -> None:
    # Worst case without a budget: two cluster-wide GETs and two per node, each up to
    # the guarded client's 15 s.
    unbudgeted = (2 + 2 * pve.MAX_NODES) * DEFAULT_TOTAL_TIMEOUT
    assert unbudgeted > MAX_POLL_TIMEOUT_SECONDS  # which is why the budget exists
    assert pve.POLL_BUDGET_SECONDS < min(
        Proxmox().poll_interval_seconds(), MAX_POLL_TIMEOUT_SECONDS
    )


async def test_poll_budget_ends_a_slow_poll(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pve, "POLL_BUDGET_SECONDS", 0.3)
    answers = default_answers()

    async def slow(request: FakeRequest) -> bytes:
        await asyncio.sleep(0.1)  # no single request times out
        return closing(answers[request.target])

    fake_net.serve(ADDRESS, PORT, slow)
    started = time.monotonic()
    with pytest.raises(ProxmoxPollTooSlow) as info:
        await asyncio.wait_for(run_poll(cfg()), 5)
    assert time.monotonic() - started < 2
    assert str(info.value).startswith(f"Proxmox VE poll of {HOST}:8006 took longer than 0.3 s")
    assert_no_secret(info.value)


async def test_poll_skips_unusable_node_names_and_never_requests_them(
    fake_net: FakeNetwork,
) -> None:
    nodes = {
        "data": [
            {"node": "pve-example", "status": "online"},
            {"node": "../cluster", "status": "online"},
            {"node": "pve example", "status": "online"},
            {"node": "pve-example/../../access", "status": "online"},
            {"node": 7, "status": "online"},
            "not-an-object",
        ]
    }
    serve(fake_net, nodes=body(nodes))
    samples = await run_poll(cfg())
    assert {s.labels["node"] for s in samples if s.name == f"{PREFIX}_node_up"} == {"pve-example"}
    assert_read_only(fake_net)
    assert value(samples, "nodes") == 1


async def test_poll_skips_unusable_values(fake_net: FakeNetwork) -> None:
    guests = {
        "data": [
            {
                "type": "qemu",
                "vmid": 300,
                "node": "pve-example",
                "status": "running",
                "cpu": -1,
                "mem": "NaN",
                "maxmem": True,
                "netin": "inf",
                "netout": None,
                "uptime": "12",
            },
            {"type": "qemu", "vmid": True, "node": "pve-example", "status": "running"},
            {"type": "qemu", "vmid": 0, "node": "pve-example", "status": "running"},
            {"type": "openvz", "vmid": 301, "node": "pve-example", "status": "running"},
            {"type": "lxc", "vmid": 302, "node": "../x", "status": "running"},
            {"type": "lxc", "vmid": "303", "node": "pve-example", "status": "running"},
        ]
    }
    status = fixture("node_status")
    status["data"]["loadavg"] = ["0.1", "x"]  # wrong length: no load samples
    status["data"]["memory"] = "n/a"
    serve(fake_net, guests=body(guests), status1=body(status))
    samples = await run_poll(cfg())
    guest = [s for s in samples if "vmid" in s.labels]
    assert {(s.name.removeprefix(PREFIX + "_"), s.value) for s in guest} == {
        ("guest_running", 1.0),
        ("guest_uptime_seconds", 12.0),
        ("guest_info", 1.0),
    }
    node1 = {
        s.name.removeprefix(PREFIX + "_") for s in samples if s.labels == {"node": "pve-example"}
    }
    assert "node_load1" not in node1 and "node_memory_used_bytes" not in node1
    assert "node_root_disk_used_bytes" in node1


async def test_poll_parses_large_answers_off_the_event_loop(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    real = asyncio.to_thread

    async def spy(func: Any, *args: Any) -> Any:
        calls.append(func)
        return await real(func, *args)

    monkeypatch.setattr(pve.asyncio, "to_thread", spy)
    monkeypatch.setattr(pve, "_PARSE_IN_THREAD_BYTES", 1024)
    serve(fake_net)
    await run_poll(cfg())
    assert calls and all(func is json.loads for func in calls)


async def test_free_text_labels_only_on_info_series(fake_net: FakeNetwork) -> None:
    """Maintainer exception (2026-10-28): cleaned, capped guest names may be a label
    value on vantage_proxmox_guest_info only (value 1), never on a value series. Tags
    and other device text are never emitted."""
    serve(fake_net)
    samples = await run_poll(cfg())
    info = [s for s in samples if s.name.endswith("_info")]
    assert {s.name for s in info} == {f"{PREFIX}_guest_info"}
    for sample in samples:
        if sample.name.endswith("_info"):
            assert sample.value == 1.0 and sample.type == "gauge"
            assert set(sample.labels) == {"node", "vmid", "guest_type", "guest_name"}
        else:
            assert not FREE_TEXT_LABELS & sample.labels.keys(), sample.name
            assert set(sample.labels) <= OWN_LABELS, sample
            for text in GUEST_TEXT:
                assert text not in sample.labels.values()
        for text in ("example;web", "lvmthin", "Example CPU"):
            assert text not in sample.labels.values()
    names = {s.labels["vmid"]: s.labels["guest_name"] for s in info}
    # Templates are skipped, so 9000 has no info series either.
    assert names == {
        "100": "example-web",
        "101": "example-db",
        "200": "example-dns",
        "201": "example-new",
    }


async def test_guest_name_is_cleaned_and_capped(fake_net: FakeNetwork) -> None:
    long_name = "web\n\x00\tone  " + "x" * 300
    guests = {
        "data": [
            {"type": "qemu", "vmid": 100, "node": "pve-example", "name": long_name},
            {"type": "qemu", "vmid": 101, "node": "pve-example", "name": 42},
            {"type": "lxc", "vmid": 102, "node": "pve-example"},
        ]
    }
    serve(fake_net, guests=body(guests))
    samples = await run_poll(cfg())
    names = {s.labels["vmid"]: s.labels["guest_name"] for s in samples if s.name.endswith("_info")}
    assert names["100"].startswith("web one x")
    assert len(names["100"]) == 128
    assert names["100"].isprintable()
    assert names["101"] == "" and names["102"] == ""


# --- read-only -------------------------------------------------------------------------


async def test_poll_and_test_send_only_allowlisted_gets(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    await run_test(cfg())
    await run_poll(cfg())
    assert_read_only(fake_net)
    assert {r.target for r in fake_net.requests} <= set(default_answers())


def test_no_write_or_console_endpoint_in_the_code() -> None:
    """Hard rule 2: nothing in the integration could send a write or open a console."""
    source = inspect.getsource(pve)
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
        "vncproxy",
        "termproxy",
        "spiceproxy",
        "vncwebsocket",
        "/execute",
        "/agent/",
        "access/ticket",
    ):
        assert word not in source, word
    # Every allowlisted pattern is a fixed path under /api2/json.
    for pattern in pve.ALLOWED_PATHS:
        assert pattern.pattern.startswith(r"/api2/json/")


# --- through the poll collector --------------------------------------------------------


async def collect(
    engine: Engine, config: dict[str, Any], *, site: str | None
) -> tuple[MetricsStore, PollSpec]:
    box = SecretBox(TEST_MASTER_KEY)
    instance_id = uuid.uuid4()
    stored, secrets = ic.split(ProxmoxConfig.model_validate(config))
    spec = PollSpec(
        instance_id=instance_id,
        type_id="proxmox",
        name="pve-1",
        site=site,
        updated_at=utcnow(),
        config=stored,
        secrets=ic.encrypt(box, instance_id, secrets),
    )
    store = MetricsStore()
    scheduler = PollScheduler(
        session_factory=make_session_factory(engine),
        registry=Registry([Proxmox]),
        secret_box=box,
        net_guard_factory=fake_guards,
        store=store,
    )
    store.track(spec.instance_id, spec.labels)
    await scheduler.poll_once(spec, Proxmox())
    return store, spec


@pytest.mark.parametrize(("site", "label"), [("lab", "lab"), (None, "")])
async def test_collector_exposes_samples_with_standard_labels(
    fake_net: FakeNetwork,
    engine: Engine,
    caplog: pytest.LogCaptureFixture,
    site: str | None,
    label: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net)
    store, spec = await collect(engine, cfg(), site=site)
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "up", status
    text = generate_latest(make_registry(store)).decode()
    std = (
        f'vantage_instance="pve-1",vantage_instance_id="{spec.instance_id}",'
        f'vantage_integration="proxmox",vantage_site="{label}"'
    )
    assert f'{PREFIX}_node_up{{node="pve-example",{std}}} 1.0' in text
    assert (
        f'{PREFIX}_guest_network_receive_bytes_total{{guest_type="qemu",node="pve-example",'
        f'{std},vmid="100"}} 1.073741824e+09' in text
    )
    assert f"# TYPE {PREFIX}_guest_network_receive_bytes_total counter" in text
    assert f'{PREFIX}_storage_active{{node="pve-example",storage="local",{std}}} 1.0' in text
    assert f"vantage_instance_up{{{std}}} 1.0" in text
    assert f"vantage_poll_dropped_samples{{{std}}} 0.0" in text
    assert_no_secret(text, caplog.text)


async def test_collector_marks_bad_token_down(
    fake_net: FakeNetwork, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net, nodes=status_answer(401))
    store, spec = await collect(engine, cfg(), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error == pve._msg_auth(f"{HOST}:8006")
    text = generate_latest(make_registry(store)).decode()
    assert f"{PREFIX}_node_up" not in text
    assert_no_secret(status, text, caplog.text)


async def test_collector_reports_guard_refusal(fake_net: FakeNetwork, engine: Engine) -> None:
    store, spec = await collect(engine, cfg(host="metadata.example.internal"), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error is not None and status.error.startswith("refused by the network guard")
    assert fake_net.connects == []


# --- through the instance API ----------------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_net: FakeNetwork) -> Iterator[TestClient]:
    # The built-in registry, as in production; only DNS and the sockets are fake.
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def test_api_create_test_read_and_update(admin: TestClient, fake_net: FakeNetwork) -> None:
    serve(fake_net)
    bodies = []
    r = admin.post("/api/v1/instances/test", json={"type_id": "proxmox", "config": cfg()})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    bodies.append(r.text)
    created = admin.post(
        "/api/v1/instances", json={"type_id": "proxmox", "name": "pve-1", "config": cfg()}
    )
    assert created.status_code == 201, created.text
    bodies.append(created.text)
    instance = created.json()
    assert "token_secret" not in instance["config"]
    assert instance["config"]["token_id"] == TOKEN_ID
    assert instance["secrets_set"] == {"token_secret": True}
    instance_id = instance["id"]
    for path in ("/api/v1/instances", f"/api/v1/instances/{instance_id}"):
        r = admin.get(path)
        assert r.status_code == 200
        bodies.append(r.text)
    tested = admin.post(f"/api/v1/instances/{instance_id}/test")
    assert tested.status_code == 200 and tested.json()["ok"] is True, tested.text
    bodies.append(tested.text)
    cards = admin.get(f"/api/v1/instances/{instance_id}/cards")
    assert cards.json() == [
        {"card_type": "proxmox-node", "title": "Proxmox node", "default_size": [4, 3]},
        {"card_type": "proxmox-guests", "title": "Guests", "default_size": [4, 3]},
    ]
    # An edit that omits the secret keeps it.
    patched = admin.patch(
        f"/api/v1/instances/{instance_id}",
        json={"config": {"host": HOST, "token_id": TOKEN_ID, "max_guests": 10}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["secrets_set"] == {"token_secret": True}
    bodies.append(patched.text)
    bodies.append(admin.get("/api/v1/integrations").text)
    bodies.append(admin.get("/api/v1/audit").text)
    assert_no_secret(*bodies)
    assert {(r.address, r.port) for r in fake_net.requests} == {(ADDRESS, PORT)}


@pytest.mark.parametrize("host", ["metadata.example.internal", "127.0.0.1", "192.0.2.50"])
def test_api_refuses_denied_host_on_save(
    admin: TestClient, fake_net: FakeNetwork, host: str
) -> None:
    r = admin.post(
        "/api/v1/instances",
        json={"type_id": "proxmox", "name": "pve-1", "config": cfg(host=host)},
    )
    assert r.status_code == 422, r.text
    assert fake_net.connects == []
    assert_no_secret(r.text)


def test_api_validation_error_holds_no_secret(admin: TestClient) -> None:
    bad = "example-token with a space"
    r = admin.post(
        "/api/v1/instances",
        json={"type_id": "proxmox", "name": "pve-1", "config": cfg(token_secret=bad)},
    )
    assert r.status_code == 422
    assert bad not in r.text and "example-token" not in r.text


def test_api_serves_tcp_hint_on_timeout(admin: TestClient, fake_net: FakeNetwork) -> None:
    fake_net.fail(ADDRESS, PORT, httpcore.ConnectTimeout("timed out"))
    r = admin.post("/api/v1/instances/test", json={"type_id": "proxmox", "config": cfg()})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["ok"] is False
    assert result["hint"] == TCP_HINT
    assert_no_secret(r.text)


def test_api_test_401_and_403(admin: TestClient, fake_net: FakeNetwork) -> None:
    for code, words in ((401, "refused the API token"), (403, "lacks permission")):
        serve(fake_net, version=status_answer(code))
        r = admin.post("/api/v1/instances/test", json={"type_id": "proxmox", "config": cfg()})
        assert r.status_code == 200 and r.json()["ok"] is False
        assert words in r.json()["message"]
        assert_no_secret(r.text)


# --- static parts ----------------------------------------------------------------------


def test_firewall_hints_cards_and_interval() -> None:
    ctx = make_ctx(Proxmox, cfg())
    assert [h.model_dump() for h in Proxmox().firewall_hints(ctx)] == [TCP_HINT]
    [hint] = Proxmox().firewall_hints(make_ctx(Proxmox, cfg(host="fd00::50", port=8443)))
    assert (hint.destination, hint.port) == ("fd00::50", 8443)
    assert [c.card_type for c in Proxmox().cards()] == ["proxmox-node", "proxmox-guests"]
    assert Proxmox().poll_interval_seconds() == 60
    assert Proxmox.category == "hypervisor" and Proxmox.cloud_hosts == ()


async def test_secret_reaches_only_the_authorization_header() -> None:
    config = ProxmoxConfig.model_validate(cfg())
    assert isinstance(config.token_secret, SecretStr)
    async with pve._client(make_ctx(Proxmox, cfg()), config) as client:
        assert client.headers["Authorization"] == AUTH_HEADER
        assert TOKEN_SECRET not in str(client.base_url)
        assert client.follow_redirects is False

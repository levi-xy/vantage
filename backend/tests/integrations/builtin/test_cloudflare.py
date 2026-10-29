"""Cloudflare account: schema, connection test, poll parsing, read-only requests, rate
limits, the cloud-host rules, and no secret anywhere.

Requests go through the real SSRF guard and the real guarded client; only the socket
layer is fake (``fake_net``). ``api.cloudflare.com`` resolves to a TEST-NET-1 address
that these tests treat as public (``public_test_net``). Answers are the hand-written
``fixtures/cloudflare_*.json`` files, in the shape of Cloudflare API v4 responses, with
example values only (example.com/.net/.org zones, made-up IDs).
"""

import asyncio
import datetime as dt
import inspect
import json
import logging
import re
import ssl
import uuid
from collections.abc import Callable, Iterator
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any

import httpcore
import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine

from app.collectors.scheduler import MAX_POLL_TIMEOUT_SECONDS, PollScheduler, PollSpec
from app.collectors.store import MAX_SAMPLES_PER_INSTANCE, MetricsStore, make_registry
from app.core import net_guard
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.core.net_guard import NetGuardError
from app.integrations import instance_config as ic
from app.integrations.base import Sample, TestResult
from app.integrations.builtin.cloudflare import (
    Cloudflare,
    CloudflareAuthFailed,
    CloudflareBadAnswer,
    CloudflareConfig,
    CloudflarePollTooSlow,
    CloudflareRateLimited,
    CloudflareRequestFailed,
    CloudflareTimeout,
    CloudflareTlsUntrusted,
    CloudflareTunnelsForbidden,
    CloudflareUnreachable,
)
from app.integrations.builtin.cloudflare import integration as cf
from app.integrations.registry import Registry
from app.integrations.schema import config_json_schema
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import (
    DNS,
    FakeNetwork,
    FakeRequest,
    fake_guards,
    hang,
    http_response,
    make_ctx,
)
from tests.test_auth import do_setup, make_client

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "api.cloudflare.com"
ADDRESS = "192.0.2.80"  # TEST-NET-1, treated as public by public_test_net
PORT = 443
ACCOUNT = "0000000000000000000000000acc0001"
ZONE_1 = "e0000000000000000000000000000001"  # example.com, with traffic
ZONE_2 = "e0000000000000000000000000000002"  # example.net, no traffic in the window
ZONE_3 = "e0000000000000000000000000000003"  # example.org, pending and paused
TUNNEL_1 = "00000000-0000-4000-8000-000000000001"
TUNNEL_2 = "00000000-0000-4000-8000-000000000002"
TUNNEL_3 = "00000000-0000-4000-8000-000000000003"
TOKEN = "example-token-cloudflare-0000000000000000"
AUTH_HEADER = f"Bearer {TOKEN}"
PREFIX = "vantage_cloudflare"
ZONES_TARGET = cf.zones_path(ACCOUNT)
TUNNELS_TARGET = cf.tunnels_path(ACCOUNT)
GRAPHQL_TARGET = "/client/v4/graphql"
TCP_HINT = {
    "protocol": "tcp",
    "port": 443,
    "destination": HOST,
    "reason": "The Vantage backend calls the Cloudflare API (HTTPS) on this port.",
}
OWN_LABELS = {"zone_id", "tunnel_id", "status", "status_class", "country"}
# The maintainer's label exception (INTEGRATIONS.md section 9): admin text, info series only.
FREE_TEXT_LABELS = {"zone_name", "tunnel_name"}
# A fixed "now": the analytics window is then 10:00 to 11:00 UTC.
NOW = dt.datetime(2026, 10, 29, 11, 23, 45, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def public_test_net(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat TEST-NET-1 as public, and resolve the API host there."""
    test_net = IPv4Network("192.0.2.0/24")
    original = net_guard._is_public
    monkeypatch.setattr(net_guard, "_is_public", lambda addr: addr in test_net or original(addr))
    monkeypatch.setitem(DNS, HOST, [ADDRESS])
    monkeypatch.setitem(DNS, "api.example.com", ["192.0.2.81"])


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(cf, "_utcnow", lambda: NOW)
    cf._backoff_until.clear()
    yield
    cf._backoff_until.clear()


def cfg(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"account_id": ACCOUNT, "api_token": TOKEN}
    config.update(overrides)
    return config


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"cloudflare_{name}.json").read_text())


def closing(raw: bytes) -> bytes:
    """The fake network answers one request per connection: tell the client so."""
    return raw.replace(b"\r\n", b"\r\nConnection: close\r\n", 1)


def body(data: Any, status: int = 200, headers: list[tuple[str, str]] | None = None) -> bytes:
    return http_response(
        status,
        json.dumps(data).encode(),
        reason="OK" if status == 200 else "Error",
        headers=[("Content-Type", "application/json"), *(headers or [])],
    )


def error_answer(status: int, headers: list[tuple[str, str]] | None = None) -> bytes:
    # Cloudflare's error envelope; its text must never reach a message.
    envelope = {
        "success": False,
        "errors": [{"code": 10000, "message": "Authentication error example-detail"}],
        "messages": [],
        "result": None,
    }
    return body(envelope, status, headers)


def graphql_for(request: FakeRequest) -> bytes:
    """Answer the traffic query with the fixture, for the zones the query asked for."""
    variables = json.loads(request.body)["variables"]
    data = fixture("graphql_zone_traffic")
    zones = data["data"]["viewer"]["zones"]
    data["data"]["viewer"]["zones"] = [z for z in zones if z["zoneTag"] in variables["zoneTags"]]
    return body(data)


Answer = bytes | Callable[[FakeRequest], bytes]


def serve(
    fake_net: FakeNetwork,
    *,
    zones: Answer | None = None,
    tunnels: Answer | None = None,
    graphql: Answer | None = None,
) -> None:
    answers: dict[tuple[str, str], Answer] = {
        ("GET", ZONES_TARGET): zones if zones is not None else body(fixture("zones")),
        ("GET", TUNNELS_TARGET): tunnels if tunnels is not None else body(fixture("tunnels")),
        ("POST", GRAPHQL_TARGET): graphql if graphql is not None else graphql_for,
    }

    async def handler(request: FakeRequest) -> bytes:
        answer = answers.get((request.method, request.target))
        if answer is None:
            return closing(http_response(404, b"{}", reason="Not Found"))
        result = answer if isinstance(answer, bytes) else answer(request)
        return closing(result)

    fake_net.serve(ADDRESS, PORT, handler)


async def run_test(config: dict[str, Any]) -> TestResult:
    return await Cloudflare().test_connection(make_ctx(Cloudflare, config))


async def run_poll(config: dict[str, Any]) -> list[Sample]:
    return await Cloudflare().poll(make_ctx(Cloudflare, config))


def assert_read_only(fake_net: FakeNetwork) -> None:
    """GET only to the two REST paths; POST only to /graphql with one of our query
    documents; nothing else, only to the checked API address, the token only in the
    Authorization header."""
    assert fake_net.requests
    for request in fake_net.requests:
        assert (request.address, request.port) == (ADDRESS, PORT)
        assert request.headers["host"] == HOST
        assert request.headers["authorization"] == AUTH_HEADER
        assert TOKEN not in request.target and TOKEN.encode() not in request.body
        if request.method == "GET":
            assert request.target in (ZONES_TARGET, TUNNELS_TARGET), request.target
            assert request.body == b""
        else:
            assert request.method == "POST", request
            assert request.target == GRAPHQL_TARGET
            payload = json.loads(request.body)
            assert set(payload) == {"query", "variables"}
            assert payload["query"] in cf.QUERY_DOCUMENTS
            assert cf.is_single_query(payload["query"])
            assert "mutation" not in payload["query"]
            assert set(payload["variables"]) <= cf.GRAPHQL_VARIABLES


def assert_no_secret(*texts: object) -> None:
    for text in texts:
        assert TOKEN not in str(text)
        assert "example-token" not in str(text)
        assert "example-detail" not in str(text)  # Cloudflare's own error text


def index(samples: list[Sample]) -> dict[tuple[str, tuple[tuple[str, str], ...]], Sample]:
    return {
        (s.name.removeprefix(PREFIX + "_"), tuple(sorted(s.labels.items()))): s for s in samples
    }


def value(samples: list[Sample], metric: str, **labels: str) -> float:
    return index(samples)[(metric, tuple(sorted(labels.items())))].value


def names(samples: list[Sample]) -> set[str]:
    return {s.name.removeprefix(PREFIX + "_") for s in samples}


def graphql_requests(fake_net: FakeNetwork) -> list[dict[str, Any]]:
    return [json.loads(r.body) for r in fake_net.requests if r.method == "POST"]


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults() -> None:
    config = CloudflareConfig.model_validate(cfg())
    assert config.account_id == ACCOUNT
    assert config.include_analytics is True and config.include_tunnels is True
    assert (config.max_zones, config.top_countries, config.top_status_codes) == (10, 10, 10)
    assert config.timeout_seconds == 10


def test_the_only_secret_is_the_api_token() -> None:
    stored, secrets = ic.split(CloudflareConfig.model_validate(cfg()))
    assert set(secrets) == {"api_token"}
    assert "api_token" not in stored and stored["account_id"] == ACCOUNT
    assert TOKEN not in json.dumps(stored)


def test_served_schema_flags_the_secret_write_only() -> None:
    schema = config_json_schema(CloudflareConfig)
    assert schema["properties"]["api_token"]["writeOnly"] is True
    assert "host" not in schema["properties"]  # the API host is fixed, not configured
    text = json.dumps(schema)
    for permission in ("Zone Analytics Read", "Zone Read", "Cloudflare Tunnel Read"):
        assert permission in text


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id": ACCOUNT.upper()},
        {"account_id": f"  {ACCOUNT} "},
        {"max_zones": 50, "top_countries": 25, "top_status_codes": 20, "timeout_seconds": 15},
        {"include_analytics": False, "include_tunnels": False},
    ],
)
def test_schema_accepts(overrides: dict[str, Any]) -> None:
    assert CloudflareConfig.model_validate(cfg(**overrides)).account_id == ACCOUNT


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id": "acc0001"},
        {"account_id": "g" * 32},
        {"account_id": "../" + "0" * 29},
        {"api_token": ""},
        {"api_token": "example token with spaces"},
        {"api_token": "example-token\r\nX-Injected: 1"},
        {"api_token": "x" * 257},
        {"max_zones": 0},
        {"max_zones": 51},
        {"top_countries": 26},
        {"top_status_codes": 21},
        {"timeout_seconds": 16},
    ],
)
def test_schema_rejects(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        CloudflareConfig.model_validate(cfg(**overrides))
    # What the API serves: the messages, never the input (pydantic's own str() shows the
    # input; the instance API leaves it out, see test_api_validation_error_holds_no_secret).
    messages = str(caught.value.errors(include_input=False, include_url=False))
    secret = overrides.get("api_token")
    if secret:
        assert secret not in messages
    assert_no_secret(messages)


def test_repr_holds_no_secret() -> None:
    config = CloudflareConfig.model_validate(cfg())
    assert isinstance(config.api_token, SecretStr)
    assert_no_secret(repr(config), str(config), repr(make_ctx(Cloudflare, cfg())))


# --- GraphQL: only our own read-only query ---------------------------------------------


def test_the_query_documents_are_single_queries() -> None:
    assert cf.QUERY_DOCUMENTS == frozenset({cf.ZONE_TRAFFIC_QUERY})
    for document in cf.QUERY_DOCUMENTS:
        assert cf.is_single_query(document)
        words = set(re.findall(r"[_A-Za-z][_0-9A-Za-z]*", document))
        assert not words & {"mutation", "subscription", "fragment"}


@pytest.mark.parametrize(
    "document",
    [
        "mutation { deleteZone(id: $id) }",
        "  mutation Example { purgeCache }",
        "subscription { events }",
        "{ viewer { zones { zoneTag } } }",  # anonymous shorthand
        "query A { viewer { zoneTag } } query B { viewer { zoneTag } }",
        "query A { viewer { zoneTag } } mutation B { x }",
        "query A { viewer { zoneTag } } { x }",
        "query A { viewer { ...Z } } fragment Z on Zone { zoneTag }",
        'query A { viewer { zones(filter: {zoneTag: "e01"}) { zoneTag } } }',
        "query A { viewer { mutation } }",
        "query A { viewer { zoneTag }",
        "query A { viewer } }",
        "query A",
        "",
    ],
)
def test_is_single_query_refuses(document: str) -> None:
    assert not cf.is_single_query(document)


def test_is_single_query_ignores_comments() -> None:
    assert cf.is_single_query("# a comment { \nquery A { viewer { zoneTag } } # end }")


VALID_VARIABLES = {
    "zoneTags": [ZONE_1],
    "start": "2026-10-29T10:00:00Z",
    "end": "2026-10-29T11:00:00Z",
    "limit": 1,
}


@pytest.mark.parametrize(
    "document",
    [
        "query Other { viewer { zones { zoneTag } } }",  # a valid query, but not ours
        "mutation { purgeEverything }",
        cf.ZONE_TRAFFIC_QUERY.replace("query", "mutation", 1),
        cf.ZONE_TRAFFIC_QUERY + "\nmutation X { y }",
    ],
)
def test_graphql_body_refuses_documents_that_are_not_ours(document: str) -> None:
    with pytest.raises(cf.GraphQLRefused):
        cf.graphql_body(document, VALID_VARIABLES)


@pytest.mark.parametrize(
    "variables",
    [
        {**VALID_VARIABLES, "input": {"paused": True}},
        {**VALID_VARIABLES, "zoneTags": []},
        {**VALID_VARIABLES, "zoneTags": [ZONE_1] * 11},
        {**VALID_VARIABLES, "zoneTags": ["example.com"]},
        {**VALID_VARIABLES, "zoneTags": ZONE_1},
        {**VALID_VARIABLES, "start": "2026-10-29T10:30:00Z"},
        {**VALID_VARIABLES, "end": "yesterday"},
        {**VALID_VARIABLES, "limit": 0},
        {**VALID_VARIABLES, "limit": 11},
        {**VALID_VARIABLES, "limit": True},
    ],
)
def test_graphql_body_refuses_unexpected_variables(variables: dict[str, Any]) -> None:
    with pytest.raises(cf.GraphQLRefused):
        cf.graphql_body(cf.ZONE_TRAFFIC_QUERY, variables)


def test_graphql_body_is_the_query_and_variables_only() -> None:
    payload = json.loads(cf.graphql_body(cf.ZONE_TRAFFIC_QUERY, VALID_VARIABLES))
    assert payload == {"query": cf.ZONE_TRAFFIC_QUERY, "variables": VALID_VARIABLES}


@pytest.mark.parametrize(
    ("method", "target", "content"),
    [
        ("POST", GRAPHQL_TARGET, json.dumps({"query": "mutation { purge }", "variables": {}})),
        (
            "POST",
            GRAPHQL_TARGET,
            json.dumps({"query": cf.ZONE_TRAFFIC_QUERY, "variables": VALID_VARIABLES, "extra": 1}),
        ),
        ("POST", GRAPHQL_TARGET, "not json"),
        ("POST", GRAPHQL_TARGET, None),
        ("POST", f"/client/v4/zones/{ZONE_1}/purge_cache", json.dumps({"purge_everything": True})),
        ("POST", ZONES_TARGET, None),
        ("PUT", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_1}", "{}"),
        ("PATCH", f"/client/v4/zones/{ZONE_1}", '{"paused": true}'),
        ("DELETE", f"/client/v4/zones/{ZONE_1}", None),
        ("DELETE", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_1}", None),
        ("GET", f"/client/v4/zones/{ZONE_1}/dns_records", None),
        ("GET", "/client/v4/user/tokens", None),
        ("GET", ZONES_TARGET + "&name=example.com", None),
        ("GET", ZONES_TARGET, "{}"),
    ],
)
async def test_request_refuses_anything_else_before_sending(
    fake_net: FakeNetwork, method: Any, target: str, content: str | None
) -> None:
    serve(fake_net)
    ctx = make_ctx(Cloudflare, cfg())
    async with cf._client(ctx, ctx.config_as(CloudflareConfig)) as client:
        with pytest.raises(cf.GraphQLRefused):
            await cf._request(
                client, method, target, 5.0, content=None if content is None else content.encode()
            )
    assert fake_net.requests == [] and fake_net.connects == []


@pytest.mark.parametrize(
    ("method", "target", "expected"),
    [
        ("GET", ZONES_TARGET, True),
        ("GET", TUNNELS_TARGET, True),
        ("POST", GRAPHQL_TARGET, True),
        ("GET", GRAPHQL_TARGET, False),
        ("POST", ZONES_TARGET, False),
        ("PUT", GRAPHQL_TARGET, False),
        ("HEAD", ZONES_TARGET, False),
        ("GET", cf.zones_path("0" * 31 + "/"), False),
        ("GET", cf.tunnels_path(ACCOUNT).replace("is_deleted=false", "is_deleted=true"), False),
    ],
)
def test_allowed_request(method: str, target: str, expected: bool) -> None:
    assert cf.allowed_request(method, target) is expected


def test_no_write_method_in_the_code() -> None:
    """Hard rule 2: the only state-changing verb in the code is the approved GraphQL
    POST, and it appears only where requests are allowlisted and sent."""
    source = inspect.getsource(cf)
    for word in ('"PUT"', '"PATCH"', '"DELETE"', ".put(", ".patch(", ".delete(", ".post("):
        assert word not in source, word
    for name in ("purge", "dns_records", "/tokens", "/members"):
        assert name not in source, name
    posts = [line.strip() for line in source.splitlines() if '"POST"' in line]
    assert posts == [
        'return method == "POST" and target == PATH_GRAPHQL',
        'method: Literal["GET", "POST"],',
        'if method == "POST":',
        'payload = await _request(client, "POST", PATH_GRAPHQL, timeout, content=body)',
    ]


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net)
    result = await run_test(cfg())
    assert result.ok is True, result
    assert result.message == (
        "Cloudflare API: token accepted; 3 zones, 3 tunnels (1 healthy), analytics readable"
    )
    assert result.hint is None
    assert_read_only(fake_net)
    [query] = graphql_requests(fake_net)
    assert query["variables"]["zoneTags"] == [ZONE_1]
    assert_no_secret(result, caplog.text)


async def test_connection_without_tunnels_and_analytics(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    result = await run_test(cfg(include_tunnels=False, include_analytics=False))
    assert result.ok is True and result.message == "Cloudflare API: token accepted; 3 zones"
    assert [r.target for r in fake_net.requests] == [ZONES_TARGET]


async def test_connection_account_without_zones(fake_net: FakeNetwork) -> None:
    empty = {"success": True, "errors": [], "messages": [], "result": [], "result_info": {}}
    serve(fake_net, zones=body(empty))
    result = await run_test(cfg())
    assert result.ok is True
    assert result.message.endswith("0 zones, 3 tunnels (1 healthy), no zone to read analytics for")
    assert graphql_requests(fake_net) == []


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_connection_bad_token(fake_net: FakeNetwork, status: int) -> None:
    serve(fake_net, zones=error_answer(status))
    result = await run_test(cfg())
    assert result.ok is False and result.hint is None
    assert result.message == cf._msg_auth(status)
    assert "refused the API token" in result.message
    assert_no_secret(result)
    assert [r.target for r in fake_net.requests] == [ZONES_TARGET]


async def test_connection_token_without_tunnel_read(fake_net: FakeNetwork) -> None:
    serve(fake_net, tunnels=error_answer(403))
    result = await run_test(cfg())
    assert result.ok is False and "Cloudflare Tunnel Read" in result.message
    assert_no_secret(result)


async def test_connection_wrong_account_for_tunnels(fake_net: FakeNetwork) -> None:
    serve(fake_net, tunnels=error_answer(404))
    result = await run_test(cfg())
    assert result.ok is False and "check the account ID" in result.message


@pytest.mark.parametrize(
    "answer",
    [
        body(
            {"data": None, "errors": [{"message": "not authorized for that zone example-detail"}]}
        ),
        error_answer(403),
    ],
)
async def test_connection_token_without_analytics(fake_net: FakeNetwork, answer: bytes) -> None:
    serve(fake_net, graphql=answer)
    result = await run_test(cfg())
    assert result.ok is False and result.message == cf._MSG_ANALYTICS
    assert_no_secret(result)


async def test_connection_other_status(fake_net: FakeNetwork) -> None:
    serve(fake_net, zones=error_answer(500))
    result = await run_test(cfg())
    assert (
        result.ok is False and result.message == f"Cloudflare API at {HOST}:443 answered HTTP 500"
    )
    assert_no_secret(result)


@pytest.mark.parametrize(
    "answer",
    [
        http_response(200, b"<html>captive portal</html>"),
        body({"success": False, "result": None}),
        body({"success": True}),
        body({"success": True, "result": {"not": "a list"}}),
        body([1, 2, 3]),
    ],
)
async def test_connection_not_the_api(fake_net: FakeNetwork, answer: bytes) -> None:
    serve(fake_net, zones=answer)
    result = await run_test(cfg())
    assert result.ok is False and result.message == cf._MSG_BAD_ANSWER


async def test_connection_rate_limited_starts_backoff(fake_net: FakeNetwork) -> None:
    serve(fake_net, zones=error_answer(429, [("Retry-After", "90")]))
    ctx = make_ctx(Cloudflare, cfg())
    result = await Cloudflare().test_connection(ctx)
    assert result.ok is False and result.message == cf._msg_rate_limited(90)
    sent = len(fake_net.requests)
    again = await Cloudflare().test_connection(ctx)
    assert again.ok is False and again.message.startswith("Cloudflare API rate limit: waiting")
    assert len(fake_net.requests) == sent  # nothing sent during the backoff


@pytest.mark.parametrize(
    "error", [httpcore.ConnectTimeout("timed out"), httpcore.ConnectError("refused")]
)
async def test_connection_failure_gives_tcp_hint(fake_net: FakeNetwork, error: Exception) -> None:
    fake_net.fail(ADDRESS, PORT, error)
    result = await run_test(cfg())
    assert result.ok is False
    assert result.hint is not None and result.hint.model_dump() == TCP_HINT
    assert HOST in result.message
    assert_no_secret(result)


async def test_connection_hung_api_times_out_with_hint(fake_net: FakeNetwork) -> None:
    fake_net.serve(ADDRESS, PORT, hang)
    result = await run_test(cfg(timeout_seconds=1))
    assert result.ok is False and result.message == f"timed out waiting for {HOST}:443"
    assert result.hint is not None and result.hint.model_dump() == TCP_HINT


async def test_connection_tls_failure_has_no_hint(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    fake_net.tls_error = httpcore.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    )
    result = await run_test(cfg())
    assert result.ok is False and result.message == cf._MSG_TLS and result.hint is None


async def test_connection_refused_when_the_api_host_resolves_privately(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(DNS, HOST, ["10.0.0.9"])
    result = await run_test(cfg())
    assert result.ok is False and result.message.startswith("refused by the network guard")
    assert result.hint is None and fake_net.connects == []


# --- the cloud host rules --------------------------------------------------------------


async def test_the_integration_verifies_the_api_certificate(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    await run_test(cfg())
    assert fake_net.tls
    for server_hostname, context in fake_net.tls:
        assert server_hostname == HOST
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert set(fake_net.connects) == {(ADDRESS, PORT)}


async def test_cloud_host_refused_without_verification(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    ctx = make_ctx(Cloudflare, cfg())
    async with ctx.http_client(verify=False) as client:
        with pytest.raises(NetGuardError):
            await client.get(f"https://{HOST}{ZONES_TARGET}")
    no_hostname = ssl.create_default_context()
    no_hostname.check_hostname = False
    async with ctx.http_client(verify=no_hostname) as client:
        with pytest.raises(NetGuardError):
            await client.get(f"https://{HOST}{ZONES_TARGET}")
    assert fake_net.connects == []


async def test_cloud_host_refused_over_http(fake_net: FakeNetwork) -> None:
    fake_net.serve(ADDRESS, 80, http_response(200, b"{}"))
    async with make_ctx(Cloudflare, cfg()).http_client() as client:
        with pytest.raises(NetGuardError):
            await client.get(f"http://{HOST}{ZONES_TARGET}")
    assert fake_net.connects == []


async def test_undeclared_public_host_refused(fake_net: FakeNetwork) -> None:
    fake_net.serve("192.0.2.81", 443, http_response(200, b"{}"))
    async with make_ctx(Cloudflare, cfg()).http_client() as client:
        with pytest.raises(NetGuardError):
            await client.get("https://api.example.com/client/v4/zones")
    assert fake_net.connects == []


async def test_redirects_are_not_followed(fake_net: FakeNetwork) -> None:
    moved = http_response(
        302, b"", reason="Found", headers=[("Location", "https://api.example.com/")]
    )
    serve(fake_net, zones=moved)
    result = await run_test(cfg())
    assert (
        result.ok is False and result.message == f"Cloudflare API at {HOST}:443 answered HTTP 302"
    )
    assert set(fake_net.connects) == {(ADDRESS, PORT)}


async def test_token_reaches_only_the_authorization_header() -> None:
    ctx = make_ctx(Cloudflare, cfg())
    async with cf._client(ctx, ctx.config_as(CloudflareConfig)) as client:
        assert client.headers["Authorization"] == AUTH_HEADER
        assert str(client.base_url) == f"https://{HOST}"
        assert client.follow_redirects is False


# --- poll ------------------------------------------------------------------------------


async def test_poll_parses_the_fixtures(
    fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net)
    samples = await run_poll(cfg())
    assert all(s.type == "gauge" for s in samples)
    z1 = {"zone_id": ZONE_1}
    # Zones.
    assert value(samples, "zones") == 3 and value(samples, "zones_omitted") == 0
    assert value(samples, "zone_active", **z1) == 1
    assert value(samples, "zone_active", zone_id=ZONE_3) == 0
    assert value(samples, "zone_paused", zone_id=ZONE_3) == 1
    assert value(samples, "zone_info", zone_id=ZONE_1, zone_name="example.com") == 1
    # Traffic over the last complete hour.
    assert value(samples, "analytics_up") == 1
    window_end = dt.datetime(2026, 10, 29, 11, tzinfo=dt.UTC).timestamp()
    assert value(samples, "analytics_window_end_timestamp_seconds") == window_end
    assert value(samples, "zone_requests", **z1) == 12000
    assert value(samples, "zone_cached_requests", **z1) == 8000
    assert value(samples, "zone_bytes", **z1) == 500_000_000
    assert value(samples, "zone_cached_bytes", **z1) == 300_000_000
    assert value(samples, "zone_threats", **z1) == 12
    assert value(samples, "zone_page_views", **z1) == 3000
    classes = {"1xx": 0, "2xx": 10000, "3xx": 1200, "4xx": 600, "5xx": 200, "other": 0}
    for status_class, requests in classes.items():
        assert value(samples, "zone_requests_by_status_class", **z1, status_class=status_class) == (
            requests
        )
    for status, requests in {"200": 10000, "304": 1200, "404": 600, "500": 150, "503": 50}.items():
        assert value(samples, "zone_requests_by_status", **z1, status=status) == requests
    assert value(samples, "zone_requests_by_status", **z1, status="other") == 0
    countries = {"US": (6000, 5), "DE": (3000, 4), "GB": (2400, 0), "XX": (500, 1), "T1": (100, 2)}
    for country, (requests, threats) in countries.items():
        assert value(samples, "zone_requests_by_country", **z1, country=country) == requests
        assert value(samples, "zone_threats_by_country", **z1, country=country) == threats
    assert value(samples, "zone_requests_by_country", **z1, country="other") == 0
    # A zone with no traffic in the window has zeros, not gaps.
    assert value(samples, "zone_requests", zone_id=ZONE_2) == 0
    assert value(samples, "zone_requests_by_status_class", zone_id=ZONE_2, status_class="5xx") == 0
    # Tunnels: the deleted one is left out.
    assert value(samples, "tunnels") == 3 and value(samples, "tunnels_omitted") == 0
    assert value(samples, "tunnel_healthy", tunnel_id=TUNNEL_1) == 1
    assert value(samples, "tunnel_healthy", tunnel_id=TUNNEL_2) == 0
    assert value(samples, "tunnel_connections", tunnel_id=TUNNEL_1) == 4
    assert value(samples, "tunnel_connections", tunnel_id=TUNNEL_3) == 0
    for status in cf.TUNNEL_STATUSES:
        expected = 1 if status == "degraded" else 0
        assert value(samples, "tunnel_status", tunnel_id=TUNNEL_2, status=status) == expected
    assert value(samples, "tunnel_info", tunnel_id=TUNNEL_1, tunnel_name="example-home") == 1
    assert "example-deleted" not in str(samples)
    # The window and the request pattern.
    [query] = graphql_requests(fake_net)
    assert query["variables"] == {
        "zoneTags": [ZONE_1, ZONE_2, ZONE_3],
        "start": "2026-10-29T10:00:00Z",
        "end": "2026-10-29T11:00:00Z",
        "limit": 1,
    }
    assert [r.method for r in fake_net.requests] == ["GET", "GET", "POST"]
    assert_read_only(fake_net)
    assert_no_secret(samples, caplog.text)


async def test_poll_top_n_with_the_rest_as_other(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg(top_countries=2, top_status_codes=2))
    z1 = {"zone_id": ZONE_1}
    countries = {
        s.labels["country"]: s.value
        for s in samples
        if s.name == f"{PREFIX}_zone_requests_by_country" and s.labels["zone_id"] == ZONE_1
    }
    assert countries == {"US": 6000, "DE": 3000, "other": 3000}
    assert value(samples, "zone_threats_by_country", **z1, country="other") == 3
    statuses = {
        s.labels["status"]: s.value
        for s in samples
        if s.name == f"{PREFIX}_zone_requests_by_status" and s.labels["zone_id"] == ZONE_1
    }
    assert statuses == {"200": 10000, "304": 1200, "other": 800}


async def test_poll_label_sets_stay_bounded(fake_net: FakeNetwork) -> None:
    """Hundreds of countries and status codes still give top-N plus "other" per zone."""
    country_map: list[dict[str, Any]] = [
        {"clientCountryName": f"{chr(65 + i // 26)}{chr(65 + i % 26)}", "requests": i, "threats": 1}
        for i in range(300)
    ]
    country_map += [{"clientCountryName": "not a code", "requests": 7, "threats": 1}]
    status_map: list[dict[str, Any]] = [
        {"edgeResponseStatus": code, "requests": 1} for code in range(100, 600)
    ]
    status_map += [{"edgeResponseStatus": 0, "requests": 5}, {"edgeResponseStatus": "x"}]
    data = {
        "data": {
            "viewer": {
                "zones": [
                    {
                        "zoneTag": ZONE_1,
                        "httpRequests1hGroups": [
                            {
                                "sum": {
                                    "requests": 1,
                                    "countryMap": country_map,
                                    "responseStatusMap": status_map,
                                }
                            }
                        ],
                    }
                ]
            }
        }
    }
    serve(fake_net, graphql=body(data))
    samples = await run_poll(cfg(top_countries=25, top_status_codes=20))

    def label_values(metric: str, label: str) -> set[str]:
        return {
            s.labels[label]
            for s in samples
            if s.name == f"{PREFIX}_{metric}" and s.labels["zone_id"] == ZONE_1
        }

    assert len(label_values("zone_requests_by_country", "country")) == 26
    assert len(label_values("zone_requests_by_status", "status")) == 21
    assert label_values("zone_requests_by_status_class", "status_class") == set(cf.STATUS_CLASSES)
    total = sum(
        s.value
        for s in samples
        if s.name == f"{PREFIX}_zone_requests_by_country" and s.labels["zone_id"] == ZONE_1
    )
    assert total == sum(range(300)) + 7  # nothing lost: the rest is in "other"
    assert (
        value(samples, "zone_requests_by_status_class", zone_id=ZONE_1, status_class="other") == 5
    )


async def test_poll_zone_cap(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg(max_zones=1))
    assert value(samples, "zones") == 3 and value(samples, "zones_omitted") == 2
    assert {s.labels.get("zone_id") for s in samples} - {None} == {ZONE_1}
    [query] = graphql_requests(fake_net)
    assert query["variables"]["zoneTags"] == [ZONE_1]


async def test_poll_queries_at_most_ten_zones_at_a_time(fake_net: FakeNetwork) -> None:
    zones = fixture("zones")
    template = zones["result"][0]
    zones["result"] = [
        {**template, "id": f"e{n:031x}", "name": f"zone-{n}.example.com"} for n in range(1, 51)
    ]
    zones["result_info"]["total_count"] = 120
    serve(fake_net, zones=body(zones))
    samples = await run_poll(cfg(max_zones=25))
    assert value(samples, "zones") == 120 and value(samples, "zones_omitted") == 95
    queries = graphql_requests(fake_net)
    assert [len(q["variables"]["zoneTags"]) for q in queries] == [10, 10, 5]
    assert len(samples) < MAX_SAMPLES_PER_INSTANCE
    assert_read_only(fake_net)


async def test_poll_sample_count_stays_inside_the_collector_limit(fake_net: FakeNetwork) -> None:
    zones = fixture("zones")
    template = zones["result"][0]
    zones["result"] = [{**template, "id": f"e{n:031x}"} for n in range(1, 51)]
    tunnels = fixture("tunnels")
    one = tunnels["result"][0]
    tunnels["result"] = [{**one, "id": f"00000000-0000-4000-8000-{n:012x}"} for n in range(200)]
    many = {
        "clientCountryName": "US",
        "requests": 1,
        "threats": 1,
    }

    def graphql(request: FakeRequest) -> bytes:
        tags = json.loads(request.body)["variables"]["zoneTags"]
        countries = [{**many, "clientCountryName": f"A{chr(65 + i)}"} for i in range(26)]
        statuses = [{"edgeResponseStatus": c, "requests": 1} for c in range(100, 600)]
        zone = {"sum": {"requests": 1, "countryMap": countries, "responseStatusMap": statuses}}
        return body(
            {
                "data": {
                    "viewer": {
                        "zones": [{"zoneTag": t, "httpRequests1hGroups": [zone]} for t in tags]
                    }
                }
            }
        )

    serve(fake_net, zones=body(zones), tunnels=body(tunnels), graphql=graphql)
    samples = await run_poll(cfg(max_zones=50, top_countries=25, top_status_codes=20))
    assert value(samples, "tunnels") == 200 and value(samples, "tunnels_omitted") == 100
    assert len(samples) < MAX_SAMPLES_PER_INSTANCE
    assert len(index(samples)) == len(samples)  # no duplicate series


async def test_poll_skips_unusable_entries(fake_net: FakeNetwork) -> None:
    zones = fixture("zones")
    zones["result"] += [
        {"id": "not-a-zone-id", "name": "bad.example.com", "status": "active"},
        {"id": ZONE_1, "name": "duplicate.example.com", "status": "active"},
        "not an object",
    ]
    tunnels = fixture("tunnels")
    tunnels["result"] += [
        {"id": "../../etc", "name": "bad", "status": "healthy"},
        {"id": "00000000-0000-4000-8000-0000000000aa", "name": "odd", "status": "exploded"},
    ]
    serve(fake_net, zones=body(zones), tunnels=body(tunnels))
    samples = await run_poll(cfg())
    zone_ids = {s.labels["zone_id"] for s in samples if "zone_id" in s.labels}
    assert zone_ids == {ZONE_1, ZONE_2, ZONE_3}
    assert "duplicate.example.com" not in str(samples) and "bad.example.com" not in str(samples)
    odd = "00000000-0000-4000-8000-0000000000aa"
    assert value(samples, "tunnel_healthy", tunnel_id=odd) == 0
    assert all(
        value(samples, "tunnel_status", tunnel_id=odd, status=status) == 0
        for status in cf.TUNNEL_STATUSES
    )
    assert "../../etc" not in str(samples)


async def test_poll_skips_unusable_numbers(fake_net: FakeNetwork) -> None:
    data = fixture("graphql_zone_traffic")
    summed = data["data"]["viewer"]["zones"][0]["httpRequests1hGroups"][0]["sum"]
    summed.update(requests=-5, cachedRequests="many", bytes=True, threats=float("inf"))
    data_text = json.dumps(data).replace("Infinity", "1e999")
    serve(fake_net, graphql=body(json.loads(data_text)))
    samples = await run_poll(cfg())
    for metric in ("zone_requests", "zone_cached_requests", "zone_bytes", "zone_threats"):
        assert value(samples, metric, zone_id=ZONE_1) == 0, metric


async def test_poll_ignores_zones_it_did_not_ask_for(fake_net: FakeNetwork) -> None:
    data = fixture("graphql_zone_traffic")
    data["data"]["viewer"]["zones"].append(
        {"zoneTag": "f" * 32, "httpRequests1hGroups": [{"sum": {"requests": 1}}]}
    )
    serve(fake_net, graphql=body(data))
    samples = await run_poll(cfg())
    assert "f" * 32 not in str(samples)


@pytest.mark.parametrize(
    "answer",
    [
        body({"data": None, "errors": [{"message": "not authorized example-detail"}]}),
        error_answer(403),
        error_answer(500),
        http_response(200, b"not json"),
        body({"data": {"viewer": {}}}),
    ],
)
async def test_poll_analytics_failure_keeps_the_rest(
    fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture, answer: bytes
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net, graphql=answer)
    samples = await run_poll(cfg())
    assert value(samples, "analytics_up") == 0
    assert "zone_requests" not in names(samples)
    assert value(samples, "zone_active", zone_id=ZONE_1) == 1
    assert value(samples, "tunnel_healthy", tunnel_id=TUNNEL_1) == 1
    assert "an analytics query failed" in caplog.text
    assert_no_secret(caplog.text, samples)


async def test_poll_without_tunnels_or_analytics(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg(include_tunnels=False, include_analytics=False))
    assert [r.target for r in fake_net.requests] == [ZONES_TARGET]
    assert not {n for n in names(samples) if n.startswith(("tunnel", "analytics"))}
    assert "zone_requests" not in names(samples)


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"zones": error_answer(403)}, CloudflareAuthFailed, cf._msg_auth(403)),
        ({"zones": error_answer(401)}, CloudflareAuthFailed, cf._msg_auth(401)),
        ({"zones": error_answer(500)}, CloudflareRequestFailed, cf._msg_status(500)),
        ({"zones": http_response(200, b"<html>")}, CloudflareBadAnswer, cf._MSG_BAD_ANSWER),
        ({"tunnels": error_answer(403)}, CloudflareTunnelsForbidden, cf._msg_tunnels(403)),
        ({"tunnels": error_answer(502)}, CloudflareRequestFailed, cf._msg_status(502)),
    ],
)
async def test_poll_failures_raise(
    fake_net: FakeNetwork, overrides: dict[str, bytes], error: type[Exception], message: str
) -> None:
    serve(fake_net, **overrides)
    with pytest.raises(error) as caught:
        await run_poll(cfg())
    assert str(caught.value) == message
    assert_no_secret(caught.value)


async def test_poll_tls_failure(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    fake_net.tls_error = httpcore.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] example-detail")
    with pytest.raises(CloudflareTlsUntrusted) as caught:
        await run_poll(cfg())
    assert_no_secret(caught.value)


async def test_poll_unreachable(fake_net: FakeNetwork) -> None:
    fake_net.fail(ADDRESS, PORT, httpcore.ConnectError("refused"))
    with pytest.raises(CloudflareUnreachable):
        await run_poll(cfg())


async def test_poll_timeout(fake_net: FakeNetwork) -> None:
    fake_net.serve(ADDRESS, PORT, hang)
    with pytest.raises(CloudflareTimeout) as caught:
        await run_poll(cfg(timeout_seconds=1))
    assert str(caught.value) == f"timed out waiting for {HOST}:443"


async def test_poll_refused_by_guard(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(DNS, HOST, ["169.254.169.254"])
    with pytest.raises(NetGuardError):
        await run_poll(cfg())
    assert fake_net.connects == []


def test_poll_budget_fits_the_collector_timeout() -> None:
    assert cf.POLL_BUDGET_SECONDS < MAX_POLL_TIMEOUT_SECONDS
    assert cf.POLL_BUDGET_SECONDS < Cloudflare().poll_interval_seconds()


async def test_poll_budget_ends_a_slow_poll(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cf, "POLL_BUDGET_SECONDS", 0.2)
    fake_net.serve(ADDRESS, PORT, hang)
    with pytest.raises(CloudflarePollTooSlow):
        await run_poll(cfg(timeout_seconds=5))


# --- rate limits -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("120", 120),
        (" 30 ", 30),
        ("0", 1),
        ("999999", 3600),
        ("Wed, 21 Oct 2026 07:28:00 GMT", 300),
        ("-5", 300),
        ("١٢", 300),  # non-ASCII digits
        (None, 300),
    ],
)
def test_retry_after_is_bounded(header: str | None, seconds: int) -> None:
    assert cf.retry_after_seconds(header) == seconds


@pytest.mark.parametrize("target", ["zones", "tunnels", "graphql"])
async def test_poll_429_backs_off_for_retry_after(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(cf, "_monotonic", lambda: clock[0])
    serve(fake_net, **{target: error_answer(429, [("Retry-After", "120")])})
    ctx = make_ctx(Cloudflare, cfg())
    with pytest.raises(CloudflareRateLimited) as caught:
        await Cloudflare().poll(ctx)
    assert str(caught.value) == cf._msg_rate_limited(120)
    sent = len(fake_net.requests)
    # During the backoff a poll sends nothing and says how long it still waits.
    clock[0] += 100
    with pytest.raises(CloudflareRateLimited) as waiting:
        await Cloudflare().poll(ctx)
    assert str(waiting.value) == cf._msg_waiting(20)
    assert len(fake_net.requests) == sent
    # Another instance (another token, or the same account set up twice) isn't held back.
    serve(fake_net)
    assert await run_poll(cfg())
    # Once Retry-After has passed, the instance polls again.
    clock[0] += 21
    assert await Cloudflare().poll(ctx)
    assert cf._backoff_until == {}


async def test_poll_graphql_rate_limit_error_backs_off(fake_net: FakeNetwork) -> None:
    limited = body(
        {"data": None, "errors": [{"message": "rate limiter budget depleted, try again later"}]}
    )
    serve(fake_net, graphql=limited)
    ctx = make_ctx(Cloudflare, cfg())
    with pytest.raises(CloudflareRateLimited) as caught:
        await Cloudflare().poll(ctx)
    assert str(caught.value) == cf._msg_rate_limited(cf.DEFAULT_BACKOFF_SECONDS)
    assert cf.backoff_remaining(ctx.instance_id) > 0


def test_request_budget_stays_far_inside_cloudflare_limits() -> None:
    """Per instance and 5 minutes: 2 REST requests (limit 1200) and at most 5 GraphQL
    queries (limit 300), at the largest zone cap."""
    assert Cloudflare().poll_interval_seconds() == 300
    queries = -(-cf.MAX_ZONES // cf.ZONES_PER_QUERY)
    assert queries <= 5


# --- labels ----------------------------------------------------------------------------


async def test_free_text_labels_only_on_info_series(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg())
    texts = {"example.com", "example.net", "example.org", "example-home", "example-lab"}
    for sample in samples:
        assert set(sample.labels) <= OWN_LABELS | FREE_TEXT_LABELS, sample
        if set(sample.labels) & FREE_TEXT_LABELS:
            assert sample.name in (cf.METRIC_ZONE_INFO, cf.METRIC_TUNNEL_INFO), sample
            assert sample.value == 1.0
        else:
            assert not set(sample.labels.values()) & texts, sample
    infos = [s for s in samples if s.name.endswith("_info")]
    assert len(infos) == 6  # 3 zones, 3 tunnels


async def test_names_are_cleaned_and_capped(fake_net: FakeNetwork) -> None:
    zones = fixture("zones")
    zones["result"][0]["name"] = "evil\nzone‮" + "x" * 300
    tunnels = fixture("tunnels")
    tunnels["result"][0]["name"] = "home\x00\ttunnel\r\n\u0007" + "y" * 300
    serve(fake_net, zones=body(zones), tunnels=body(tunnels))
    samples = await run_poll(cfg())
    [zone_info] = [
        s for s in samples if s.name == cf.METRIC_ZONE_INFO and s.labels["zone_id"] == ZONE_1
    ]
    [tunnel_info] = [
        s for s in samples if s.name == cf.METRIC_TUNNEL_INFO and s.labels["tunnel_id"] == TUNNEL_1
    ]
    for text in (zone_info.labels["zone_name"], tunnel_info.labels["tunnel_name"]):
        assert len(text) <= 128 and text.isprintable()
    assert zone_info.labels["zone_name"].startswith("evil zone")
    assert tunnel_info.labels["tunnel_name"].startswith("home tunnel")


# --- through the poll collector --------------------------------------------------------


async def collect(
    engine: Engine, config: dict[str, Any], *, site: str | None
) -> tuple[MetricsStore, PollSpec]:
    box = SecretBox(TEST_MASTER_KEY)
    instance_id = uuid.uuid4()
    stored, secrets = ic.split(CloudflareConfig.model_validate(config))
    spec = PollSpec(
        instance_id=instance_id,
        type_id="cloudflare",
        name="cf-1",
        site=site,
        updated_at=utcnow(),
        config=stored,
        secrets=ic.encrypt(box, instance_id, secrets),
    )
    store = MetricsStore()
    scheduler = PollScheduler(
        session_factory=make_session_factory(engine),
        registry=Registry([Cloudflare]),
        secret_box=box,
        net_guard_factory=fake_guards,
        store=store,
    )
    store.track(spec.instance_id, spec.labels)
    await scheduler.poll_once(spec, Cloudflare())
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
        f'vantage_instance="cf-1",vantage_instance_id="{spec.instance_id}",'
        f'vantage_integration="cloudflare",vantage_site="{label}"'
    )
    assert f'{PREFIX}_zone_requests{{{std},zone_id="{ZONE_1}"}} 12000.0' in text
    assert f'{PREFIX}_tunnel_healthy{{tunnel_id="{TUNNEL_1}",{std}}} 1.0' in text
    assert f"vantage_instance_up{{{std}}} 1.0" in text
    assert f"vantage_poll_dropped_samples{{{std}}} 0.0" in text
    assert_no_secret(text, caplog.text)


async def test_collector_marks_bad_token_down(
    fake_net: FakeNetwork, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net, zones=error_answer(403))
    store, spec = await collect(engine, cfg(), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error == cf._msg_auth(403)
    text = generate_latest(make_registry(store)).decode()
    assert f"{PREFIX}_zone_requests" not in text
    assert_no_secret(status, text, caplog.text)


async def test_collector_marks_rate_limited_down(fake_net: FakeNetwork, engine: Engine) -> None:
    serve(fake_net, zones=error_answer(429, [("Retry-After", "60")]))
    store, spec = await collect(engine, cfg(), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error == cf._msg_rate_limited(60)


async def test_collector_reports_guard_refusal(
    fake_net: FakeNetwork, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(DNS, HOST, ["10.0.0.9"])
    store, spec = await collect(engine, cfg(), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error is not None and status.error.startswith("refused by the network guard")
    assert fake_net.connects == []


# --- through the instance API ----------------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_net: FakeNetwork) -> Iterator[TestClient]:
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def test_api_create_test_read_and_update(admin: TestClient, fake_net: FakeNetwork) -> None:
    serve(fake_net)
    bodies = []
    r = admin.post("/api/v1/instances/test", json={"type_id": "cloudflare", "config": cfg()})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    bodies.append(r.text)
    created = admin.post(
        "/api/v1/instances", json={"type_id": "cloudflare", "name": "cf-1", "config": cfg()}
    )
    assert created.status_code == 201, created.text
    bodies.append(created.text)
    instance = created.json()
    assert "api_token" not in instance["config"]
    assert instance["config"]["account_id"] == ACCOUNT
    assert instance["secrets_set"] == {"api_token": True}
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
        {"card_type": "cloudflare-zones", "title": "Cloudflare zones", "default_size": [4, 3]},
        {"card_type": "cloudflare-tunnels", "title": "Cloudflare tunnels", "default_size": [4, 2]},
    ]
    patched = admin.patch(
        f"/api/v1/instances/{instance_id}",
        json={"config": {"account_id": ACCOUNT, "max_zones": 5}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["secrets_set"] == {"api_token": True}
    bodies.append(patched.text)
    bodies.append(admin.get("/api/v1/integrations").text)
    bodies.append(admin.get("/api/v1/audit").text)
    assert_no_secret(*bodies)
    assert {(r.address, r.port) for r in fake_net.requests} == {(ADDRESS, PORT)}
    assert_read_only(fake_net)


def test_api_validation_error_holds_no_secret(admin: TestClient) -> None:
    bad = "example-token with a space"
    r = admin.post(
        "/api/v1/instances",
        json={"type_id": "cloudflare", "name": "cf-1", "config": cfg(api_token=bad)},
    )
    assert r.status_code == 422
    assert bad not in r.text and "example-token" not in r.text


def test_api_serves_tcp_hint_on_timeout(admin: TestClient, fake_net: FakeNetwork) -> None:
    fake_net.fail(ADDRESS, PORT, httpcore.ConnectTimeout("timed out"))
    r = admin.post("/api/v1/instances/test", json={"type_id": "cloudflare", "config": cfg()})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["ok"] is False and result["hint"] == TCP_HINT
    assert_no_secret(r.text)


def test_api_test_bad_token(admin: TestClient, fake_net: FakeNetwork) -> None:
    serve(fake_net, zones=error_answer(403))
    r = admin.post("/api/v1/instances/test", json={"type_id": "cloudflare", "config": cfg()})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "refused the API token" in r.json()["message"]
    assert_no_secret(r.text)


# --- static parts ----------------------------------------------------------------------


def test_firewall_hints_cards_and_interval() -> None:
    ctx = make_ctx(Cloudflare, cfg())
    assert [h.model_dump() for h in Cloudflare().firewall_hints(ctx)] == [TCP_HINT]
    assert [c.card_type for c in Cloudflare().cards()] == ["cloudflare-zones", "cloudflare-tunnels"]
    assert Cloudflare().poll_interval_seconds() == 300
    assert Cloudflare.category == "cloud" and Cloudflare.cloud_hosts == (HOST,)


def test_window_is_the_last_complete_utc_hour() -> None:
    start, end = cf.analytics_window(dt.datetime(2026, 10, 29, 0, 5, tzinfo=dt.UTC))
    assert (start, end) == (
        dt.datetime(2026, 10, 28, 23, tzinfo=dt.UTC),
        dt.datetime(2026, 10, 29, 0, tzinfo=dt.UTC),
    )
    local = dt.datetime(2026, 10, 29, 13, 5, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert cf.analytics_window(local)[1] == dt.datetime(2026, 10, 29, 11, tzinfo=dt.UTC)


def test_fixtures_hold_only_example_values() -> None:
    for path in FIXTURES.glob("cloudflare_*.json"):
        text = path.read_text()
        for domain in re.findall(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+\.[a-z]{2,}", text):
            assert domain.endswith(("example.com", "example.net", "example.org")), domain
        for address in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
            assert address.startswith("192.0.2."), address


async def test_parse_runs_off_the_event_loop_for_large_answers(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    original = asyncio.to_thread

    async def spy(func: Any, *args: Any) -> Any:
        calls.append(len(args[0]))
        return await original(func, *args)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    tunnels = fixture("tunnels")
    tunnels["result"][0]["metadata"] = {"padding": "z" * 100_000}
    serve(fake_net, tunnels=body(tunnels))
    await run_poll(cfg())
    assert calls and calls[0] > cf._PARSE_IN_THREAD_BYTES

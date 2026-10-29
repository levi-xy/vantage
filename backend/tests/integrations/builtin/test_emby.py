"""Emby: schema, connection test, poll parsing, read-only paths, no secret anywhere.

Requests go through the real SSRF guard and the real guarded client; only the socket
layer is fake (``fake_net``). Answers are the hand-written ``fixtures/emby_*.json``
files, with example values only.
"""

import asyncio
import inspect
import json
import logging
import ssl
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpcore
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine

from app.integrations.base import Sample, TestResult
from app.integrations.builtin.emby import (
    Emby,
    EmbyAuthFailed,
    EmbyBadAnswer,
    EmbyConfig,
    EmbyForbidden,
    EmbyPollTooSlow,
    EmbyRequestFailed,
    EmbyTimeout,
    EmbyTlsUntrusted,
    EmbyUnreachable,
)
from app.integrations.builtin.emby import integration as emby
from app.integrations.schema import config_json_schema
from tests.integrations.builtin.test_proxmox import closing, make_ca_pem
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
HOST = "emby.example.internal"
ADDRESS = "10.0.0.60"
PORT = 8096
API_KEY = "example-token-0123456789abcdef"
PREFIX = "vantage_emby"
HINT = {
    "protocol": "tcp",
    "port": PORT,
    "destination": HOST,
    "reason": "The Vantage backend calls the Emby API (HTTP) on this port.",
}
PRIVATE_TEXT = (
    "example-alice", "example-bob", "example-laptop", "192.0.2.1", "Example Movie",
    "example-emby", "Example Movies", "/example/",
)  # fmt: skip

INFO = emby.PATH_INFO
SESSIONS = emby.PATH_SESSIONS
COUNTS = emby.PATH_COUNTS
LIBRARIES = emby.PATH_LIBRARIES


def cfg(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"host": HOST, "api_key": API_KEY}
    config.update(overrides)
    return config


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"emby_{name}.json").read_text())


def body(data: Any) -> bytes:
    return http_response(
        200, json.dumps(data).encode(), headers=[("Content-Type", "application/json")]
    )


def serve(fake_net: FakeNetwork, port: int = PORT, **overrides: bytes) -> None:
    answers = {
        INFO: body(fixture("system_info")),
        SESSIONS: body(fixture("sessions")),
        COUNTS: body(fixture("items_counts")),
        LIBRARIES: body(fixture("virtual_folders")),
    }
    keys = {"info": INFO, "sessions": SESSIONS, "counts": COUNTS, "libraries": LIBRARIES}
    for key, value in overrides.items():
        answers[keys[key]] = value

    async def handler(request: FakeRequest) -> bytes:
        answer = answers.get(request.target)
        return closing(answer or http_response(501, b"{}", reason="Not Implemented"))

    fake_net.serve(ADDRESS, port, handler)


def error(code: int) -> bytes:
    return http_response(code, b"{}", reason="Error")


async def run_test(config: dict[str, Any]) -> TestResult:
    return await Emby().test_connection(make_ctx(Emby, config))


async def run_poll(config: dict[str, Any]) -> list[Sample]:
    return await Emby().poll(make_ctx(Emby, config))


def assert_read_only(fake_net: FakeNetwork) -> None:
    """Only GET, only allowlisted targets, only to the checked address, key in the
    header and nowhere else."""
    assert fake_net.requests
    for request in fake_net.requests:
        assert request.method == "GET", request
        assert emby.allowed_path(request.target), request.target
        assert (request.address, request.port) == (ADDRESS, PORT)
        assert request.headers["x-emby-token"] == API_KEY
        assert API_KEY not in request.target
        assert "api_key" not in request.target.lower()


def assert_no_secret(*texts: object) -> None:
    for text in texts:
        assert API_KEY not in str(text)


def by_name(samples: list[Sample]) -> dict[str, list[Sample]]:
    found: dict[str, list[Sample]] = {}
    for s in samples:
        found.setdefault(s.name.removeprefix(PREFIX + "_"), []).append(s)
    return found


def labelled(samples: list[Sample], metric: str, label: str) -> dict[str, float]:
    return {s.labels[label]: s.value for s in by_name(samples)[metric]}


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults_and_the_only_secret() -> None:
    config = EmbyConfig.model_validate(cfg())
    assert (config.port, config.use_https, config.timeout_seconds) == (8096, False, 10)
    assert config.tls_verify() is True
    schema = config_json_schema(EmbyConfig)
    assert schema["properties"]["api_key"]["writeOnly"] is True
    assert [k for k, v in schema["properties"].items() if v.get("writeOnly")] == ["api_key"]
    assert API_KEY not in repr(config) and API_KEY not in config.model_dump_json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "10.0.0.60", "port": 8920, "use_https": True},
        {"use_https": True, "tls_insecure_skip_verify": True},
        {"use_https": True, "tls_ca_certificate": make_ca_pem()},
    ],
)
def test_schema_accepts(overrides: dict[str, Any]) -> None:
    EmbyConfig.model_validate(cfg(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": ""},
        {"host": "http://emby.example.internal"},
        {"port": 0},
        {"port": 70000},
        {"timeout_seconds": 16},
        {"api_key": ""},
        {"api_key": "has space"},
        {"api_key": "line\nbreak"},
        {"api_key": "x" * 129},
        {"tls_insecure_skip_verify": True},  # http: nothing to verify
        {"tls_ca_certificate": make_ca_pem()},
        {"use_https": True, "tls_insecure_skip_verify": True, "tls_ca_certificate": make_ca_pem()},
        {"use_https": True, "tls_ca_certificate": "not a certificate"},
    ],
)
def test_schema_rejects(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as exc:
        EmbyConfig.model_validate(cfg(**overrides))
    assert_no_secret(exc.value)


def test_tls_choices() -> None:
    assert (
        EmbyConfig.model_validate(cfg(use_https=True, tls_insecure_skip_verify=True)).tls_verify()
        is False
    )
    pinned = EmbyConfig.model_validate(cfg(use_https=True, tls_ca_certificate=make_ca_pem()))
    assert isinstance(pinned.tls_verify(), ssl.SSLContext)


# --- allowlist -------------------------------------------------------------------------


@pytest.mark.parametrize("target", [INFO, SESSIONS, COUNTS, LIBRARIES])
def test_allowlist_accepts(target: str) -> None:
    assert emby.allowed_path(target)


@pytest.mark.parametrize(
    "target",
    [
        "/emby/Sessions",
        "/emby/Sessions/s1/Playing/Stop",
        "/emby/Sessions/s1/Message",
        "/emby/Sessions?ActiveWithinSeconds=1&api_key=example-token",
        "/emby/Library/Refresh",
        "/emby/Library/VirtualFolders/Delete",
        "/emby/Items/123",
        "/emby/Items/123/Delete",
        "/emby/System/Restart",
        "/emby/System/Shutdown",
        "/emby/Users/Authenticate",
        "/emby/Users",
        "/System/Info",
        "emby/System/Info",
        "/emby/System/Info/../Restart",
        "/emby/System/Info ",
        "http://192.0.2.1/emby/System/Info",
        "",
    ],
)
def test_allowlist_refuses(target: str) -> None:
    assert not emby.allowed_path(target)


async def test_get_refuses_a_path_off_the_allowlist(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    config = EmbyConfig.model_validate(cfg())
    async with emby._client(make_ctx(Emby, cfg()), config) as client:
        with pytest.raises(ValueError, match="allowlisted"):
            await emby._get_json(client, "/emby/System/Restart", 5)
    assert fake_net.requests == [] and fake_net.connects == []


async def test_poll_and_test_send_only_allowlisted_gets(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    await run_test(cfg())
    await run_poll(cfg())
    assert_read_only(fake_net)
    assert {r.target for r in fake_net.requests} == {INFO, SESSIONS, COUNTS, LIBRARIES}


def test_no_write_endpoint_in_the_code() -> None:
    """Hard rule 2: nothing in the integration could send a write."""
    source = inspect.getsource(emby)
    for word in (
        '"POST"', '"PUT"', '"DELETE"', '"PATCH"', ".post(", ".put(", ".delete(", ".patch(",
        ".request(", "/Playing", "/Message", "/Refresh", "/Restart", "/Shutdown",
        "Authenticate", "api_key=",
    ):  # fmt: skip
        assert word not in source, word
    for pattern in emby.ALLOWED_PATHS:
        assert pattern.pattern.startswith("/emby/")


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_net: FakeNetwork, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_net)
    result = await run_test(cfg())
    assert result.ok, result
    assert result.message == "Emby 4.8.10.0: 4 active sessions"
    assert result.hint is None
    assert [r.target for r in fake_net.requests] == [INFO, SESSIONS]
    assert_read_only(fake_net)
    assert fake_net.tls == []  # http
    assert_no_secret(result, caplog.text)


async def test_connection_https_verifies_by_default(fake_net: FakeNetwork) -> None:
    serve(fake_net, port=8920)
    # The fake serves the plain bytes after the (fake) handshake.
    result = await run_test(cfg(use_https=True, port=8920))
    assert result.ok, result
    for server_name, context in fake_net.tls:
        assert server_name == HOST
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


async def test_connection_https_pinned_and_insecure(fake_net: FakeNetwork) -> None:
    serve(fake_net, port=8920)
    assert (await run_test(cfg(use_https=True, port=8920, tls_ca_certificate=make_ca_pem()))).ok
    assert fake_net.tls[-1][1].check_hostname
    assert (await run_test(cfg(use_https=True, port=8920, tls_insecure_skip_verify=True))).ok
    assert fake_net.tls[-1][1].verify_mode == ssl.CERT_NONE


@pytest.mark.parametrize(("code", "text"), [(401, "refused the API key"), (403, "not allowed")])
async def test_connection_auth_statuses(fake_net: FakeNetwork, code: int, text: str) -> None:
    serve(fake_net, info=error(code))
    result = await run_test(cfg())
    assert not result.ok and text in result.message and f"{HOST}:8096" in result.message
    assert result.hint is None
    assert_no_secret(result)


async def test_connection_other_status(fake_net: FakeNetwork) -> None:
    serve(fake_net, info=error(500))
    result = await run_test(cfg())
    assert not result.ok and "HTTP 500" in result.message


async def test_connection_tls_not_trusted(fake_net: FakeNetwork) -> None:
    serve(fake_net, port=8920)
    fake_net.tls_error = httpcore.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed (_ssl.c:1000)"
    )
    result = await run_test(cfg(use_https=True, port=8920))
    assert not result.ok and result.hint is None
    assert result.message.startswith("certificate not trusted")
    assert "_ssl.c" not in result.message
    assert fake_net.requests == []  # the key was never sent


@pytest.mark.parametrize(
    "err", [httpcore.ConnectTimeout("timed out"), httpcore.ConnectError("refused")]
)
async def test_connection_failure_gives_tcp_hint(fake_net: FakeNetwork, err: Exception) -> None:
    fake_net.fail(ADDRESS, PORT, err)
    result = await run_test(cfg())
    assert not result.ok and f"{HOST}:8096" in result.message
    assert result.hint is not None and result.hint.model_dump() == HINT


async def test_connection_hung_server_times_out_with_hint(fake_net: FakeNetwork) -> None:
    fake_net.serve(ADDRESS, PORT, hang)
    result = await asyncio.wait_for(run_test(cfg(timeout_seconds=1)), 5)
    assert result.message == f"timed out waiting for {HOST}:8096"
    assert result.hint is not None and result.hint.model_dump() == HINT


async def test_connection_hint_uses_configured_port_and_scheme(fake_net: FakeNetwork) -> None:
    fake_net.fail(ADDRESS, 8920, httpcore.ConnectTimeout("timed out"))
    result = await run_test(cfg(port=8920, use_https=True))
    assert result.hint is not None
    assert result.hint.port == 8920 and "HTTPS" in result.hint.reason


@pytest.mark.parametrize("host", ["127.0.0.1", "192.0.2.50", "169.254.169.254"])
async def test_connection_refused_by_guard(fake_net: FakeNetwork, host: str) -> None:
    result = await run_test(cfg(host=host))
    assert not result.ok and result.message.startswith("refused by the network guard")
    assert result.hint is None and fake_net.connects == []


GARBAGE = [
    b"",
    b"<html>nope</html>",
    b"\x00\xff\xfe",
    b"[" * 100_000,
    b"null",
    b"42",
    b'"text"',
    b"[1,2,3]",
    b'{"Version": ["a"]}',
    b'{"Version": 4}',
]


@pytest.mark.parametrize("raw", GARBAGE)
async def test_connection_garbage_never_raises(fake_net: FakeNetwork, raw: bytes) -> None:
    serve(fake_net, info=body_raw(raw), sessions=body_raw(raw))
    result = await run_test(cfg())
    assert isinstance(result, TestResult)
    assert_no_secret(result)
    if not result.ok:
        assert "not like the Emby API" in result.message


def body_raw(raw: bytes) -> bytes:
    return http_response(200, raw, headers=[("Content-Type", "application/json")])


async def test_connection_sessions_garbage_after_good_info(fake_net: FakeNetwork) -> None:
    serve(fake_net, sessions=body({"not": "a list"}))
    result = await run_test(cfg())
    assert not result.ok and "not like the Emby API" in result.message


async def test_connection_version_that_is_not_a_version_is_left_out(
    fake_net: FakeNetwork,
) -> None:
    serve(fake_net, info=body({"Version": "<b>evil</b>\n" + "9" * 100}))
    result = await run_test(cfg())
    assert result.ok and result.message == "Emby: 4 active sessions"


# --- poll ------------------------------------------------------------------------------


async def test_poll_parses_the_fixtures(fake_net: FakeNetwork) -> None:
    serve(fake_net)
    samples = await run_poll(cfg())
    assert_read_only(fake_net)
    found = by_name(samples)
    [info] = found["server_info"]
    assert info.labels == {"version": "4.8.10.0"} and info.value == 1
    assert found["update_available"][0].value == 1
    assert found["restart_pending"][0].value == 0
    assert found["sessions"][0].value == 4
    assert found["now_playing"][0].value == 3
    assert labelled(samples, "streams", "play_method") == {
        "transcode": 1, "direct_play": 1, "direct_stream": 1, "unknown": 0,
    }  # fmt: skip
    items = labelled(samples, "items", "item_type")
    assert items["movie"] == 120 and items["episode"] == 900 and items["song"] == 500
    assert "trailer" in items and "game" not in items  # absent fields give no sample
    libs = labelled(samples, "libraries", "library_type")
    assert libs["movies"] == libs["tvshows"] == libs["music"] == libs["other"] == 1
    assert libs["books"] == 0
    assert all(s.type == "gauge" for s in samples)


async def test_poll_nothing_playing(fake_net: FakeNetwork) -> None:
    serve(fake_net, sessions=body([]))
    samples = await run_poll(cfg())
    found = by_name(samples)
    assert found["sessions"][0].value == 0 and found["now_playing"][0].value == 0
    assert set(labelled(samples, "streams", "play_method").values()) == {0}


async def test_poll_unknown_play_method_is_counted_unknown(fake_net: FakeNetwork) -> None:
    sessions = [
        {"NowPlayingItem": {}, "PlayState": {"PlayMethod": "Teleport"}},
        {"NowPlayingItem": {}, "PlayState": "garbage"},
        {"NowPlayingItem": {}},
        "garbage",
        None,
    ]
    serve(fake_net, sessions=body(sessions))
    samples = await run_poll(cfg())
    assert by_name(samples)["sessions"][0].value == 3
    assert labelled(samples, "streams", "play_method")["unknown"] == 3


async def test_poll_optional_requests_may_fail(fake_net: FakeNetwork) -> None:
    serve(fake_net, counts=error(403), libraries=body_raw(b"<html>"))
    samples = await run_poll(cfg())
    found = by_name(samples)
    assert "items" not in found and "libraries" not in found
    assert "sessions" in found and "server_info" in found


async def test_poll_optional_wrong_shape_is_skipped(fake_net: FakeNetwork) -> None:
    serve(fake_net, counts=body([1]), libraries=body({"a": 1}))
    samples = await run_poll(cfg())
    assert "items" not in by_name(samples) and "libraries" not in by_name(samples)


async def test_poll_skips_unusable_values(fake_net: FakeNetwork) -> None:
    counts = {"MovieCount": -1, "SeriesCount": "7", "EpisodeCount": True, "SongCount": 5.0,
              "AlbumCount": float("nan") if False else 1e30, "BookCount": None}  # fmt: skip
    serve(fake_net, counts=body(counts), info=body({"Version": 5, "HasUpdateAvailable": "yes"}))
    samples = await run_poll(cfg())
    assert labelled(samples, "items", "item_type") == {"song": 5.0}
    assert by_name(samples)["server_info"][0].labels == {"version": ""}
    assert "update_available" not in by_name(samples)


async def test_poll_session_cap(fake_net: FakeNetwork) -> None:
    serve(fake_net, sessions=body([{"NowPlayingItem": {}}] * (emby.MAX_SESSIONS + 50)))
    samples = await run_poll(cfg())
    assert by_name(samples)["sessions"][0].value == emby.MAX_SESSIONS


async def test_metrics_carry_counts_only(fake_net: FakeNetwork) -> None:
    """No user, device, address, title, server or library name reaches a sample."""
    serve(fake_net)
    samples = await run_poll(cfg())
    allowed = {"version", "play_method", "item_type", "library_type"}
    for s in samples:
        assert set(s.labels) <= allowed, s
        text = s.name + "".join(f"{k}={v}" for k, v in s.labels.items())
        for private in PRIVATE_TEXT:
            assert private not in text
    values = {v for s in samples for v in s.labels.values()}
    assert values <= {
        "4.8.10.0", "transcode", "direct_play", "direct_stream", "unknown", "other",
        *emby.ITEM_TYPES.values(), *emby.LIBRARY_TYPES.values(),
    }  # fmt: skip


@pytest.mark.parametrize("raw", GARBAGE)
async def test_poll_garbage_raises_only_a_poll_failure(fake_net: FakeNetwork, raw: bytes) -> None:
    serve(fake_net, info=body_raw(raw))
    try:
        await run_poll(cfg())
    except EmbyBadAnswer as exc:
        assert API_KEY not in str(exc) and "not like the Emby API" in str(exc)


@pytest.mark.parametrize("which", ["info", "sessions"])
async def test_poll_required_garbage_is_bad_answer(fake_net: FakeNetwork, which: str) -> None:
    serve(fake_net, **{which: body("a string")})
    with pytest.raises(EmbyBadAnswer):
        await run_poll(cfg())


@pytest.mark.parametrize(
    ("code", "exc_type"),
    [(401, EmbyAuthFailed), (403, EmbyForbidden), (500, EmbyRequestFailed)],
)
async def test_poll_statuses_raise(fake_net: FakeNetwork, code: int, exc_type: type) -> None:
    serve(fake_net, sessions=error(code))
    with pytest.raises(exc_type) as exc:
        await run_poll(cfg())
    assert_no_secret(exc.value)
    assert f"{HOST}:8096" in str(exc.value)


async def test_poll_body_too_large(fake_net: FakeNetwork) -> None:
    serve(fake_net, sessions=body_raw(b"[" + b"1," * emby.MAX_BODY_BYTES + b"1]"))
    with pytest.raises(EmbyBadAnswer):
        await run_poll(cfg())


async def test_poll_transport_failures(fake_net: FakeNetwork) -> None:
    fake_net.fail(ADDRESS, PORT, httpcore.ConnectError("refused"))
    with pytest.raises(EmbyUnreachable):
        await run_poll(cfg())
    fake_net.fail(ADDRESS, PORT, httpcore.ConnectTimeout("timed out"))
    with pytest.raises(EmbyTimeout):
        await run_poll(cfg())


async def test_poll_tls_failure(fake_net: FakeNetwork) -> None:
    serve(fake_net, port=8920)
    fake_net.tls_error = httpcore.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] bad")
    with pytest.raises(EmbyTlsUntrusted):
        await run_poll(cfg(use_https=True, port=8920))


async def test_poll_refused_by_guard(fake_net: FakeNetwork) -> None:
    from app.core.net_guard import NetGuardError

    with pytest.raises(NetGuardError):
        await run_poll(cfg(host="127.0.0.1"))
    assert fake_net.connects == []


async def test_poll_budget_ends_a_slow_poll(
    fake_net: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emby, "POLL_BUDGET_SECONDS", 0.3)
    fake_net.serve(ADDRESS, PORT, hang)
    with pytest.raises(EmbyPollTooSlow) as exc:
        await asyncio.wait_for(run_poll(cfg(timeout_seconds=10)), 5)
    assert f"{HOST}:8096" in str(exc.value)


def test_poll_budget_fits_the_collector_timeout() -> None:
    assert emby.POLL_BUDGET_SECONDS < 60


# --- static parts and secrets ----------------------------------------------------------


def test_firewall_hints_cards_and_interval() -> None:
    ctx = make_ctx(Emby, cfg())
    assert [h.model_dump() for h in Emby().firewall_hints(ctx)] == [HINT]
    [hint] = Emby().firewall_hints(make_ctx(Emby, cfg(host="fd00::60", port=8920)))
    assert (hint.destination, hint.port) == ("fd00::60", 8920)
    assert [c.card_type for c in Emby().cards()] == ["emby-overview", "emby-libraries"]
    assert Emby().poll_interval_seconds() == 30
    assert Emby.category == "media" and Emby.cloud_hosts == ()


async def test_key_reaches_only_the_token_header() -> None:
    config = EmbyConfig.model_validate(cfg())
    async with emby._client(make_ctx(Emby, cfg()), config) as client:
        assert client.headers["X-Emby-Token"] == API_KEY
        assert API_KEY not in str(client.base_url)
        assert str(client.base_url).startswith("http://")
        assert client.follow_redirects is False


# --- through the API -------------------------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_net: FakeNetwork) -> Iterator[TestClient]:
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def test_api_create_test_read_and_update(admin: TestClient, fake_net: FakeNetwork) -> None:
    serve(fake_net)
    bodies = []
    r = admin.post("/api/v1/instances/test", json={"type_id": "emby", "config": cfg()})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    bodies.append(r.text)
    created = admin.post(
        "/api/v1/instances", json={"type_id": "emby", "name": "emby-1", "config": cfg()}
    )
    assert created.status_code == 201, created.text
    bodies.append(created.text)
    instance = created.json()
    assert "api_key" not in instance["config"]
    assert instance["secrets_set"] == {"api_key": True}
    iid = instance["id"]
    bodies.append(admin.get(f"/api/v1/instances/{iid}").text)
    tested = admin.post(f"/api/v1/instances/{iid}/test")
    assert tested.json()["ok"] is True, tested.text
    bodies.append(tested.text)
    cards = admin.get(f"/api/v1/instances/{iid}/cards")
    assert [c["card_type"] for c in cards.json()] == ["emby-overview", "emby-libraries"]
    patched = admin.patch(
        f"/api/v1/instances/{iid}", json={"config": {"host": HOST, "timeout_seconds": 5}}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["secrets_set"] == {"api_key": True}
    bodies += [
        patched.text,
        admin.get("/api/v1/integrations").text,
        admin.get("/api/v1/audit").text,
    ]
    assert_no_secret(*bodies)
    assert {(r.address, r.port) for r in fake_net.requests} == {(ADDRESS, PORT)}


def test_api_refuses_denied_host_and_bad_key_without_echo(
    admin: TestClient, fake_net: FakeNetwork
) -> None:
    r = admin.post(
        "/api/v1/instances",
        json={"type_id": "emby", "name": "e", "config": cfg(host="127.0.0.1")},
    )
    assert r.status_code == 422 and fake_net.connects == []
    bad = "example-token with a space"
    r = admin.post(
        "/api/v1/instances", json={"type_id": "emby", "name": "e", "config": cfg(api_key=bad)}
    )
    assert r.status_code == 422
    assert bad not in r.text and "example-token" not in r.text


def test_api_serves_tcp_hint_on_timeout(admin: TestClient, fake_net: FakeNetwork) -> None:
    fake_net.fail(ADDRESS, PORT, httpcore.ConnectTimeout("timed out"))
    r = admin.post("/api/v1/instances/test", json={"type_id": "emby", "config": cfg()})
    assert r.status_code == 200 and r.json()["hint"] == HINT
    assert_no_secret(r.text)

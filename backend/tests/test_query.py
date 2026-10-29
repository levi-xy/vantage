"""PromQL query proxy (M5.1): main path, budget refusals, Prometheus failures, security."""

import logging
from collections.abc import Callable, Iterator, Sequence
from typing import Any
from urllib.parse import parse_qs

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.deps import CSRF_HEADER
from app.core.config import ConfigError, load_settings
from app.core.net_guard import NetGuard, NetPolicy
from app.models import Role
from app.query.proxy import MAX_SERIES, PrometheusClient
from tests.test_auth import add_user, do_setup, login, make_client

QUERY = "/api/v1/query"
# A made-up internal address; it must never show up in a response or log line.
PROM_URL = "http://prometheus.internal.example:9090"
NOW = 1_700_000_000.0

Handler = Callable[[httpx.Request], httpx.Response]


class FakePrometheus:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = lambda _r: ok_matrix(1)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def form(self, i: int = -1) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.requests[i].content.decode()).items()}


def ok_matrix(n: int) -> httpx.Response:
    result = [
        {"metric": {"__name__": "up", "i": str(i)}, "values": [[NOW, "1"], [NOW + 60, "NaN"]]}
        for i in range(n)
    ]
    return httpx.Response(
        200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
    )


@pytest.fixture
def prom() -> FakePrometheus:
    return FakePrometheus()


@pytest.fixture
def admin(engine: Engine, db_url: str, prom: FakePrometheus) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        # Budget, shape and error tests fake Prometheus at the httpx level; the guarded
        # client itself is covered by the net_guard tests below.
        c.app.state.prometheus = PrometheusClient(  # type: ignore[attr-defined]
            httpx.AsyncClient(base_url=PROM_URL, transport=httpx.MockTransport(prom))
        )
        me = do_setup(c)
        c.headers[CSRF_HEADER] = me["csrf_token"]
        yield c


def test_range_query(admin: TestClient, prom: FakePrometheus) -> None:
    r = admin.post(QUERY, json={"query": "up", "start": NOW, "end": NOW + 3600, "step": 60})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "result_type": "matrix",
        "series": [
            {"labels": {"__name__": "up", "i": "0"}, "values": [[NOW, "1"], [NOW + 60, "NaN"]]}
        ],
        "warnings": [],
    }
    sent = prom.requests[0]
    assert str(sent.url) == f"{PROM_URL}/api/v1/query_range"
    form = prom.form()
    assert form["query"] == "up"
    assert float(form["step"]) == 60
    assert form["limit"] == str(MAX_SERIES + 1)
    assert form["timeout"] == "10s"


def test_instant_query_any_user(admin: TestClient, session: Session, prom: FakePrometheus) -> None:
    prom.handler = lambda _r: httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"job": "x"}, "value": [NOW, "3"]}],
            },
            "warnings": ["PromQL info: example"],
        },
    )
    add_user(session, "viewer", Role.VIEWER)
    viewer = TestClient(admin.app, base_url="https://testserver")
    viewer.headers[CSRF_HEADER] = login(viewer, "viewer")["csrf_token"]
    r = viewer.post(QUERY, json={"query": "sum(up)"})
    assert r.status_code == 200, r.text
    assert r.json()["series"] == [{"labels": {"job": "x"}, "values": [[NOW, "3"]]}]
    assert r.json()["warnings"] == ["PromQL info: example"]
    assert str(prom.requests[0].url) == f"{PROM_URL}/api/v1/query"
    assert "start" not in prom.form()


def test_login_and_csrf_required(admin: TestClient, prom: FakePrometheus) -> None:
    anon = TestClient(admin.app, base_url="https://testserver")
    assert anon.post(QUERY, json={"query": "up"}).status_code == 401
    no_csrf = admin.post(QUERY, json={"query": "up"}, headers={CSRF_HEADER: "wrong"})
    assert no_csrf.status_code == 403
    assert prom.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"query": "up", "start": NOW, "end": NOW + 32 * 86400, "step": 3600},  # range too long
        {"query": "up", "start": NOW, "end": NOW + 3600, "step": 5},  # step too small
        {"query": "up", "start": NOW, "end": NOW + 86400, "step": 15},  # too many points
        {"query": "up", "start": NOW + 60, "end": NOW},  # end before start
        {"query": "up", "start": NOW},  # end missing
        {"query": "up", "step": 60},  # step without a range
        {"query": "x" * 2001},  # query too long
        {"query": "up", "url": "http://192.0.2.10/"},  # no way to steer the target
    ],
)
def test_over_budget_refused(admin: TestClient, prom: FakePrometheus, body: dict[str, Any]) -> None:
    r = admin.post(QUERY, json=body)
    assert r.status_code == 422, r.text
    assert prom.requests == []


def test_default_step_stays_in_budget(admin: TestClient, prom: FakePrometheus) -> None:
    r = admin.post(QUERY, json={"query": "up", "start": NOW, "end": NOW + 31 * 86400})
    assert r.status_code == 200, r.text
    assert float(prom.form()["step"]) >= 31 * 86400 / 1099


def test_too_many_series(admin: TestClient, prom: FakePrometheus) -> None:
    prom.handler = lambda _r: ok_matrix(MAX_SERIES + 1)
    r = admin.post(QUERY, json={"query": "up", "start": NOW, "end": NOW + 600})
    assert r.status_code == 422
    assert "series" in r.json()["detail"]


def _raise_connect(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"connection refused to {PROM_URL}", request=request)


INTERNAL = "parse error: internal detail from prometheus.internal.example"


@pytest.mark.parametrize(
    ("handler", "status", "detail"),
    [
        (_raise_connect, 503, "metrics backend unavailable"),
        (
            lambda _r: httpx.Response(
                400, json={"status": "error", "errorType": "bad_data", "error": INTERNAL}
            ),
            400,
            "invalid PromQL query",
        ),
        (
            lambda _r: httpx.Response(
                503, json={"status": "error", "errorType": "timeout", "error": INTERNAL}
            ),
            504,
            "query timed out",
        ),
        (
            lambda _r: httpx.Response(502, text=f"<html>{INTERNAL}</html>"),
            502,
            "metrics backend error",
        ),
    ],
)
def test_prometheus_failures_leak_nothing(
    admin: TestClient,
    prom: FakePrometheus,
    caplog: pytest.LogCaptureFixture,
    handler: Handler,
    status: int,
    detail: str,
) -> None:
    prom.handler = handler
    with caplog.at_level(logging.DEBUG):
        r = admin.post(QUERY, json={"query": "up"})
    assert r.status_code == status
    assert r.json() == {"detail": detail}
    # Our own log lines (httpx's request log names the configured URL; that's fine).
    ours = " ".join(rec.getMessage() for rec in caplog.records if rec.name.startswith("app."))
    for text in (r.text, ours):
        assert "prometheus.internal.example" not in text
        assert "9090" not in text


# --- through net_guard -----------------------------------------------------------------

# Fake DNS for the guard: the compose network is RFC 1918; loopback is always denied.
GUARD_DNS = {"prometheus": ["192.168.255.243"], "loopback.example": ["127.0.0.1"]}


async def fake_resolver(host: str, _port: int) -> Sequence[str]:
    return GUARD_DNS[host]


def guarded(url: str, backend: httpcore.AsyncNetworkBackend) -> PrometheusClient:
    guard = NetGuard(NetPolicy(own_addresses=frozenset()), resolver=fake_resolver)
    return PrometheusClient.guarded(url, guard, network_backend=backend)


def test_guarded_client_reaches_compose_prometheus(admin: TestClient) -> None:
    body = b'{"status":"success","data":{"resultType":"scalar","result":[1700000000,"1"]}}'
    backend = httpcore.AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: application/json\r\n",
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n",
            body,
        ]
    )
    admin.app.state.prometheus = guarded("http://prometheus:9090", backend)  # type: ignore[attr-defined]
    r = admin.post(QUERY, json={"query": "1"})
    assert r.status_code == 200, r.text
    assert r.json()["series"] == [{"labels": {}, "values": [[NOW, "1"]]}]


def test_guard_refusal_is_a_clear_503(admin: TestClient) -> None:
    admin.app.state.prometheus = guarded(  # type: ignore[attr-defined]
        "http://loopback.example:9090", httpcore.AsyncMockBackend([])
    )
    r = admin.post(QUERY, json={"query": "up"})
    assert r.status_code == 503
    assert r.json() == {"detail": "metrics backend refused by the network guard"}
    assert "loopback.example" not in r.text and "127.0.0.1" not in r.text


def test_prometheus_url_setting() -> None:
    assert load_settings({}).prometheus_url == "http://prometheus:9090"
    assert load_settings({"VANTAGE_PROMETHEUS_URL": "http://10.0.0.5:9090/"}).prometheus_url == (
        "http://10.0.0.5:9090"
    )
    with pytest.raises(ConfigError) as err:
        load_settings({"VANTAGE_PROMETHEUS_URL": "http://user:example-password@10.0.0.5:9090"})
    assert "example-password" not in str(err.value)
    with pytest.raises(ConfigError):
        load_settings({"VANTAGE_PROMETHEUS_URL": "file:///etc/passwd"})

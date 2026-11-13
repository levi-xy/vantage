"""Version in the UI (maintainer-approved 2026-11-13): /me carries server_version,
/setup/status doesn't, GET /about needs a login and never leaks an address or error text."""

from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app import __version__
from app.query.proxy import PrometheusClient
from tests.test_auth import do_setup, make_client

# A made-up internal address; it must never show up in a response.
PROM_URL = "http://prometheus.internal.example:9090"

Handler = Callable[[httpx.Request], httpx.Response]


def _prometheus(handler: Handler) -> PrometheusClient:
    return PrometheusClient(
        httpx.AsyncClient(base_url=PROM_URL, transport=httpx.MockTransport(handler))
    )


def _buildinfo(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.path == "/api/v1/status/buildinfo"
    return httpx.Response(200, json={"status": "success", "data": {"version": "2.55.1"}})


@pytest.fixture
def app_client(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        yield c


def test_setup_status_does_not_expose_the_version(app_client: TestClient) -> None:
    body = app_client.get("/api/v1/setup/status").json()
    assert body == {"setup_required": True}
    assert "version" not in app_client.get("/api/v1/setup/status").text.lower()


def test_me_carries_server_version(app_client: TestClient) -> None:
    me = do_setup(app_client)
    assert me["server_version"] == __version__
    assert app_client.get("/api/v1/me").json()["server_version"] == __version__


def test_about_needs_a_login(app_client: TestClient) -> None:
    assert app_client.get("/api/v1/about").status_code == 401


def test_about_shape(app_client: TestClient) -> None:
    app_client.app.state.prometheus = _prometheus(_buildinfo)  # type: ignore[attr-defined]
    do_setup(app_client)
    r = app_client.get("/api/v1/about")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "backend_version",
        "backend_build_date",
        "schema_version",
        "prometheus_version",
        "started_at",
    }
    assert body["backend_version"] == __version__
    assert body["prometheus_version"] == "2.55.1"
    assert isinstance(body["schema_version"], str) and body["schema_version"]
    assert body["started_at"]


def test_about_prometheus_failure_is_null_and_leaks_nothing(app_client: TestClient) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused: {PROM_URL}")

    app_client.app.state.prometheus = _prometheus(refuse)  # type: ignore[attr-defined]
    do_setup(app_client)
    r = app_client.get("/api/v1/about")
    assert r.status_code == 200
    assert r.json()["prometheus_version"] is None
    assert "prometheus.internal.example" not in r.text
    assert "refused" not in r.text


def test_about_prometheus_garbage_is_null(app_client: TestClient) -> None:
    app_client.app.state.prometheus = _prometheus(  # type: ignore[attr-defined]
        lambda _r: httpx.Response(200, text="<html>not json</html>")
    )
    do_setup(app_client)
    assert app_client.get("/api/v1/about").json()["prometheus_version"] is None

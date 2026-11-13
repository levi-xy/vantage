"""Version in the UI (maintainer-approved 2026-11-13): /me carries server_version and
server_commit, /setup/status doesn't, GET /about needs a login and never leaks an address
or error text; VANTAGE_RELEASE wins over the baked build version when it is a version."""

import logging
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app import version
from app.query.proxy import PrometheusClient
from app.version import VersionInfo, read_version
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
    assert me["server_version"] == version.current().release
    assert me["server_commit"] == version.current().commit
    assert app_client.get("/api/v1/me").json()["server_version"] == version.current().release


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
        "backend_build_version",
        "backend_commit",
        "backend_build_date",
        "schema_version",
        "prometheus_version",
        "started_at",
    }
    assert body["backend_version"] == version.current().release
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


# The release version: VANTAGE_RELEASE (compose file) over the baked build version.

REV = "0123456789abcdef0123456789abcdef01234567"
RC_IMAGE = {"VANTAGE_VERSION": "0.1.1-rc.1", "VANTAGE_COMMIT": REV}


def test_a_promoted_final_reports_the_release_and_keeps_the_build() -> None:
    env = {**RC_IMAGE, "VANTAGE_RELEASE": "0.1.1", "VANTAGE_BUILD_DATE": "2026-11-14"}
    assert read_version(env) == VersionInfo(
        release="0.1.1", build="0.1.1-rc.1", commit=REV, build_date="2026-11-14"
    )


def test_without_vantage_release_the_build_version_is_reported() -> None:
    assert read_version(RC_IMAGE).release == "0.1.1-rc.1"
    assert read_version({**RC_IMAGE, "VANTAGE_RELEASE": "  "}).release == "0.1.1-rc.1"
    unstamped = read_version({})
    assert (unstamped.release, unstamped.build, unstamped.commit) == ("dev", "dev", None)


@pytest.mark.parametrize(
    "bad", ["latest", "1.2", "v1.2.3", "0.1.1; rm -rf /", "0.1.1\nX", "1.2.3-" + "a" * 80]
)
def test_a_bad_vantage_release_is_ignored_with_a_warning(
    bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.version"):
        info = read_version({**RC_IMAGE, "VANTAGE_RELEASE": bad})
    assert info.release == "0.1.1-rc.1"
    assert "VANTAGE_RELEASE is not a version" in caplog.text
    assert bad not in caplog.text  # the value itself is never logged


def test_a_bad_commit_or_date_is_unknown() -> None:
    info = read_version({"VANTAGE_COMMIT": "not-a-revision", "VANTAGE_BUILD_DATE": "soon"})
    assert info.commit is None and info.build_date is None


def test_me_and_about_report_the_release_build_and_commit(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    promoted = read_version({**RC_IMAGE, "VANTAGE_RELEASE": "0.1.1"})
    monkeypatch.setattr(version, "current", lambda: promoted)
    app_client.app.state.prometheus = _prometheus(_buildinfo)  # type: ignore[attr-defined]
    me = do_setup(app_client)
    assert (me["server_version"], me["server_commit"]) == ("0.1.1", REV)
    body = app_client.get("/api/v1/about").json()
    assert body["backend_version"] == "0.1.1"
    assert body["backend_build_version"] == "0.1.1-rc.1"
    assert body["backend_commit"] == REV

"""M8.3 security-review fixes: the Host allowlist, quiet HTTP client loggers and the
Prometheus scrape limits."""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from starlette.websockets import WebSocketDisconnect

from app.core.config import ConfigError, host_allowed, load_settings
from tests.test_auth import do_setup, make_client

PROMETHEUS_YML = Path(__file__).resolve().parents[2] / "deploy" / "prometheus" / "prometheus.yml"


@pytest.fixture
def guarded(engine: Engine, db_url: str) -> Iterator[TestClient]:
    hosts = ("testserver", "vantage.example.internal", "192.0.2.10:8443")
    with make_client(db_url, public_hosts=hosts) as c:
        do_setup(c)
        yield c


def test_public_hosts_refuse_other_hosts(guarded: TestClient) -> None:
    ok = guarded.get("/api/v1/me", headers={"host": "vantage.example.internal:8443"})
    assert ok.status_code == 200
    assert (
        guarded.get("/api/v1/setup/status", headers={"host": "192.0.2.10:8443"}).status_code == 200
    )
    for host in (
        "attacker.example",
        "192.0.2.10:9000",
        "vantage.example.internal.attacker.example",
    ):
        r = guarded.get("/api/v1/setup/status", headers={"host": host})
        assert r.status_code == 400
        assert r.json() == {"detail": "unknown host"}
    # Outside /api the allowlist doesn't apply (Prometheus uses the compose name).
    assert guarded.get("/healthz", headers={"host": "backend:8000"}).status_code == 200
    # A logged-in browser whose Origin matches the rebound Host: still refused.
    cookie = "; ".join(f"{c.name}={c.value}" for c in guarded.cookies.jar)
    with guarded.websocket_connect(
        "/api/v1/ws", headers={"origin": "https://testserver", "cookie": cookie}
    ) as sock:
        sock.send_text("ping")
        assert sock.receive_json() == {"type": "pong"}
    rebound = {"host": "attacker.example", "origin": "https://attacker.example", "cookie": cookie}
    with pytest.raises(WebSocketDisconnect):
        with guarded.websocket_connect("/api/v1/ws", headers=rebound):
            pass


def test_public_hosts_unset_accepts_any_host(engine: Engine, db_url: str) -> None:
    with make_client(db_url) as c:
        assert (
            c.get("/api/v1/setup/status", headers={"host": "anything.example"}).status_code == 200
        )


def test_public_hosts_setting() -> None:
    hosts = load_settings({"VANTAGE_PUBLIC_HOSTS": " Vantage.Example.Internal , [fd00::10]:8443 "})
    assert hosts.public_hosts == ("vantage.example.internal", "[fd00::10]:8443")
    assert host_allowed("[fd00::10]:8443", hosts.public_hosts)
    assert not host_allowed("[fd00::10]:80", hosts.public_hosts)
    assert load_settings({}).public_hosts == ()
    for bad in ("fd00::10", "host:99999", "user@host", "a/b"):
        with pytest.raises(ConfigError) as info:
            load_settings({"VANTAGE_PUBLIC_HOSTS": bad})
        assert bad not in str(info.value)


def test_http_client_loggers_never_log_request_urls(engine: Engine, db_url: str) -> None:
    # The Telegram bot token is part of the API URL; httpx logs URLs at INFO.
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    with make_client(db_url):
        pass
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_scrape_targets_job_has_generic_limits() -> None:
    text = PROMETHEUS_YML.read_text()
    job = text.split("job_name: vantage-targets", 1)[1].split("job_name:", 1)[0]
    for line in (
        "sample_limit: 50000",
        "label_limit: 64",
        "label_value_length_limit: 2048",
        "body_size_limit: 16MB",
    ):
        assert line in job

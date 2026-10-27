"""GET /metrics (M2.4): bearer auth, standard labels, end to end through the app lifespan."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.auth import Passwords
from app.core.config import Settings, normalize_database_url
from app.core.crypto import SecretBox
from app.core.db import make_session_factory
from app.core.prometheus_auth import FILE_VAR, VALUE_VAR, PrometheusToken
from app.integrations import instance_config as ic
from app.integrations.registry import Registry
from app.main import create_app
from app.models import Instance
from tests.collectors.test_scheduler import TOKEN as DEVICE_TOKEN
from tests.collectors.test_scheduler import Poller, Raiser, fake_guards
from tests.conftest import TEST_MASTER_KEY
from tests.test_auth import FAST_HASHER

PROM_TOKEN = "example-prometheus-token-0123456789abcdef"
REGISTRY = Registry([Poller, Raiser])


def build(db_url: str, prometheus_token: str | None = PROM_TOKEN) -> TestClient:
    app = create_app(
        Settings(database_url=normalize_database_url(db_url)),
        secret_box=SecretBox(TEST_MASTER_KEY),
        passwords=Passwords(FAST_HASHER),
        registry=REGISTRY,
        net_guard_factory=fake_guards,
        prometheus_token=PrometheusToken(prometheus_token) if prometheus_token else None,
    )
    return TestClient(app, base_url="https://testserver")


def add(engine: Engine, type_id: str, name: str) -> uuid.UUID:
    box = SecretBox(TEST_MASTER_KEY)
    instance_id = uuid.uuid4()
    with make_session_factory(engine)() as db:
        db.add(
            Instance(
                id=instance_id,
                type_id=type_id,
                name=name,
                site="lab",
                config={"host": "10.0.0.10", "port": 443},
                secrets=ic.encrypt(box, instance_id, {"api_token": DEVICE_TOKEN}),
            )
        )
        db.commit()
    return instance_id


def auth(token: str = PROM_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer example-wrong-token-0123456789abcdef"},
        {"Authorization": f"Basic {PROM_TOKEN}"},
        {"Authorization": PROM_TOKEN},
    ],
)
def test_requires_bearer_token(engine: Engine, db_url: str, headers: dict[str, str]) -> None:
    client = build(db_url)
    r = client.get("/metrics", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert PROM_TOKEN not in r.text
    assert "vantage_" not in r.text


def test_refuses_everything_without_configured_token(
    engine: Engine, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(VALUE_VAR, raising=False)
    monkeypatch.delenv(FILE_VAR, raising=False)
    client = build(db_url, prometheus_token=None)
    for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer None"}):
        r = client.get("/metrics", headers=headers)
        assert r.status_code == 503  # fail closed, same as /sd/targets
        assert "vantage_" not in r.text


def test_token_read_from_environment(
    engine: Engine, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(FILE_VAR, raising=False)
    monkeypatch.setenv(VALUE_VAR, PROM_TOKEN)
    client = build(db_url, prometheus_token=None)
    assert client.get("/metrics", headers=auth()).status_code == 200


def test_response_headers(engine: Engine, db_url: str) -> None:
    client = build(db_url)
    r = client.get("/metrics", headers=auth())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["cache-control"] == "no-store"


def test_polls_end_to_end(engine: Engine, db_url: str) -> None:
    good = add(engine, "poller", "nas-1")
    bad = add(engine, "raiser", "fw-1")
    with build(db_url) as client:  # the lifespan starts the poll scheduler
        deadline = time.monotonic() + 5
        while True:
            text = client.get("/metrics", headers=auth()).text
            if text.count("vantage_instance_up{") == 2 or time.monotonic() > deadline:
                break
            time.sleep(0.02)

    std_good = (
        f'vantage_instance="nas-1",vantage_instance_id="{good}",'
        'vantage_integration="poller",vantage_site="lab"'
    )
    std_bad = (
        f'vantage_instance="fw-1",vantage_instance_id="{bad}",'
        'vantage_integration="raiser",vantage_site="lab"'
    )
    assert f"vantage_instance_up{{{std_good}}} 1.0" in text
    assert f"vantage_instance_up{{{std_bad}}} 0.0" in text
    assert f'vantage_poller_temp{{s="cpu",{std_good}}} 40.0' in text
    assert DEVICE_TOKEN not in text
    assert PROM_TOKEN not in text
    assert TEST_MASTER_KEY.decode() not in text

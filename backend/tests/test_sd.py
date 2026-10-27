"""Prometheus HTTP service discovery (M2.3): ``GET /sd/targets``.

Covers the bearer auth, the HTTP SD format with the four standard labels, enabled-only
filtering, SSRF checks on every target, isolation of broken instances, and the security
assertion: no secret (instance secrets or the Prometheus token) in a response or log.
"""

import ipaddress
import logging
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import Engine

from app.core.crypto import SecretBox
from app.core.db import make_session_factory
from app.core.net_guard import NetGuard, NetPolicy
from app.core.prometheus_auth import (
    FILE_VAR,
    VALUE_VAR,
    PrometheusToken,
    PrometheusTokenError,
    load_prometheus_token,
)
from app.integrations import instance_config as ic
from app.integrations.base import InstanceContext, Integration, ScrapeTarget
from app.integrations.registry import Registry
from app.models import Instance
from app.sd.targets import split_address
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.fake import FakeIntegration
from tests.test_auth import do_setup, make_client

PROM_TOKEN = "example-prometheus-token-0123456789abcdef"
DEVICE_TOKEN = "example-token-sd-4e2a9b"
SECRETS = (PROM_TOKEN, DEVICE_TOKEN)

DNS: dict[str, list[str]] = {
    "node.example.internal": ["10.0.0.40"],
    "rebind.example.internal": ["10.0.0.41", "127.0.0.1"],
    "metadata.example.internal": ["169.254.169.254"],
    "api.example.com": ["10.0.0.30"],
}


async def fake_resolver(host: str, port: int) -> Sequence[str]:
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


def fake_guards(cls: type[Integration]) -> NetGuard:
    return NetGuard(
        NetPolicy(own_addresses=frozenset()), cloud_hosts=cls.cloud_hosts, resolver=fake_resolver
    )


class SdConfig(BaseModel):
    host: str
    port: int = 9100
    api_token: SecretStr
    metrics_path: str = "/metrics"
    scheme: Literal["http", "https"] = "http"
    extra: list[dict[str, str]] = Field(default_factory=list)


class Exporter(FakeIntegration):
    """Serves one target from host/port, plus any ``extra`` ones, as configured."""

    type_id = "sd_exporter"
    display_name = "SD exporter"
    config_schema = SdConfig

    def scrape_targets(self, ctx: InstanceContext) -> list[ScrapeTarget]:
        cfg = ctx.config_as(SdConfig)
        targets = [
            ScrapeTarget(
                address=f"{cfg.host}:{cfg.port}", metrics_path=cfg.metrics_path, scheme=cfg.scheme
            )
        ]
        return targets + [ScrapeTarget.model_validate(t) for t in cfg.extra]

    def firewall_hints(self, ctx: InstanceContext) -> list[Any]:
        return []


class CloudExporter(Exporter):
    type_id = "sd_cloud"
    cloud_hosts = ("api.example.com",)


class Raising(Exporter):
    type_id = "sd_raising"

    def scrape_targets(self, ctx: InstanceContext) -> list[ScrapeTarget]:
        token = ctx.config_as(SdConfig).api_token.get_secret_value()
        raise RuntimeError(f"broken with {token}")


# FakeIntegration itself has no scrape targets (poll-only).
REGISTRY = Registry([Exporter, CloudExporter, Raising, FakeIntegration])


@pytest.fixture
def client(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url, registry=REGISTRY, net_guard_factory=fake_guards) as c:
        c.app.state.prometheus_token = PrometheusToken(PROM_TOKEN)  # type: ignore[attr-defined]
        c.headers["Authorization"] = f"Bearer {PROM_TOKEN}"
        yield c


def add(
    engine: Engine,
    name: str,
    *,
    type_id: str = "sd_exporter",
    enabled: bool = True,
    site: str | None = "lab",
    secrets: dict[str, str] | None = None,
    **config: Any,
) -> uuid.UUID:
    """Store an instance directly, bypassing the API's host checks on purpose."""
    instance_id = uuid.uuid4()
    box = SecretBox(TEST_MASTER_KEY)
    with make_session_factory(engine)() as db:
        db.add(
            Instance(
                id=instance_id,
                type_id=type_id,
                name=name,
                site=site,
                tags=["t"],
                enabled=enabled,
                config={"host": "10.0.0.5", **config},
                secrets=ic.encrypt(
                    box, instance_id, {"api_token": DEVICE_TOKEN} if secrets is None else secrets
                ),
            )
        )
        db.commit()
    return instance_id


def targets(client: TestClient) -> list[dict[str, Any]]:
    resp = client.get("/sd/targets")
    assert resp.status_code == 200, resp.text
    assert_no_secrets(resp.text)
    body: list[dict[str, Any]] = resp.json()
    return body


def addresses(groups: list[dict[str, Any]]) -> list[str]:
    return [a for g in groups for a in g["targets"]]


def assert_no_secrets(text: str) -> None:
    for value in SECRETS:
        assert value not in text


# --- auth ------------------------------------------------------------------------------


def test_requires_bearer_token(client: TestClient) -> None:
    for headers in (
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer example-wrong-token-0123456789abcdef"},
        {"Authorization": f"Bearer {PROM_TOKEN}x"},
        {"Authorization": f"Basic {PROM_TOKEN}"},
        {"Authorization": PROM_TOKEN},
        {"Authorization": f"Bearer {'a' * 5000}"},
    ):
        resp = client.get("/sd/targets", headers=headers)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"
        assert_no_secrets(resp.text)


def test_session_login_is_not_enough(engine: Engine, db_url: str) -> None:
    with make_client(db_url, registry=REGISTRY, net_guard_factory=fake_guards) as c:
        c.app.state.prometheus_token = PrometheusToken(PROM_TOKEN)  # type: ignore[attr-defined]
        do_setup(c)  # logged in as admin, with a session cookie
        assert c.get("/sd/targets").status_code == 401


def test_bearer_scheme_is_case_insensitive(client: TestClient) -> None:
    resp = client.get("/sd/targets", headers={"Authorization": f"bearer {PROM_TOKEN}"})
    assert resp.status_code == 200


def test_no_token_configured_fails_closed(engine: Engine, db_url: str) -> None:
    with make_client(db_url, registry=REGISTRY, net_guard_factory=fake_guards) as c:
        c.app.state.prometheus_token = None  # type: ignore[attr-defined]
        for headers in (
            {},
            {"Authorization": "Bearer "},
            {"Authorization": f"Bearer {PROM_TOKEN}"},
        ):
            assert c.get("/sd/targets", headers=headers).status_code == 503


# --- format ----------------------------------------------------------------------------


def test_empty_when_no_instances(client: TestClient) -> None:
    resp = client.get("/sd/targets")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == []


def test_http_sd_format_with_standard_labels(client: TestClient, engine: Engine) -> None:
    node = add(engine, "node-1", host="10.0.0.5", port=9100)
    https = add(
        engine,
        "node-2",
        host="node.example.internal",
        port=9443,
        site=None,
        metrics_path="/probe/metrics",
        scheme="https",
    )
    assert targets(client) == [
        {
            "targets": ["10.0.0.5:9100"],
            "labels": {
                "__metrics_path__": "/metrics",
                "__scheme__": "http",
                "vantage_instance_id": str(node),
                "vantage_instance": "node-1",
                "vantage_integration": "sd_exporter",
                "vantage_site": "lab",
            },
        },
        {
            "targets": ["node.example.internal:9443"],
            "labels": {
                "__metrics_path__": "/probe/metrics",
                "__scheme__": "https",
                "vantage_instance_id": str(https),
                "vantage_instance": "node-2",
                "vantage_integration": "sd_exporter",
                "vantage_site": "",
            },
        },
    ]


def test_only_enabled_instances_with_targets(client: TestClient, engine: Engine) -> None:
    add(engine, "on", host="10.0.0.5")
    add(engine, "off", host="10.0.0.6", enabled=False)
    add(engine, "poll-only", type_id="fake_device", port=8443)
    assert addresses(targets(client)) == ["10.0.0.5:9100"]


def test_targets_grouped_per_path_and_scheme(client: TestClient, engine: Engine) -> None:
    add(
        engine,
        "multi",
        host="10.0.0.5",
        extra=[
            {"address": "10.0.0.6:9100"},
            {"address": "10.0.0.5:9100"},  # duplicate, listed once
            {"address": "10.0.0.7:9182", "metrics_path": "/other"},
            {"address": "[fd00::5]:9100", "scheme": "https"},
        ],
    )
    groups = targets(client)
    by_key = {(g["labels"]["__metrics_path__"], g["labels"]["__scheme__"]): g for g in groups}
    assert by_key[("/metrics", "http")]["targets"] == ["10.0.0.5:9100", "10.0.0.6:9100"]
    assert by_key[("/other", "http")]["targets"] == ["10.0.0.7:9182"]
    assert by_key[("/metrics", "https")]["targets"] == ["[fd00::5]:9100"]
    assert all(g["labels"]["vantage_instance"] == "multi" for g in groups)


# --- SSRF guard ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1:9100",
        "169.254.169.254:80",
        "0.0.0.0:9100",
        "[::1]:9100",
        "[fe80::1]:9100",
        "[fd00:ec2::254]:80",
        "100.100.100.200:80",
        "224.0.0.1:9100",
        "rebind.example.internal:9100",  # one good answer next to a loopback one
        "metadata.example.internal:80",
        "unknown.example.internal:9100",  # doesn't resolve: fail closed
        "2130706433:9100",  # ambiguous numeric host
    ],
)
def test_guard_refused_targets_are_left_out(
    client: TestClient, engine: Engine, address: str
) -> None:
    add(engine, "good", host="10.0.0.5")
    add(engine, "bad", host="10.0.0.9", extra=[{"address": address}])
    assert addresses(targets(client)) == ["10.0.0.9:9100", "10.0.0.5:9100"]  # by name


def test_cloud_host_must_use_https(client: TestClient, engine: Engine) -> None:
    add(engine, "cloud", type_id="sd_cloud", host="api.example.com", port=443, scheme="http")
    assert targets(client) == []


def test_host_own_addresses_are_refused(engine: Engine, db_url: str) -> None:
    def guards(cls: type[Integration]) -> NetGuard:
        own = NetPolicy(own_addresses=frozenset({ipaddress.ip_address("10.0.0.5")}))
        return NetGuard(own, cloud_hosts=cls.cloud_hosts, resolver=fake_resolver)

    with make_client(db_url, registry=REGISTRY, net_guard_factory=guards) as c:
        c.app.state.prometheus_token = PrometheusToken(PROM_TOKEN)  # type: ignore[attr-defined]
        c.headers["Authorization"] = f"Bearer {PROM_TOKEN}"
        add(engine, "self", host="10.0.0.5")
        add(engine, "other", host="10.0.0.6")
        assert addresses(targets(c)) == ["10.0.0.6:9100"]


# --- broken instances ------------------------------------------------------------------


def test_broken_instances_are_isolated(
    client: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    add(engine, "a-good", host="10.0.0.5")
    add(engine, "b-raises", type_id="sd_raising")
    add(engine, "c-unknown-type", type_id="removed_integration")
    add(engine, "d-missing-secret", secrets={})
    bad_blob = add(engine, "e-bad-secrets")
    add(engine, "f-bad-config", port="not-a-port")
    add(engine, "g-bad-path", host="10.0.0.6", metrics_path="metrics?x=1")
    with make_session_factory(engine)() as db:
        row = db.get(Instance, bad_blob)
        assert row is not None
        row.secrets = b"\x01" + b"\x00" * 40  # can't be decrypted
        db.commit()

    with caplog.at_level(logging.DEBUG):
        assert addresses(targets(client)) == ["10.0.0.5:9100"]
    assert "RuntimeError" in caplog.text
    assert_no_secrets(caplog.text)


@pytest.mark.parametrize(
    "address",
    [
        "",
        "10.0.0.5",  # no port
        "10.0.0.5:",
        "10.0.0.5:0",
        "10.0.0.5:99999",
        "user:pw@10.0.0.5:9100",
        "10.0.0.5:9100/metrics",
        "10.0.0.5:9100?x=1",
        "10.0.0.5:9100#x",
        "10.0.0.5 :9100",
        "10.0.0.5:9100\n",
        "[fe80::1%eth0]:9100",
        "http://10.0.0.5:9100",
    ],
)
def test_malformed_addresses_are_left_out(client: TestClient, engine: Engine, address: str) -> None:
    add(engine, "good", host="10.0.0.5", extra=[{"address": address}])
    assert addresses(targets(client)) == ["10.0.0.5:9100"]


def test_split_address() -> None:
    assert split_address("10.0.0.5:9100") == ("10.0.0.5", 9100)
    assert split_address("node.example.internal:443") == ("node.example.internal", 443)
    assert split_address("[fd00::5]:9100") == ("fd00::5", 9100)
    assert split_address("10.0.0.5") is None
    assert split_address("a@10.0.0.5:1") is None


def test_secrets_never_in_response_or_logs(
    client: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    add(engine, "node", host="10.0.0.5")
    add(engine, "raising", type_id="sd_raising")
    with caplog.at_level(logging.DEBUG):
        resp = client.get("/sd/targets")
        bad = client.get("/sd/targets", headers={"Authorization": "Bearer nope"})
    assert_no_secrets(resp.text + bad.text + caplog.text)
    assert "api_token" not in resp.text


# --- token loading ---------------------------------------------------------------------


def test_load_token_from_file(tmp_path: Path) -> None:
    path = tmp_path / "prometheus_token"
    path.write_text(PROM_TOKEN + "\n")
    token = load_prometheus_token({FILE_VAR: str(path), VALUE_VAR: "ignored"})
    assert token is not None
    assert token.matches(PROM_TOKEN)
    assert not token.matches(PROM_TOKEN[:-1])
    assert PROM_TOKEN not in repr(token)


def test_load_token_from_env() -> None:
    token = load_prometheus_token({VALUE_VAR: f"  {PROM_TOKEN} "})
    assert token is not None and token.matches(PROM_TOKEN)


def test_no_token_configured() -> None:
    assert load_prometheus_token({}) is None
    assert load_prometheus_token({VALUE_VAR: "  ", FILE_VAR: ""}) is None


@pytest.mark.parametrize("value", ["short-example-token", "", "example token with spaces ok?"])
def test_bad_token_file_refuses_start(tmp_path: Path, value: str) -> None:
    path = tmp_path / "prometheus_token"
    path.write_text(value)
    with pytest.raises(PrometheusTokenError) as exc:
        load_prometheus_token({FILE_VAR: str(path)})
    assert value.strip() == "" or value not in str(exc.value)


def test_unreadable_token_file_refuses_start(tmp_path: Path) -> None:
    with pytest.raises(PrometheusTokenError, match=FILE_VAR):
        load_prometheus_token({FILE_VAR: str(tmp_path / "missing")})


def test_short_env_token_refuses_start() -> None:
    with pytest.raises(PrometheusTokenError) as exc:
        load_prometheus_token({VALUE_VAR: "example-short"})
    assert "example-short" not in str(exc.value)


def test_create_app_reads_token_from_env(
    engine: Engine, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VALUE_VAR, PROM_TOKEN)
    monkeypatch.delenv(FILE_VAR, raising=False)
    with make_client(db_url, registry=REGISTRY) as c:
        assert c.get("/sd/targets").status_code == 401
        ok = c.get("/sd/targets", headers={"Authorization": f"Bearer {PROM_TOKEN}"})
        assert ok.status_code == 200


def test_create_app_refuses_bad_token(
    engine: Engine, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VALUE_VAR, "example-short")
    monkeypatch.delenv(FILE_VAR, raising=False)
    with pytest.raises(PrometheusTokenError):
        make_client(db_url, registry=REGISTRY)

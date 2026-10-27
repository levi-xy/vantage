"""Instance API (M2.2): CRUD, test endpoints, config/secrets split, SSRF checks, audit.

The security assertion that runs through every test: no secret value ever appears in a
response body, a log line or the audit log.
"""

import asyncio
import logging
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api import instances as instances_api
from app.api.deps import CSRF_HEADER
from app.collectors.store import MetricsStore
from app.core.crypto import SecretBox, row_context
from app.core.net_guard import NetGuard, NetGuardError, NetPolicy
from app.integrations import instance_config as ic
from app.integrations.base import FirewallHint, InstanceContext, Integration, TestResult
from app.integrations.registry import Registry
from app.models import AuditLog, Instance, Role
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.fake import FakeCloudIntegration, FakeConfig, FakeIntegration
from tests.test_auth import add_user, do_setup, login, make_client

API = "/api/v1"
TOKEN = "example-token-7f3a91c2"
NEW_TOKEN = "example-token-new-55e0b1"
COMMUNITY = "example-community-c4d2"
SECRETS = (TOKEN, NEW_TOKEN, COMMUNITY, "example-default-secret-5a4b")

# Fake DNS, so host checks never touch the network. RFC 1918 answers are allowed.
DNS: dict[str, list[str]] = {
    "nas.example.internal": ["10.0.0.20"],
    "rebind.example.internal": ["10.0.0.21", "127.0.0.1"],
    "metadata.example.internal": ["169.254.169.254"],
    "api.example.com": ["10.0.0.30"],  # a cloud host pointed at the LAN
    # IDN hosts, by the ASCII (punycode) form the resolver is asked for.
    "xn--e1afmkfd.example": ["10.0.0.25"],  # пример.example
    "xn--e1aybc.example": ["169.254.169.254"],  # тест.example
}


async def fake_resolver(host: str, port: int) -> Sequence[str]:
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


def fake_guards(cls: type[Integration]) -> NetGuard:
    # No own addresses: the test machine's real addresses must not affect results.
    return NetGuard(
        NetPolicy(own_addresses=frozenset()), cloud_hosts=cls.cloud_hosts, resolver=fake_resolver
    )


class Probe(FakeIntegration):
    """A test-only integration whose test_connection does what the test asks."""

    type_id = "probe_device"
    display_name = "Probe"
    mode: ClassVar[str] = "ok"
    seen: ClassVar[list[InstanceContext]] = []

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        Probe.seen.append(ctx)
        token = ctx.config_as(FakeConfig).api_token.get_secret_value()
        if Probe.mode == "echo":  # a buggy integration that echoes its secret
            return TestResult(ok=True, message=f"logged in with {token}")
        if Probe.mode == "raise":
            raise RuntimeError(f"failed with token {token}")
        if Probe.mode == "hang":
            await asyncio.sleep(10)
        if Probe.mode == "http_timeout":
            raise httpx.ConnectTimeout(f"timed out, token {token}")
        if Probe.mode == "guard":
            await ctx.net_guard.resolve("127.0.0.1", 443)
        return TestResult(ok=True, message="reachable")


class UrlConfig(BaseModel):
    api_url: str
    api_token: SecretStr


class UrlDevice(FakeIntegration):
    """A test-only integration configured by URL instead of host."""

    type_id = "url_device"
    display_name = "URL device"
    config_schema = UrlConfig

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return []


class ScrapeOnly(FakeIntegration):
    """A test-only integration with nothing to poll (Prometheus scrapes it)."""

    type_id = "scrape_only"
    display_name = "Scrape only"
    poll = Integration.poll  # type: ignore[assignment]


REGISTRY = Registry([FakeIntegration, FakeCloudIntegration, Probe, UrlDevice, ScrapeOnly])


def fake_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"host": "10.0.0.10", "port": 8443, "api_token": TOKEN}
    config.update(overrides)
    return config


def body(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type_id": "fake_device",
        "name": "nas-1",
        "site": "lab",
        "tags": ["storage"],
        "config": fake_config(),
    }
    data.update(overrides)
    return data


def assert_no_secrets(text: str) -> None:
    for value in SECRETS:
        assert value not in text
    assert "**********" not in text  # not even Pydantic's mask


@pytest.fixture(autouse=True)
def _reset_probe() -> None:
    Probe.mode = "ok"
    Probe.seen.clear()


@pytest.fixture
def admin(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url, registry=REGISTRY, net_guard_factory=fake_guards) as c:
        me = do_setup(c)
        c.headers[CSRF_HEADER] = me["csrf_token"]
        yield c


@pytest.fixture
def viewer(admin: TestClient, session: Session) -> TestClient:
    add_user(session, "viewer", Role.VIEWER)
    c = TestClient(admin.app, base_url="https://testserver")
    me = login(c, "viewer")
    c.headers[CSRF_HEADER] = me["csrf_token"]
    return c


def create(client: TestClient, **overrides: Any) -> dict[str, Any]:
    r = client.post(f"{API}/instances", json=body(**overrides))
    assert r.status_code == 201, r.text
    assert_no_secrets(r.text)
    data: dict[str, Any] = r.json()
    return data


def stored(session: Session, instance_id: str) -> Instance:
    session.expire_all()
    row = session.get(Instance, uuid.UUID(instance_id))
    assert row is not None
    return row


def stored_secrets(row: Instance) -> dict[str, str]:
    assert row.secrets is not None
    return SecretBox(TEST_MASTER_KEY).decrypt_secrets(
        row.secrets, context=row_context("instances", row.id)
    )


def audit_rows(session: Session) -> list[AuditLog]:
    session.expire_all()
    return list(session.scalars(select(AuditLog).where(AuditLog.target_type == "instance")))


# --- create and read -------------------------------------------------------------------


def test_create_splits_config_and_secrets(admin: TestClient, session: Session) -> None:
    data = create(admin)
    assert data["type_id"] == "fake_device"
    assert data["name"] == "nas-1"
    assert data["site"] == "lab"
    assert data["tags"] == ["storage"]
    assert data["enabled"] is True
    assert data["config"] == {"host": "10.0.0.10", "port": 8443, "verify_tls": True}
    # The defaulted secret counts as set; the optional one wasn't sent.
    assert data["secrets_set"] == {
        "api_token": True,
        "client_secret": True,
        "snmp_community": False,
    }

    row = stored(session, data["id"])
    assert row.config == {"host": "10.0.0.10", "port": 8443, "verify_tls": True}
    assert row.secrets is not None and TOKEN.encode() not in row.secrets
    assert stored_secrets(row) == {
        "api_token": TOKEN,
        "client_secret": "example-default-secret-5a4b",
    }


def test_blob_is_bound_to_its_row(admin: TestClient, session: Session) -> None:
    a = stored(session, create(admin)["id"])
    b = stored(session, create(admin, name="nas-2")["id"])
    # Copying one row's blob into another row makes it undecryptable there.
    b.secrets = a.secrets
    session.commit()
    r = admin.post(f"{API}/instances/{b.id}/test")
    assert r.status_code == 409
    assert "decrypted" in r.json()["detail"]


def test_list_and_get(admin: TestClient) -> None:
    first = create(admin, name="b-host")
    create(admin, name="a-host", config=fake_config(snmp_community=COMMUNITY))
    r = admin.get(f"{API}/instances")
    assert r.status_code == 200
    assert_no_secrets(r.text)
    assert [i["name"] for i in r.json()] == ["a-host", "b-host"]
    assert r.json()[0]["secrets_set"]["snmp_community"] is True

    r = admin.get(f"{API}/instances/{first['id']}")
    assert r.status_code == 200
    assert r.json() == first
    assert_no_secrets(r.text)


def test_get_unknown_and_malformed_id(admin: TestClient) -> None:
    assert admin.get(f"{API}/instances/{uuid.uuid4()}").status_code == 404
    assert admin.get(f"{API}/instances/not-a-uuid").status_code == 422


def test_cards(admin: TestClient) -> None:
    data = create(admin)
    r = admin.get(f"{API}/instances/{data['id']}/cards")
    assert r.status_code == 200
    assert r.json() == [{"card_type": "generic-status", "title": "Status", "default_size": [2, 2]}]
    assert admin.get(f"{API}/instances/{uuid.uuid4()}/cards").status_code == 404


def test_extra_config_fields_are_dropped(admin: TestClient, session: Session) -> None:
    data = create(admin, config=fake_config(password="example-password-x1"))
    row = stored(session, data["id"])
    assert "password" not in row.config
    assert "example-password-x1" not in str(row.config)


# --- validation ------------------------------------------------------------------------


def test_create_rejects_invalid_config_without_echoing_it(admin: TestClient) -> None:
    config = fake_config(port=f"not-a-port {TOKEN}")
    del config["api_token"]
    config["snmp_community"] = COMMUNITY
    r = admin.post(f"{API}/instances", json=body(config=config))
    assert r.status_code == 422
    assert_no_secrets(r.text)
    assert "not-a-port" not in r.text
    locs = {tuple(e["loc"]) for e in r.json()["detail"]}
    assert locs == {("body", "config", "port"), ("body", "config", "api_token")}
    assert admin.get(f"{API}/instances").json() == []


def test_create_rejects_secret_of_wrong_type(admin: TestClient) -> None:
    r = admin.post(f"{API}/instances", json=body(config=fake_config(api_token=12345)))
    assert r.status_code == 422
    assert "12345" not in r.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"type_id": "no_such_type"},
        {"name": ""},
        {"name": "x" * 129},
        {"tags": ["ok", ""]},
        {"unexpected": True},
    ],
)
def test_create_rejects_bad_fields(admin: TestClient, overrides: dict[str, Any]) -> None:
    r = admin.post(f"{API}/instances", json=body(**overrides))
    assert r.status_code == 422
    assert_no_secrets(r.text)


def test_duplicate_name_conflicts(admin: TestClient) -> None:
    create(admin)
    r = admin.post(f"{API}/instances", json=body())
    assert r.status_code == 409
    assert_no_secrets(r.text)


# --- SSRF guard ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",  # loopback
        "169.254.169.254",  # cloud metadata
        "192.0.2.10",  # public (documentation) range, not allowed for a LAN device
        "0.0.0.0",  # noqa: S104 - a host the guard must refuse, not a bind address
        "::1",
        "2130706433",  # ambiguous numeric form of 127.0.0.1
        "rebind.example.internal",  # one good answer next to a loopback one
        "metadata.example.internal",
        "unknown.example.internal",  # unresolvable: refused, fail closed
    ],
)
def test_create_refuses_denied_hosts(admin: TestClient, session: Session, host: str) -> None:
    r = admin.post(f"{API}/instances", json=body(config=fake_config(host=host)))
    assert r.status_code == 422, r.text
    [error] = r.json()["detail"]
    assert error["loc"] == ["body", "config", "host"]
    assert error["type"] == "net_guard"
    assert_no_secrets(r.text)
    assert session.scalars(select(Instance)).all() == []


def test_create_accepts_private_hostname(admin: TestClient) -> None:
    data = create(admin, config=fake_config(host="nas.example.internal"))
    assert data["config"]["host"] == "nas.example.internal"


def test_cloud_host_pointing_at_lan_is_refused(admin: TestClient) -> None:
    r = admin.post(
        f"{API}/instances",
        json=body(type_id="fake_cloud", config=fake_config(host="api.example.com")),
    )
    assert r.status_code == 422
    assert "non-public" in r.json()["detail"][0]["msg"]


def test_default_guard_factory_uses_cloud_hosts() -> None:
    guard = ic.default_net_guard_factory(FakeCloudIntegration)
    assert guard.cloud_hosts == frozenset({"api.example.com"})
    assert ic.default_net_guard_factory(FakeIntegration).cloud_hosts == frozenset()


# --- update ----------------------------------------------------------------------------


def test_patch_omitted_secret_is_unchanged(admin: TestClient, session: Session) -> None:
    data = create(admin, config=fake_config(snmp_community=COMMUNITY))
    r = admin.patch(f"{API}/instances/{data['id']}", json={"config": {"port": 9443}})
    assert r.status_code == 200, r.text
    assert_no_secrets(r.text)
    assert r.json()["config"]["port"] == 9443
    assert r.json()["secrets_set"] == data["secrets_set"]
    secrets = stored_secrets(stored(session, data["id"]))
    assert secrets["api_token"] == TOKEN
    assert secrets["snmp_community"] == COMMUNITY


def test_patch_replaces_and_clears_secrets(admin: TestClient, session: Session) -> None:
    data = create(admin, config=fake_config(snmp_community=COMMUNITY))
    r = admin.patch(
        f"{API}/instances/{data['id']}",
        json={"config": {"api_token": NEW_TOKEN, "snmp_community": None}},
    )
    assert r.status_code == 200, r.text
    assert_no_secrets(r.text)
    assert r.json()["secrets_set"]["snmp_community"] is False
    row = stored(session, data["id"])
    assert stored_secrets(row) == {
        "api_token": NEW_TOKEN,
        "client_secret": "example-default-secret-5a4b",
    }
    assert row.config["port"] == 8443  # untouched


def test_patch_cannot_clear_required_secret(admin: TestClient, session: Session) -> None:
    data = create(admin)
    r = admin.patch(f"{API}/instances/{data['id']}", json={"config": {"api_token": None}})
    assert r.status_code == 422
    assert stored_secrets(stored(session, data["id"]))["api_token"] == TOKEN


def test_patch_metadata(admin: TestClient, session: Session) -> None:
    data = create(admin)
    r = admin.patch(
        f"{API}/instances/{data['id']}",
        json={"name": "nas-renamed", "site": None, "tags": [], "enabled": False},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["name"], out["site"], out["tags"], out["enabled"]) == (
        "nas-renamed",
        None,
        [],
        False,
    )
    assert out["config"] == data["config"]
    assert stored_secrets(stored(session, data["id"]))["api_token"] == TOKEN


@pytest.mark.parametrize(
    "patch",
    [
        {"name": None},
        {"enabled": None},
        {"tags": None},
        {"type_id": "fake_cloud"},  # the type can't change
        {"config": {"port": "nope"}},
    ],
)
def test_patch_rejects_bad_fields(admin: TestClient, patch: dict[str, Any]) -> None:
    data = create(admin)
    r = admin.patch(f"{API}/instances/{data['id']}", json=patch)
    assert r.status_code == 422
    assert admin.get(f"{API}/instances/{data['id']}").json() == data


def test_patch_refuses_denied_host(admin: TestClient) -> None:
    data = create(admin)
    r = admin.patch(f"{API}/instances/{data['id']}", json={"config": {"host": "127.0.0.1"}})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "config", "host"]
    assert admin.get(f"{API}/instances/{data['id']}").json() == data


def test_patch_name_clash(admin: TestClient) -> None:
    create(admin, name="taken")
    data = create(admin)
    r = admin.patch(f"{API}/instances/{data['id']}", json={"name": "taken"})
    assert r.status_code == 409
    # Renaming to its own name is fine.
    r = admin.patch(f"{API}/instances/{data['id']}", json={"name": data["name"]})
    assert r.status_code == 200


def test_patch_unknown(admin: TestClient) -> None:
    r = admin.patch(f"{API}/instances/{uuid.uuid4()}", json={"enabled": False})
    assert r.status_code == 404


# --- delete ----------------------------------------------------------------------------


def test_delete(admin: TestClient, session: Session) -> None:
    data = create(admin)
    r = admin.delete(f"{API}/instances/{data['id']}")
    assert r.status_code == 204
    assert r.content == b""
    assert admin.get(f"{API}/instances/{data['id']}").status_code == 404
    assert admin.delete(f"{API}/instances/{data['id']}").status_code == 404
    session.expire_all()
    assert session.scalars(select(Instance)).all() == []


# --- status ----------------------------------------------------------------------------


def metrics_store(client: TestClient) -> MetricsStore:
    store: MetricsStore = client.app.state.metrics_store  # type: ignore[attr-defined]
    return store


def test_status_comes_from_the_collector(admin: TestClient) -> None:
    data = create(admin)
    # Polled, but the collector hasn't picked the new instance up yet.
    assert data["status"] == {
        "state": "pending",
        "checked_at": None,
        "last_success_at": None,
        "error": None,
    }
    instance_id = uuid.UUID(data["id"])
    store = metrics_store(admin)
    store.track(instance_id, {})
    ok_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    store.record_success(instance_id, "fake_device", [], at=ok_at, duration_seconds=0.1)
    r = admin.get(f"{API}/instances/{data['id']}")
    assert r.json()["status"]["state"] == "up"
    assert r.json()["status"]["last_success_at"] == "2026-01-02T03:04:05Z"

    failed_at = datetime(2026, 1, 2, 3, 5, 5, tzinfo=UTC)
    store.record_failure(instance_id, "ConnectTimeout", at=failed_at, duration_seconds=5)
    for status in (
        admin.get(f"{API}/instances").json()[0]["status"],
        admin.get(f"{API}/instances/{data['id']}").json()["status"],
    ):
        assert status == {
            "state": "down",
            "checked_at": "2026-01-02T03:05:05Z",
            "last_success_at": "2026-01-02T03:04:05Z",
            "error": "ConnectTimeout",
        }
    r = admin.patch(f"{API}/instances/{data['id']}", json={"tags": []})
    assert r.json()["status"]["state"] == "down"


def test_status_unknown_when_not_polled(admin: TestClient) -> None:
    disabled = create(admin, enabled=False)
    scrape = create(admin, name="scrape-1", type_id="scrape_only")
    assert disabled["status"]["state"] == "unknown"
    assert scrape["status"]["state"] == "unknown"
    # Still tracked by the collector until its next refresh: disabled wins.
    instance_id = uuid.UUID(disabled["id"])
    metrics_store(admin).track(instance_id, {})
    metrics_store(admin).record_failure(
        instance_id, "ConnectTimeout", at=datetime.now(UTC), duration_seconds=1
    )
    states = {i["name"]: i["status"] for i in admin.get(f"{API}/instances").json()}
    assert states["nas-1"] == {
        "state": "unknown",
        "checked_at": None,
        "last_success_at": None,
        "error": None,
    }
    assert states["scrape-1"]["state"] == "unknown"


# --- audit -----------------------------------------------------------------------------


def test_audit_entries_never_hold_secrets(admin: TestClient, session: Session) -> None:
    data = create(admin, config=fake_config(snmp_community=COMMUNITY))
    admin.patch(
        f"{API}/instances/{data['id']}",
        json={"name": "nas-x", "config": {"api_token": NEW_TOKEN, "port": 9443}},
    )
    admin.delete(f"{API}/instances/{data['id']}")

    created, updated, deleted = audit_rows(session)
    assert [created.action, updated.action, deleted.action] == ["create", "update", "delete"]
    assert {created.target_id, updated.target_id, deleted.target_id} == {data["id"]}
    assert created.changes["config"]["api_token"] == "<changed>"
    assert created.changes["config"]["snmp_community"] == "<changed>"
    assert created.changes["config"]["host"] == "10.0.0.10"
    assert updated.changes["name"] == {"old": "nas-1", "new": "nas-x"}
    assert updated.changes["config"]["port"] == {"old": 8443, "new": 9443}
    assert updated.changes["config"]["api_token"] == "<changed>"
    assert "snmp_community" not in updated.changes["config"]  # omitted = unchanged
    assert deleted.changes == {"type_id": "fake_device", "name": "nas-x"}
    everything = " ".join(str(row.changes) for row in session.scalars(select(AuditLog)))
    assert_no_secrets(everything)

    # And the audit API serves them as stored.
    r = admin.get(f"{API}/audit")
    assert_no_secrets(r.text)


def test_failed_create_writes_no_audit(admin: TestClient, session: Session) -> None:
    admin.post(f"{API}/instances", json=body(config=fake_config(host="127.0.0.1")))
    assert audit_rows(session) == []


# --- roles and CSRF --------------------------------------------------------------------


def test_viewer_can_read_but_not_write(admin: TestClient, viewer: TestClient) -> None:
    data = create(admin)
    iid = data["id"]
    assert viewer.get(f"{API}/instances").status_code == 200
    assert viewer.get(f"{API}/instances/{iid}").status_code == 200
    assert viewer.get(f"{API}/instances/{iid}/cards").status_code == 200
    assert viewer.post(f"{API}/instances", json=body(name="v")).status_code == 403
    assert viewer.patch(f"{API}/instances/{iid}", json={"enabled": False}).status_code == 403
    assert viewer.delete(f"{API}/instances/{iid}").status_code == 403
    assert viewer.post(f"{API}/instances/{iid}/test").status_code == 403
    assert viewer.post(f"{API}/instances/test", json=body()).status_code == 403
    assert Probe.seen == []


def test_anonymous_is_refused(admin: TestClient) -> None:
    anon = TestClient(admin.app, base_url="https://testserver")
    assert anon.get(f"{API}/instances").status_code == 401
    assert anon.post(f"{API}/instances", json=body()).status_code == 401


def test_writes_need_csrf_token(admin: TestClient) -> None:
    data = create(admin)
    token = admin.headers.pop(CSRF_HEADER)
    assert admin.post(f"{API}/instances", json=body(name="x")).status_code == 403
    assert admin.patch(f"{API}/instances/{data['id']}", json={"site": "x"}).status_code == 403
    assert admin.delete(f"{API}/instances/{data['id']}").status_code == 403
    assert admin.post(f"{API}/instances/{data['id']}/test").status_code == 403
    admin.headers[CSRF_HEADER] = token
    assert admin.get(f"{API}/instances/{data['id']}").json() == data


# --- connection tests ------------------------------------------------------------------


def probe(admin: TestClient, **config: Any) -> dict[str, Any]:
    return create(admin, type_id="probe_device", name="probe", config=fake_config(**config))


def test_saved_instance_test_gets_decrypted_config(admin: TestClient) -> None:
    data = probe(admin)
    r = admin.post(f"{API}/instances/{data['id']}/test")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "reachable", "hint": None}
    [ctx] = Probe.seen
    assert ctx.instance_id == uuid.UUID(data["id"])
    assert ctx.name == "probe"
    assert ctx.config_as(FakeConfig).api_token.get_secret_value() == TOKEN
    assert ctx.config_as(FakeConfig).host == "10.0.0.10"
    assert TOKEN not in repr(ctx)


def test_test_after_patch_uses_kept_secret(admin: TestClient) -> None:
    data = probe(admin)
    admin.patch(f"{API}/instances/{data['id']}", json={"config": {"port": 9000}})
    admin.post(f"{API}/instances/{data['id']}/test")
    [ctx] = Probe.seen
    config = ctx.config_as(FakeConfig)
    assert (config.port, config.api_token.get_secret_value()) == (9000, TOKEN)


def test_test_scrubs_echoed_secret(admin: TestClient) -> None:
    data = probe(admin)
    Probe.mode = "echo"
    r = admin.post(f"{API}/instances/{data['id']}/test")
    assert r.status_code == 200
    assert r.json()["message"] == "logged in with <redacted>"
    assert_no_secrets(r.text)


def test_test_exception_is_reported_without_its_message(
    admin: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    data = probe(admin)
    Probe.mode = "raise"
    with caplog.at_level(logging.DEBUG):
        r = admin.post(f"{API}/instances/{data['id']}/test")
    assert r.status_code == 200
    assert r.json() == {
        "ok": False,
        "message": "connection test failed (RuntimeError)",
        "hint": None,
    }
    assert_no_secrets(r.text)
    assert_no_secrets(caplog.text)
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("mode", ["hang", "http_timeout"])
def test_test_timeout_gives_firewall_hint(
    admin: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(instances_api, "TEST_TIMEOUT_SECONDS", 0.05)
    data = probe(admin)
    Probe.mode = mode
    r = admin.post(f"{API}/instances/{data['id']}/test")
    assert r.status_code == 200
    assert r.json() == {
        "ok": False,
        "message": "connection test timed out",
        "hint": {"protocol": "tcp", "port": 8443, "destination": "10.0.0.10", "reason": "API"},
    }
    assert_no_secrets(r.text)


def test_test_reports_guard_refusal(admin: TestClient) -> None:
    data = probe(admin)
    Probe.mode = "guard"
    r = admin.post(f"{API}/instances/{data['id']}/test")
    assert r.status_code == 200
    result = r.json()
    assert result["ok"] is False
    assert "network guard" in result["message"] and "127.0.0.1" in result["message"]


def test_test_unknown_instance(admin: TestClient) -> None:
    assert admin.post(f"{API}/instances/{uuid.uuid4()}/test").status_code == 404


def test_unsaved_test(admin: TestClient, session: Session) -> None:
    r = admin.post(
        f"{API}/instances/test",
        json={"type_id": "probe_device", "config": fake_config(host="nas.example.internal")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    [ctx] = Probe.seen
    assert ctx.config_as(FakeConfig).api_token.get_secret_value() == TOKEN
    # Nothing was saved or audited.
    assert session.scalars(select(Instance)).all() == []
    assert audit_rows(session) == []


def test_unsaved_test_validates(admin: TestClient) -> None:
    config = fake_config()
    del config["api_token"]
    r = admin.post(f"{API}/instances/test", json={"type_id": "probe_device", "config": config})
    assert r.status_code == 422
    r = admin.post(
        f"{API}/instances/test",
        json={"type_id": "probe_device", "config": fake_config(host="169.254.169.254")},
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "net_guard"
    r = admin.post(f"{API}/instances/test", json={"type_id": "nope", "config": {}})
    assert r.status_code == 422
    assert Probe.seen == []


# --- rows the API can't fully interpret ------------------------------------------------


def test_instance_of_unregistered_type(admin: TestClient, session: Session) -> None:
    row = Instance(type_id="removed_type", name="orphan", config={"host": "10.0.0.5"})
    session.add(row)
    session.commit()
    r = admin.get(f"{API}/instances/{row.id}")
    assert r.status_code == 200
    assert r.json()["secrets_set"] == {}
    assert admin.post(f"{API}/instances/{row.id}/test").status_code == 409
    assert admin.get(f"{API}/instances/{row.id}/cards").status_code == 409
    assert admin.patch(f"{API}/instances/{row.id}", json={"enabled": False}).status_code == 409
    assert admin.delete(f"{API}/instances/{row.id}").status_code == 204


def test_undecryptable_secrets(admin: TestClient, session: Session) -> None:
    data = create(admin)
    row = stored(session, data["id"])
    row.secrets = b"\x01" + b"\x00" * 40
    session.commit()
    r = admin.get(f"{API}/instances")
    assert r.status_code == 200
    assert r.json()[0]["secrets_set"]["api_token"] is False
    assert admin.post(f"{API}/instances/{data['id']}/test").status_code == 409
    r = admin.patch(f"{API}/instances/{data['id']}", json={"config": {"port": 1}})
    assert r.status_code == 409


def test_stored_config_that_no_longer_validates(admin: TestClient, session: Session) -> None:
    data = probe(admin)
    row = stored(session, data["id"])
    row.config = {**row.config, "port": "garbage"}
    session.commit()
    r = admin.post(f"{API}/instances/{data['id']}/test")
    assert r.status_code == 409
    assert "garbage" not in r.text


# --- the whole flow, once more, for leaks ----------------------------------------------


def test_no_response_or_log_line_contains_a_secret(
    admin: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG):
        data = probe(admin, snmp_community=COMMUNITY)
        iid = data["id"]
        for r in (
            admin.get(f"{API}/instances"),
            admin.get(f"{API}/instances/{iid}"),
            admin.get(f"{API}/instances/{iid}/cards"),
            admin.patch(f"{API}/instances/{iid}", json={"config": {"api_token": NEW_TOKEN}}),
            admin.post(f"{API}/instances/{iid}/test"),
            admin.post(f"{API}/instances/test", json=body(type_id="probe_device")),
            admin.post(f"{API}/instances", json=body(config=fake_config(port="x"))),
            admin.post(f"{API}/instances", json=body(config=fake_config(host="127.0.0.1"))),
            admin.get(f"{API}/audit"),
            admin.delete(f"{API}/instances/{iid}"),
        ):
            texts.append(r.text)
    assert_no_secrets("\n".join(texts))
    assert_no_secrets(caplog.text)


# --- instance_config helpers -----------------------------------------------------------


def test_split_and_rebuild_round_trip() -> None:
    model = FakeConfig.model_validate(fake_config(snmp_community=COMMUNITY))
    config, secrets = ic.split(model)
    assert config == {"host": "10.0.0.10", "port": 8443, "verify_tls": True}
    assert secrets == {
        "api_token": TOKEN,
        "snmp_community": COMMUNITY,
        "client_secret": "example-default-secret-5a4b",
    }
    assert FakeConfig.model_validate({**config, **secrets}) == model


def test_secret_field_keys_refuses_nested_secrets() -> None:
    class Inner(BaseModel):
        api_key: SecretStr

    class Outer(BaseModel):
        host: str
        inner: Inner

    with pytest.raises(ValueError, match="nested secret"):
        ic.secret_field_keys(Outer)
    assert ic.secret_field_keys(FakeConfig) == {"api_token", "snmp_community", "client_secret"}


def test_public_config_hides_secret_keys() -> None:
    leaked = {"host": "10.0.0.10", "api_token": TOKEN}
    assert ic.public_config(leaked, FakeConfig) == {"host": "10.0.0.10"}


def test_configured_hosts() -> None:
    hosts = ic.configured_hosts(
        {
            "host": "10.0.0.1",
            "backup_host": "nas.example.internal",
            "port": 8006,
            "url": "https://10.0.0.2:9000/api",
            "status_url": "http://[fd00::5]/",
            "empty_host": "",
            "hostname_label": "not checked",
            "timeout": 5,
        }
    )
    assert hosts == [
        ("host", "10.0.0.1", 8006, None),
        ("backup_host", "nas.example.internal", 8006, None),
        ("url", "10.0.0.2", 9000, "https"),
        ("status_url", "fd00::5", 80, "http"),
    ]


@pytest.mark.parametrize("url", ["ftp://10.0.0.1/", "not a url", "https://"])
def test_configured_hosts_rejects_bad_urls(url: str) -> None:
    with pytest.raises(ic.HostCheckError):
        ic.configured_hosts({"url": url})


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://xn--e1afmkfd.example/", "xn--e1afmkfd.example"),
        ("http://пример.example/", "xn--e1afmkfd.example"),
    ],
)
def test_configured_hosts_uses_the_ascii_idn_form(url: str, host: str) -> None:
    # The same form the guarded transport resolves and connects to.
    assert ic.configured_hosts({"url": url}) == [("url", host, 80, "http")]


def test_idn_url_is_accepted_on_save(admin: TestClient) -> None:
    # Used to be refused: the check got the Unicode form, which parse_host rejects.
    config = {"api_url": "http://xn--e1afmkfd.example/", "api_token": TOKEN}
    r = admin.post(f"{API}/instances", json=body(type_id="url_device", config=config))
    assert r.status_code == 201, r.text
    assert r.json()["config"]["api_url"] == "http://xn--e1afmkfd.example/"


@pytest.mark.parametrize(
    ("url", "status"),
    [
        # A Unicode host is checked as its punycode form, exactly like the connection.
        ("http://пример.example/", 201),  # -> 10.0.0.25
        ("http://тест.example/", 422),  # -> 169.254.169.254
        ("http://нет.example/", 422),  # doesn't resolve
    ],
)
def test_unicode_url_host_is_checked_by_the_guard(admin: TestClient, url: str, status: int) -> None:
    config = {"api_url": url, "api_token": TOKEN}
    r = admin.post(f"{API}/instances", json=body(type_id="url_device", config=config))
    assert r.status_code == status, r.text
    assert_no_secrets(r.text)


async def test_check_hosts_refuses_cloud_host_over_http() -> None:
    guard = NetGuard(
        NetPolicy(own_addresses=frozenset()),
        cloud_hosts=("api.example.com",),
        resolver=fake_resolver,
    )
    with pytest.raises(ic.HostCheckError, match="https"):
        await ic.check_hosts(guard, {"url": "http://api.example.com/v1"})


async def test_check_hosts_passes_allowed_and_wraps_guard_errors() -> None:
    guard = fake_guards(FakeIntegration)
    await ic.check_hosts(guard, {"host": "10.0.0.1", "url": "https://nas.example.internal/"})
    with pytest.raises(ic.HostCheckError) as info:
        await ic.check_hosts(guard, {"host": "10.0.0.1", "api_url": "https://127.0.0.1/"})
    assert info.value.field == "api_url"
    assert not isinstance(info.value, NetGuardError)


# --- missing/empty secrets, credentials in URLs, host rechecks -------------------------


def test_create_missing_required_secret(admin: TestClient) -> None:
    config = fake_config()
    del config["api_token"]
    r = admin.post(f"{API}/instances", json=body(config=config))
    assert r.status_code == 422
    assert [e["loc"] for e in r.json()["detail"]] == [["body", "config", "api_token"]]


@pytest.mark.parametrize("value", ["", "   "])
def test_create_rejects_empty_secret(admin: TestClient, session: Session, value: str) -> None:
    r = admin.post(f"{API}/instances", json=body(config=fake_config(snmp_community=value)))
    assert r.status_code == 422
    assert r.json()["detail"] == [
        {
            "loc": ["body", "config", "snmp_community"],
            "msg": "must not be empty",
            "type": "value_error",
        }
    ]
    assert_no_secrets(r.text)
    assert session.scalars(select(Instance)).all() == []


def test_patch_rejects_empty_secret(admin: TestClient, session: Session) -> None:
    data = create(admin)
    r = admin.patch(f"{API}/instances/{data['id']}", json={"config": {"api_token": ""}})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "config", "api_token"]
    assert stored_secrets(stored(session, data["id"]))["api_token"] == TOKEN


def test_patch_null_clears_optional_secret(admin: TestClient, session: Session) -> None:
    data = create(admin, config=fake_config(snmp_community=COMMUNITY))
    assert data["secrets_set"]["snmp_community"] is True
    r = admin.patch(f"{API}/instances/{data['id']}", json={"config": {"snmp_community": None}})
    assert r.status_code == 200
    assert r.json()["secrets_set"]["snmp_community"] is False
    assert "snmp_community" not in stored_secrets(stored(session, data["id"]))


def test_unsaved_test_rejects_empty_secret(admin: TestClient) -> None:
    r = admin.post(
        f"{API}/instances/test",
        json={"type_id": "probe_device", "config": fake_config(api_token="")},
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["msg"] == "must not be empty"
    assert Probe.seen == []


def test_saved_test_rejects_stored_empty_secret(admin: TestClient, session: Session) -> None:
    data = probe(admin)
    row = stored(session, data["id"])
    row.secrets = ic.encrypt(SecretBox(TEST_MASTER_KEY), row.id, {"api_token": ""})
    session.commit()
    r = admin.post(f"{API}/instances/{data['id']}/test")
    assert r.status_code == 422
    assert Probe.seen == []


@pytest.mark.parametrize(
    "url",
    [
        "https://admin:example-password-u1@10.0.0.40/api",
        "https://example-password-u1@10.0.0.40/api",
        "http://admin:example-password-u1@nas.example.internal:8080/",
    ],
)
def test_url_with_credentials_is_refused(admin: TestClient, session: Session, url: str) -> None:
    config = {"api_url": url, "api_token": TOKEN}
    for path, payload in (
        ("/instances", body(type_id="url_device", config=config)),
        ("/instances/test", {"type_id": "url_device", "config": config}),
    ):
        r = admin.post(f"{API}{path}", json=payload)
        assert r.status_code == 422, r.text
        [error] = r.json()["detail"]
        assert error["loc"] == ["body", "config", "api_url"]
        assert "username or password" in error["msg"]
        assert "example-password-u1" not in r.text
        assert url not in r.text
    assert session.scalars(select(Instance)).all() == []


def test_patch_url_with_credentials_is_refused(admin: TestClient) -> None:
    data = create(
        admin, type_id="url_device", config={"api_url": "https://10.0.0.40/", "api_token": TOKEN}
    )
    assert data["config"] == {"api_url": "https://10.0.0.40/"}
    r = admin.patch(
        f"{API}/instances/{data['id']}",
        json={"config": {"api_url": "https://admin:example-password-u1@10.0.0.40/"}},
    )
    assert r.status_code == 422
    assert "example-password-u1" not in r.text
    assert admin.get(f"{API}/instances/{data['id']}").json() == data


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "169.254.169.254", "rebind.example.internal", "unknown.example.internal"]
)
def test_patch_host_only_change_is_rechecked(admin: TestClient, host: str) -> None:
    data = create(admin)
    r = admin.patch(f"{API}/instances/{data['id']}", json={"config": {"host": host}})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "net_guard"
    assert admin.get(f"{API}/instances/{data['id']}").json() == data
    # An allowed host-only change goes through.
    r = admin.patch(
        f"{API}/instances/{data['id']}", json={"config": {"host": "nas.example.internal"}}
    )
    assert r.status_code == 200
    assert r.json()["config"]["host"] == "nas.example.internal"

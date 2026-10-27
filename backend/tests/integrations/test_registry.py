"""Registry and ``GET /api/v1/integrations``."""

import json
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.integrations.base import Category
from app.integrations.registry import BUILTIN_INTEGRATIONS, Registry, RegistryError
from app.integrations.schema import config_json_schema
from app.models import Role
from tests.integrations.conftest import ClientFactory
from tests.integrations.fake import (
    FAKE_SECRET_VALUES,
    FakeCloudIntegration,
    FakeConfig,
    FakeIntegration,
)
from tests.test_auth import add_user, login


class OtherFake(FakeIntegration):
    type_id = "other_fake"
    display_name = "Other"
    category: ClassVar[Category] = "switch"


def test_registry_lookup_and_order() -> None:
    registry = Registry([OtherFake, FakeIntegration])
    assert len(registry) == 2
    assert registry.get("fake_device") is FakeIntegration
    assert registry.get("nope") is None
    assert [i["type_id"] for i in registry.info()] == ["fake_device", "other_fake"]


def test_registry_rejects_duplicates() -> None:
    class Copy(FakeIntegration):
        pass

    with pytest.raises(RegistryError, match="duplicate"):
        Registry([FakeIntegration, Copy])


@pytest.mark.parametrize("type_id", ["", "Fake", "fake-device", "1fake", "fake device", "a" * 33])
def test_registry_rejects_bad_type_id(type_id: str) -> None:
    cls = type("Bad", (FakeIntegration,), {"type_id": type_id})
    with pytest.raises(RegistryError, match="type_id"):
        Registry([cls])


def test_registry_rejects_non_integrations() -> None:
    with pytest.raises(RegistryError):
        Registry([FakeConfig])  # type: ignore[list-item]


def test_info_is_a_copy() -> None:
    registry = Registry([FakeIntegration])
    registry.info()[0]["schema"]["properties"].clear()
    assert registry.info()[0]["schema"] == config_json_schema(FakeConfig)


def test_builtin_registry_is_valid() -> None:
    assert len(Registry(BUILTIN_INTEGRATIONS)) == len(BUILTIN_INTEGRATIONS)


# --- API -------------------------------------------------------------------------------


def test_list_requires_login(client_for: ClientFactory) -> None:
    client = client_for(Registry([FakeIntegration]))
    assert client.post("/api/v1/auth/logout", headers=_csrf(client)).status_code < 300
    r = client.get("/api/v1/integrations")
    assert r.status_code == 401


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/v1/me").json()["csrf_token"]
    return {"X-CSRF-Token": token}


def test_viewers_can_list(client_for: ClientFactory, session: Session) -> None:
    client = client_for(Registry([FakeIntegration, FakeCloudIntegration]))
    client.cookies.clear()
    add_user(session, "viewer", Role.VIEWER)
    login(client, "viewer")
    r = client.get("/api/v1/integrations")
    assert r.status_code == 200
    body = r.json()
    assert [(i["type_id"], i["name"], i["category"]) for i in body] == [
        ("fake_cloud", "Fake cloud", "cloud"),
        ("fake_device", "Fake device", "generic"),
    ]
    assert set(body[0]) == {"type_id", "name", "category", "schema"}
    props = body[1]["schema"]["properties"]
    assert {n for n, p in props.items() if p.get("writeOnly")} == {
        "api_token",
        "snmp_community",
        "client_secret",
    }
    for value in FAKE_SECRET_VALUES:
        assert value not in r.text
    assert r.headers["cache-control"] == "no-store"


def test_production_app_serves_builtin_list(client_for: ClientFactory) -> None:
    client = client_for(None)  # create_app's default registry
    body = client.get("/api/v1/integrations").json()
    assert [i["type_id"] for i in body] == [cls.type_id for cls in BUILTIN_INTEGRATIONS]
    assert "fake_device" not in json.dumps(body)

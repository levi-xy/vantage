"""Audit log service and its wiring into the API (M1.4)."""

import enum
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, SecretBytes, SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.audit import REDACTED, Action, diff, record, redact, secret_fields
from app.core.auth import session_id
from app.core.db import make_session_factory
from app.models import ActorType, AuditLog, Role, User
from tests.audit_helpers import raw_audit_table
from tests.test_auth import (
    ADMIN,
    PASSWORD,
    SECURE_COOKIE,
    add_user,
    do_setup,
    login,
    make_client,
)

# Obviously fake values used to prove nothing secret reaches the audit table.
FAKE_API_TOKEN = "example-token-0f1e2d3c"
FAKE_COMMUNITY = "example-community-xyz"
FAKE_BOT_TOKEN = "example-bot-token-123456"
FAKE_RELAY_VALUE = "example-relay-value-789"


class Credentials(BaseModel):
    username: str
    api_key: SecretStr


class FakeIntegrationConfig(BaseModel):
    """Shaped like a future integration ``config_schema``."""

    host: str
    port: int = 443
    token_value: SecretStr
    snmp_secret: SecretStr | None = None
    login: Credentials | None = None
    aliased: SecretStr | None = Field(default=None, alias="privKey")
    raw_key: SecretBytes | None = None


class FakeChannelConfig(BaseModel):
    """Shaped like a future channel schema; the secret has a name no heuristic catches."""

    chat_id: str
    relay_value: SecretStr


@pytest.fixture
def client(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        yield c


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


def entries(db: Session) -> list[AuditLog]:
    db.expire_all()
    return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


# --- secret detection ----------------------------------------------------------------


def test_secret_fields_come_from_the_schema() -> None:
    assert secret_fields(FakeIntegrationConfig) == {
        "token_value",
        "snmp_secret",
        "api_key",  # nested model
        "aliased",
        "privKey",
        "raw_key",
    }


# --- redaction -----------------------------------------------------------------------


def test_always_secret_keys_are_redacted() -> None:
    out = redact({"username": "u", "password": "p", "password_hash": "h", "Token": "t"})
    assert out == {
        "username": "u",
        "password": REDACTED,
        "password_hash": REDACTED,
        "Token": REDACTED,
    }


def test_explicit_secret_keys() -> None:
    out = redact({"relay_value": FAKE_RELAY_VALUE, "chat": "1"}, secret_keys=["Relay-Value"])
    assert out == {"relay_value": REDACTED, "chat": "1"}


LEAKED_IN_REVIEW = [
    "client_secret",
    "private_key",
    "passphrase",
    "psk",
    "authorization",
    "snmp_community",
    "bot_token",
    "API-Key",
    "apiKey",
    "api_key",
    "Password ",
    "PASSWORD",
    "x-auth-token",
    "credentials",
    "passwd",
]


@pytest.mark.parametrize("key", LEAKED_IN_REVIEW)
def test_secret_key_variants_are_redacted_top_level_and_nested(key: str) -> None:
    value = "example-value-not-a-secret"
    changes = {key: value, "outer": {"inner": [{key: value}]}, "diff": {key: {"old": value}}}
    out = redact(changes)
    assert out == {
        key: REDACTED,
        "outer": {"inner": [{key: REDACTED}]},
        "diff": {key: REDACTED},
    }
    assert value not in repr(out)


def test_harmless_keys_are_kept() -> None:
    changes = {"host": "192.0.2.1", "port": 161, "name": "sw1", "site": "lab", "enabled": True}
    assert redact(changes) == changes


def test_bytes_keys_are_decoded_before_matching() -> None:
    assert redact({b"password": "p", b"host": "h"}) == {"password": REDACTED, "host": "h"}  # type: ignore[dict-item]


@pytest.mark.parametrize("key", [1, ("password",), None])
def test_non_string_keys_are_refused(key: object) -> None:
    with pytest.raises(TypeError):
        redact({key: "x"})  # type: ignore[dict-item]


@pytest.mark.parametrize("target_type", ["instance", "channel"])
def test_instance_and_channel_changes_need_a_schema(db: Session, target_type: str) -> None:
    with pytest.raises(ValueError, match="schema"):
        record(
            db,
            actor_type=ActorType.USER,
            action=Action.UPDATE,
            target_type=target_type,
            target_id=1,
            user_id=None,
            changes={"relay_value": FAKE_RELAY_VALUE},
        )
    # Nothing to leak: a delete without changes, or a model instance (its own schema).
    record(
        db,
        actor_type=ActorType.USER,
        action=Action.DELETE,
        target_type=target_type,
        target_id=1,
        user_id=None,
    )
    record(
        db,
        actor_type=ActorType.USER,
        action=Action.CREATE,
        target_type=target_type,
        target_id=2,
        user_id=None,
        changes=FakeChannelConfig(chat_id="1", relay_value=SecretStr(FAKE_RELAY_VALUE)),
    )
    db.commit()
    rows = entries(db)
    assert len(rows) == 2 and rows[1].changes == {"chat_id": "1", "relay_value": REDACTED}


def test_secretstr_values_are_redacted_whatever_the_key() -> None:
    out = redact({"anything": SecretStr(FAKE_API_TOKEN), "blob": b"\x00ciphertext"})
    assert out == {"anything": REDACTED, "blob": REDACTED}


def test_nested_dicts_and_lists() -> None:
    changes = {
        "config": {
            "host": "192.0.2.10",
            "token_value": {"old": "a", "new": "b"},
            "items": [{"community": FAKE_COMMUNITY, "name": "x"}],
        }
    }
    out = redact(changes, schema=FakeIntegrationConfig)
    assert out == {
        "config": {
            "host": "192.0.2.10",
            "token_value": REDACTED,
            "items": [{"community": REDACTED, "name": "x"}],
        }
    }


def test_schema_driven_redaction_of_a_model_instance() -> None:
    cfg = FakeIntegrationConfig(
        host="10.0.0.5",
        token_value=SecretStr(FAKE_API_TOKEN),
        login=Credentials(username="ro", api_key=SecretStr(FAKE_BOT_TOKEN)),
        privKey=SecretStr(FAKE_COMMUNITY),
    )
    out = redact(cfg)
    assert out == {
        "host": "10.0.0.5",
        "port": 443,
        "token_value": REDACTED,
        "snmp_secret": REDACTED,  # present (None) but secret: still "<changed>"
        "login": {"username": "ro", "api_key": REDACTED},
        "aliased": REDACTED,
        "raw_key": REDACTED,
    }


def test_schema_applies_to_plain_dicts_too() -> None:
    sent = {"host": "10.0.0.5", "token_value": FAKE_API_TOKEN}
    assert redact(sent, schema=FakeIntegrationConfig) == {
        "host": "10.0.0.5",
        "token_value": REDACTED,
    }


def test_none_secret_is_redacted_and_omitted_secret_is_absent() -> None:
    assert redact({"password": None}) == {"password": REDACTED}
    assert redact({"password": ""}) == {"password": REDACTED}
    # PATCH with the secret omitted (= unchanged): nothing about it is recorded.
    patch = FakeIntegrationConfig(host="10.0.0.6", token_value=SecretStr("x")).model_dump(
        exclude_unset=True, exclude={"token_value"}
    )
    assert redact(patch, schema=FakeIntegrationConfig) == {"host": "10.0.0.6"}


def test_diff_then_redact_hides_old_and_new_secret() -> None:
    before = {"host": "10.0.0.1", "token_value": "old-example-token"}
    after = {"host": "10.0.0.2", "token_value": FAKE_API_TOKEN, "port": 443}
    out = redact(diff(before, after), schema=FakeIntegrationConfig)
    assert out == {
        "host": {"old": "10.0.0.1", "new": "10.0.0.2"},
        "token_value": REDACTED,
        "port": {"old": None, "new": 443},
    }


def test_values_become_json_safe() -> None:
    class Color(enum.Enum):
        RED = "red"

    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    uid = UUID("00000000-0000-4000-8000-000000000001")
    out = redact({"at": when, "id": uid, "role": Role.ADMIN, "c": Color.RED, "t": (1, 2)})
    assert out == {
        "at": "2026-01-02T03:04:05+00:00",
        "id": str(uid),
        "role": "admin",
        "c": "red",
        "t": [1, 2],
    }


def test_unknown_objects_are_refused() -> None:
    class Opaque:
        def __repr__(self) -> str:
            return FAKE_API_TOKEN

    with pytest.raises(TypeError):
        redact({"x": Opaque()})


# --- record: create / update / delete ------------------------------------------------


def test_record_create_update_delete(engine: Engine, db: Session) -> None:
    admin = add_user(db, "admin2", Role.ADMIN)
    target = "00000000-0000-4000-8000-0000000000aa"

    record(
        db,
        actor_type=ActorType.USER,
        action=Action.CREATE,
        target_type="instance",
        target_id=UUID(target),
        user_id=admin.id,
        changes=FakeIntegrationConfig(host="10.0.0.7", token_value=SecretStr(FAKE_API_TOKEN)),
    )
    record(
        db,
        actor_type=ActorType.USER,
        action=Action.UPDATE,
        target_type="instance",
        target_id=target,
        user_id=admin.id,
        changes=diff({"token_value": FAKE_API_TOKEN}, {"token_value": "example-token-new"}),
        schema=FakeIntegrationConfig,
    )
    record(
        db,
        actor_type=ActorType.USER,
        action=Action.DELETE,
        target_type="instance",
        target_id=target,
        user_id=admin.id,
    )
    db.commit()

    rows = entries(db)
    assert [(r.action, r.target_type, r.target_id, r.user_id) for r in rows] == [
        ("create", "instance", target, admin.id),
        ("update", "instance", target, admin.id),
        ("delete", "instance", target, admin.id),
    ]
    assert rows[0].changes["token_value"] == REDACTED
    assert rows[0].changes["host"] == "10.0.0.7"
    assert rows[1].changes == {"token_value": REDACTED}
    assert rows[2].changes == {}
    assert all(r.at.tzinfo is not None for r in rows)
    raw = raw_audit_table(engine)
    assert FAKE_API_TOKEN not in raw and "example-token-new" not in raw


def test_record_is_part_of_the_callers_transaction(db: Session) -> None:
    record(
        db,
        actor_type=ActorType.USER,
        action=Action.DELETE,
        target_type="link",
        target_id=1,
        user_id=None,
    )
    db.rollback()
    assert entries(db) == []


def test_entry_survives_user_deletion(db: Session) -> None:
    user = add_user(db, "gone", Role.VIEWER)
    record(
        db,
        actor_type=ActorType.USER,
        action=Action.UPDATE,
        target_type="user",
        target_id=user.id,
        user_id=user.id,
    )
    db.commit()
    db.delete(user)
    db.commit()
    (row,) = entries(db)
    assert row.user_id is None and row.target_id is not None


@pytest.mark.parametrize("actor", [ActorType.USER, ActorType.CLI, ActorType.SYSTEM])
def test_actor_type_is_stored(db: Session, actor: ActorType) -> None:
    record(
        db, actor_type=actor, action=Action.DELETE, target_type="link", target_id=1, user_id=None
    )
    db.commit()
    (row,) = entries(db)
    assert row.actor_type == actor


@pytest.mark.parametrize("actor", [ActorType.CLI, ActorType.SYSTEM])
def test_only_user_entries_have_an_acting_user(db: Session, actor: ActorType) -> None:
    user = add_user(db, "someone", Role.ADMIN)
    with pytest.raises(ValueError, match="no acting user"):
        record(
            db,
            actor_type=actor,
            action=Action.DELETE,
            target_type="link",
            target_id=1,
            user_id=user.id,
        )


def test_actor_type_must_be_known(db: Session) -> None:
    with pytest.raises(ValueError):
        record(
            db,
            actor_type="robot",  # type: ignore[arg-type]
            action=Action.DELETE,
            target_type="link",
            target_id=1,
            user_id=None,
        )


# --- wiring into the API -------------------------------------------------------------


def test_api_entries_are_user_actions(client: TestClient, db: Session) -> None:
    me = do_setup(client)
    client.post("/api/v1/auth/login", json={"username": ADMIN, "password": "example-wrong-pw"})
    rows = entries(db)
    assert [(r.action, r.actor_type) for r in rows] == [
        ("create", ActorType.USER),
        ("login_failed", ActorType.USER),
    ]
    body = client.get("/api/v1/audit").json()
    assert [(e["action"], e["actor_type"]) for e in body] == [
        ("login_failed", "user"),
        ("create", "user"),
    ]
    assert body[1]["user_id"] == int(me["id"]) and body[0]["user_id"] is None


def test_setup_writes_a_create_entry(client: TestClient, db: Session) -> None:
    me = do_setup(client)
    (row,) = entries(db)
    assert row.action == "create" and row.target_type == "user"
    assert row.user_id == int(me["id"]) and row.target_id == str(me["id"])
    assert row.changes == {"username": ADMIN, "role": "admin", "password": REDACTED}


def test_login_and_logout_are_recorded(client: TestClient, db: Session) -> None:
    user = add_user(db, "viewer1", Role.VIEWER)
    me = login(client, "viewer1")
    r = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]})
    assert r.status_code == 204
    rows = entries(db)
    assert [(r.action, r.user_id, r.target_id) for r in rows] == [
        ("login", user.id, str(user.id)),
        ("logout", user.id, str(user.id)),
    ]
    assert rows[0].changes == {}


def raw_contains(engine: Engine, needle: str) -> bool:
    return needle in raw_audit_table(engine)


def test_failed_login_for_a_real_user_is_recorded(client: TestClient, db: Session) -> None:
    user = add_user(db, "viewer1", Role.VIEWER)
    wrong = "example-wrong-password-1"
    r = client.post("/api/v1/auth/login", json={"username": "viewer1", "password": wrong})
    assert r.status_code == 401
    (row,) = entries(db)
    assert (row.action, row.target_type, row.target_id, row.user_id) == (
        "login_failed",
        "user",
        str(user.id),
        None,
    )
    assert row.changes == {"username": "viewer1"}


def test_failed_login_for_an_unknown_user_stores_no_typed_text(
    client: TestClient, db: Session, engine: Engine
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    typed_username = "example-typo-user-q7"
    typed_password = "example-typed-password-q8"
    r = client.post(
        "/api/v1/auth/login", json={"username": typed_username, "password": typed_password}
    )
    assert r.status_code == 401
    (row,) = entries(db)
    assert row.action == "login_failed" and row.target_id is None and row.user_id is None
    assert row.changes == {"username": "<unknown>"}
    assert not raw_contains(engine, typed_username)
    assert not raw_contains(engine, typed_password)


def test_password_typed_as_username_is_not_stored(
    client: TestClient, db: Session, engine: Engine
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    # The user typed their password into the username box.
    r = client.post("/api/v1/auth/login", json={"username": PASSWORD, "password": "viewer1"})
    assert r.status_code == 401
    assert [r.changes for r in entries(db)] == [{"username": "<unknown>"}]
    assert not raw_contains(engine, PASSWORD)


def test_locked_out_attempts_write_one_entry_a_minute(client: TestClient, db: Session) -> None:
    """Refusals of a locked account are audited (M1.9), but only once a minute, so a
    spray can't flood the table. The full M1.9 tests are in test_login_failures.py."""
    add_user(db, "viewer1", Role.VIEWER)
    body = {"username": "viewer1", "password": "example-wrong-password-2"}
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json=body).status_code == 401
    assert len(entries(db)) == 5
    for _ in range(3):
        assert client.post("/api/v1/auth/login", json=body).status_code == 429
    assert len(entries(db)) == 6
    assert entries(db)[-1].changes == {"username": "viewer1", "locked": True}


def test_unknown_username_refusals_write_nothing(client: TestClient, db: Session) -> None:
    body = {"username": "nobody", "password": "example-wrong-password-2"}
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json=body).status_code == 401
    assert len(entries(db)) == 5
    for _ in range(3):
        assert client.post("/api/v1/auth/login", json=body).status_code == 429
    assert len(entries(db)) == 5


def test_rehash_on_login_is_recorded_as_changed(client: TestClient, db: Session) -> None:
    old = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, salt_len=8)
    user = User(username="olduser", password_hash=old.hash(PASSWORD), role=Role.VIEWER)
    db.add(user)
    db.commit()
    login(client, "olduser")
    (row,) = entries(db)
    assert row.action == "login" and row.changes == {"password_hash": REDACTED}


def test_no_secret_ever_reaches_the_audit_table(
    client: TestClient, db: Session, engine: Engine
) -> None:
    """Push known secrets through every audited flow, then scan the raw table."""
    setup_password = "example-setup-password-42"
    viewer_password = "example-viewer-password-43"
    old = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, salt_len=8)

    me = do_setup(client, password=setup_password)
    admin_cookie = client.cookies[SECURE_COOKIE]
    viewer = User(username="viewer1", password_hash=old.hash(viewer_password), role=Role.VIEWER)
    db.add(viewer)
    db.commit()
    stale_hash = viewer.password_hash

    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]})
    vme = login(client, "viewer1", viewer_password)  # also rehashes
    viewer_cookie = client.cookies[SECURE_COOKIE]
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": vme["csrf_token"]})
    wrong_password = "example-wrong-password-3"
    client.post("/api/v1/auth/login", json={"username": ADMIN, "password": wrong_password})
    client.post("/api/v1/auth/login", json={"username": setup_password, "password": "x"})
    login(client, ADMIN, setup_password)

    # Service-level flows later milestones will use (instances, channels).
    record(
        db,
        actor_type=ActorType.USER,
        action=Action.CREATE,
        target_type="instance",
        target_id="00000000-0000-4000-8000-0000000000bb",
        user_id=int(me["id"]),
        changes=FakeIntegrationConfig(
            host="192.0.2.20",
            token_value=SecretStr(FAKE_API_TOKEN),
            snmp_secret=SecretStr(FAKE_COMMUNITY),
        ),
    )
    record(
        db,
        actor_type=ActorType.USER,
        action=Action.UPDATE,
        target_type="channel",
        target_id=1,
        user_id=int(me["id"]),
        changes={
            "config": {"chat_id": "-100"},
            "bot_token": {"old": None, "new": FAKE_BOT_TOKEN},
            "relay_value": FAKE_RELAY_VALUE,
        },
        schema=FakeChannelConfig,
    )
    db.commit()

    db.expire_all()
    viewer_hash = db.scalars(select(User.password_hash).where(User.username == "viewer1")).one()
    admin_hash = db.scalars(select(User.password_hash).where(User.username == ADMIN)).one()

    raw = raw_audit_table(engine)
    assert raw.count("<changed>") >= 5
    secrets: list[str] = [
        setup_password,
        viewer_password,
        wrong_password,
        stale_hash,
        viewer_hash,
        admin_hash,
        admin_cookie,
        viewer_cookie,
        session_id(admin_cookie),
        session_id(viewer_cookie),
        me["csrf_token"],
        vme["csrf_token"],
        FAKE_API_TOKEN,
        FAKE_COMMUNITY,
        FAKE_BOT_TOKEN,
        FAKE_RELAY_VALUE,
    ]
    for secret in secrets:
        assert secret not in raw

    # The API returns the same redacted rows.
    body = client.get("/api/v1/audit", params={"limit": 200}).text
    for secret in secrets:
        assert secret not in body


# --- GET /audit ----------------------------------------------------------------------


def test_audit_endpoint_is_admin_only(client: TestClient, db: Session) -> None:
    assert client.get("/api/v1/audit").status_code == 401
    add_user(db, "viewer1", Role.VIEWER)
    login(client, "viewer1")
    assert client.get("/api/v1/audit").status_code == 403


def test_audit_endpoint_lists_newest_first_with_paging(client: TestClient, db: Session) -> None:
    me = do_setup(client)
    for i in range(3):
        record(
            db,
            actor_type=ActorType.USER,
            action=Action.DELETE,
            target_type="link",
            target_id=i,
            user_id=int(me["id"]),
        )
    db.commit()

    r = client.get("/api/v1/audit", params={"limit": 2})
    assert r.status_code == 200
    page: list[dict[str, Any]] = r.json()
    assert [e["target_id"] for e in page] == ["2", "1"]
    r = client.get("/api/v1/audit", params={"limit": 2, "before_id": page[-1]["id"]})
    rest: list[dict[str, Any]] = r.json()
    assert [(e["action"], e["target_type"]) for e in rest] == [
        ("delete", "link"),
        ("create", "user"),
    ]
    assert rest[-1]["changes"]["password"] == REDACTED
    assert client.get("/api/v1/audit", params={"limit": 1000}).status_code == 422

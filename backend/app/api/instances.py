"""Instances: the devices users add (Contract 3, M2.2).

Secrets are write-only. A request's ``config`` holds every field of the integration's
``config_schema``; the ``SecretStr`` fields are encrypted into ``instances.secrets`` and
never appear in a response, a log line, an error message or an audit entry. Responses
say only which secret fields are set (``secrets_set``).

On PATCH, ``config`` is merged into the stored one: an omitted field (secret or not)
keeps its value, and an explicit ``null`` clears an optional field.
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import AdminDep, AuthDep, DbDep
from app.api.integrations import RegistryDep
from app.core import audit
from app.core.crypto import DecryptionError, SecretBox
from app.core.net_guard import NetGuardError
from app.integrations import instance_config as ic
from app.integrations.base import (
    CardSpec,
    FirewallHint,
    InstanceContext,
    Integration,
    TestResult,
)
from app.models import Instance

router = APIRouter()
log = logging.getLogger(__name__)

# Upper bound on one test_connection call, on top of the guard's per-request timeouts.
TEST_TIMEOUT_SECONDS = 30.0
_SCRUBBED = "<redacted>"

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Site = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Tags = Annotated[list[Tag], Field(max_length=32)]
TypeId = Annotated[str, StringConstraints(min_length=1, max_length=64)]


def get_secret_box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


def get_net_guard_factory(request: Request) -> ic.NetGuardFactory:
    factory: ic.NetGuardFactory = request.app.state.net_guard_factory
    return factory


SecretBoxDep = Annotated[SecretBox, Depends(get_secret_box)]
GuardFactoryDep = Annotated[ic.NetGuardFactory, Depends(get_net_guard_factory)]


# --- request and response bodies -------------------------------------------------------


class InstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type_id: TypeId
    name: Name
    site: Site | None = None
    tags: Tags = Field(default_factory=list)
    enabled: bool = True
    # Every config field, secrets included. Validated against the integration's schema.
    config: dict[str, Any] = Field(default_factory=dict)


class InstanceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    site: Site | None = None
    tags: Tags | None = None
    enabled: bool | None = None
    # Merged into the stored config: omitted fields, secrets included, stay unchanged.
    config: dict[str, Any] | None = None


class InstanceTest(BaseModel):
    """An unsaved config to test."""

    model_config = ConfigDict(extra="forbid")
    type_id: TypeId
    name: Name = "unsaved"
    config: dict[str, Any] = Field(default_factory=dict)


class InstanceOut(BaseModel):
    id: uuid.UUID
    type_id: str
    name: str
    site: str | None
    tags: list[str]
    enabled: bool
    # Non-secret fields only.
    config: dict[str, Any]
    # For each secret field of the schema: whether a value is stored. Never the value.
    secrets_set: dict[str, bool]
    created_at: datetime
    updated_at: datetime


# --- helpers ---------------------------------------------------------------------------


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "instance not found")


def _integration(registry: RegistryDep, type_id: str) -> type[Integration]:
    cls = registry.get(type_id)
    if cls is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [
                {
                    "loc": ["body", "type_id"],
                    "msg": "unknown integration type",
                    "type": "value_error",
                }
            ],
        )
    return cls


def _stored_integration(registry: RegistryDep, row: Instance) -> type[Integration]:
    cls = registry.get(row.type_id)
    if cls is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this instance's integration type is no longer available"
        )
    return cls


def _validate(schema: type[BaseModel], config: dict[str, Any]) -> BaseModel:
    """Validate against the integration's schema. Errors never echo the submitted values."""
    try:
        return schema.model_validate(config)
    except ValidationError as exc:
        errors = [
            {
                "loc": ["body", "config", *err.get("loc", ())],
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, errors) from None


async def _check_hosts(factory: ic.NetGuardFactory, cls: type[Integration], config: Any) -> None:
    try:
        await ic.check_hosts(factory(cls), config)
    except ic.HostCheckError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["body", "config", exc.field], "msg": str(exc), "type": "net_guard"}],
        ) from None


def _get_row(db: Session, instance_id: uuid.UUID) -> Instance:
    row = db.get(Instance, instance_id)
    if row is None:
        raise _not_found()
    return row


def _stored_secrets(box: SecretBox, row: Instance) -> dict[str, str]:
    try:
        return ic.decrypt(box, row.id, row.secrets)
    except DecryptionError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this instance's stored secrets can't be decrypted (was the master key changed?)",
        ) from None


def _out(row: Instance, registry: RegistryDep, box: SecretBox) -> InstanceOut:
    cls = registry.get(row.type_id)
    schema = cls.config_schema if cls is not None else None
    secrets_set: dict[str, bool] = {}
    if schema is not None:
        try:
            stored = ic.decrypt(box, row.id, row.secrets)
        except DecryptionError:
            # Don't fail a whole listing over one row; tests and edits will say why.
            log.warning("stored secrets of instance %s can't be decrypted", row.id)
            stored = {}
        secrets_set = {key: key in stored for key in sorted(ic.secret_field_keys(schema))}
    return InstanceOut(
        id=row.id,
        type_id=row.type_id,
        name=row.name,
        site=row.site,
        tags=list(row.tags),
        enabled=row.enabled,
        config=ic.public_config(row.config, schema),
        secrets_set=secrets_set,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _name_taken() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, "an instance with this name already exists")


def _flush(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _name_taken() from None


def _scrub(text: str, secrets: list[str]) -> str:
    for value in secrets:
        text = text.replace(value, _SCRUBBED)
    return text


def _scrub_result(result: TestResult, secrets: dict[str, str]) -> TestResult:
    """Defence in depth: integrations must not echo secrets, but if one does, hide them."""
    # Longest first, so a secret containing another is replaced whole.
    values = sorted((v for v in secrets.values() if v), key=len, reverse=True)
    hint = result.hint
    if hint is not None:
        hint = hint.model_copy(
            update={
                "destination": _scrub(hint.destination, values),
                "reason": _scrub(hint.reason, values),
            }
        )
    return TestResult(ok=result.ok, message=_scrub(result.message, values), hint=hint)


async def _run_test(
    cls: type[Integration],
    *,
    instance_id: uuid.UUID,
    name: str,
    model: BaseModel,
    factory: ic.NetGuardFactory,
) -> TestResult:
    """Run ``test_connection`` under a time limit. Never raises, never leaks a secret."""
    integration = cls()
    ctx = ic.build_context(instance_id=instance_id, name=name, config=model, net_guard=factory(cls))
    _, secrets = ic.split(model)
    try:
        result = await asyncio.wait_for(integration.test_connection(ctx), TEST_TIMEOUT_SECONDS)
    except (TimeoutError, httpx.TimeoutException):
        result = TestResult(
            ok=False, message="connection test timed out", hint=_hint(integration, ctx)
        )
    except NetGuardError as exc:  # its message names the host and reason, nothing secret
        result = TestResult(ok=False, message=f"connection refused by the network guard: {exc}")
    except Exception as exc:
        # Other exceptions' messages can hold URLs, headers or bodies: log the type only.
        log.warning("test_connection of %s raised %s", cls.type_id, type(exc).__name__)
        result = TestResult(ok=False, message=f"connection test failed ({type(exc).__name__})")
    return _scrub_result(result, secrets)


def _hint(integration: Integration, ctx: InstanceContext) -> FirewallHint | None:
    try:
        hints = integration.firewall_hints(ctx)
    except Exception:
        return None
    return hints[0] if hints else None


# --- routes ----------------------------------------------------------------------------


@router.get("/instances")
def list_instances(
    _auth: AuthDep, db: DbDep, registry: RegistryDep, box: SecretBoxDep
) -> list[InstanceOut]:
    rows = db.scalars(select(Instance).order_by(Instance.name))
    return [_out(row, registry, box) for row in rows]


@router.post("/instances", status_code=status.HTTP_201_CREATED)
async def create_instance(
    body: InstanceCreate,
    auth: AdminDep,
    db: DbDep,
    registry: RegistryDep,
    box: SecretBoxDep,
    factory: GuardFactoryDep,
) -> InstanceOut:
    cls = _integration(registry, body.type_id)
    model = _validate(cls.config_schema, body.config)
    config, secrets = ic.split(model)
    await _check_hosts(factory, cls, config)
    if db.scalar(select(Instance.id).where(Instance.name == body.name)) is not None:
        raise _name_taken()

    instance_id = uuid.uuid4()
    row = Instance(
        id=instance_id,
        type_id=cls.type_id,
        name=body.name,
        site=body.site,
        tags=body.tags,
        enabled=body.enabled,
        config=config,
        secrets=ic.encrypt(box, instance_id, secrets),
    )
    db.add(row)
    _flush(db)
    audit.record(
        db,
        action=audit.Action.CREATE,
        target_type="instance",
        target_id=row.id,
        user_id=auth.user.id,
        changes={
            "type_id": row.type_id,
            "name": row.name,
            "site": row.site,
            "tags": row.tags,
            "enabled": row.enabled,
            "config": {**config, **dict.fromkeys(secrets, audit.REDACTED)},
        },
        schema=cls.config_schema,
    )
    db.commit()
    return _out(row, registry, box)


@router.post("/instances/test")
async def test_unsaved_instance(
    body: InstanceTest, _auth: AdminDep, registry: RegistryDep, factory: GuardFactoryDep
) -> TestResult:
    # TODO(maintainer): an edit form testing changed fields of a saved instance, with its
    # secrets omitted, can't use this endpoint (it needs every secret). Save first, or
    # add an optional instance_id here to fill omitted secrets from the stored ones?
    cls = _integration(registry, body.type_id)
    model = _validate(cls.config_schema, body.config)
    config, _ = ic.split(model)
    await _check_hosts(factory, cls, config)
    return await _run_test(
        cls, instance_id=uuid.uuid4(), name=body.name, model=model, factory=factory
    )


@router.get("/instances/{instance_id}")
def get_instance(
    instance_id: uuid.UUID, _auth: AuthDep, db: DbDep, registry: RegistryDep, box: SecretBoxDep
) -> InstanceOut:
    return _out(_get_row(db, instance_id), registry, box)


@router.patch("/instances/{instance_id}")
async def update_instance(
    instance_id: uuid.UUID,
    body: InstanceUpdate,
    auth: AdminDep,
    db: DbDep,
    registry: RegistryDep,
    box: SecretBoxDep,
    factory: GuardFactoryDep,
) -> InstanceOut:
    row = _get_row(db, instance_id)
    cls = _stored_integration(registry, row)
    sent = body.model_dump(exclude_unset=True)
    for field in ("name", "enabled", "tags"):
        if field in sent and sent[field] is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                [{"loc": ["body", field], "msg": "may not be null", "type": "value_error"}],
            )

    before = {"name": row.name, "site": row.site, "tags": list(row.tags), "enabled": row.enabled}
    after = {**before, **{k: v for k, v in sent.items() if k != "config"}}
    changes = audit.diff(before, after)

    if body.config is not None:
        stored_secrets = _stored_secrets(box, row)
        merged = {**row.config, **stored_secrets, **body.config}
        model = _validate(cls.config_schema, merged)
        config, secrets = ic.split(model)
        await _check_hosts(factory, cls, config)
        config_changes = audit.diff(row.config, config)
        secret_keys = ic.secret_field_keys(cls.config_schema)
        for key in body.config.keys() & secret_keys:
            config_changes[key] = audit.REDACTED
        if config_changes:
            changes["config"] = config_changes
        row.config = config
        row.secrets = ic.encrypt(box, row.id, secrets)

    if "name" in sent and sent["name"] != row.name:
        clash = db.scalar(
            select(Instance.id).where(Instance.name == sent["name"], Instance.id != row.id)
        )
        if clash is not None:
            raise _name_taken()
    for field in ("name", "site", "tags", "enabled"):
        if field in sent:
            setattr(row, field, sent[field])
    _flush(db)
    audit.record(
        db,
        action=audit.Action.UPDATE,
        target_type="instance",
        target_id=row.id,
        user_id=auth.user.id,
        changes=changes,
        schema=cls.config_schema,
    )
    db.commit()
    return _out(row, registry, box)


@router.delete("/instances/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_instance(
    instance_id: uuid.UUID, auth: AdminDep, db: DbDep, registry: RegistryDep
) -> Response:
    row = _get_row(db, instance_id)
    cls = registry.get(row.type_id)
    audit.record(
        db,
        action=audit.Action.DELETE,
        target_type="instance",
        target_id=row.id,
        user_id=auth.user.id,
        changes={"type_id": row.type_id, "name": row.name},
        # Nothing from the config is recorded; any schema satisfies the fail-closed check.
        schema=cls.config_schema if cls is not None else BaseModel,
    )
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/instances/{instance_id}/test")
async def test_instance(
    instance_id: uuid.UUID,
    _auth: AdminDep,
    db: DbDep,
    registry: RegistryDep,
    box: SecretBoxDep,
    factory: GuardFactoryDep,
) -> TestResult:
    row = _get_row(db, instance_id)
    cls = _stored_integration(registry, row)
    stored = {**row.config, **_stored_secrets(box, row)}
    try:
        model = cls.config_schema.model_validate(stored)
    except ValidationError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the stored config no longer matches the integration's schema; edit the instance",
        ) from None
    return await _run_test(cls, instance_id=row.id, name=row.name, model=model, factory=factory)


@router.get("/instances/{instance_id}/cards")
def instance_cards(
    instance_id: uuid.UUID, _auth: AuthDep, db: DbDep, registry: RegistryDep
) -> list[CardSpec]:
    row = _get_row(db, instance_id)
    return _stored_integration(registry, row)().cards()

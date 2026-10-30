"""Alert channels (M7.2): ``/channels`` CRUD and ``/channels/{id}/test``. Admin only.

Like instances, a request's ``config`` holds every field of the channel type's schema;
the ``SecretStr`` fields (the Telegram bot token) are encrypted into ``channels.secrets``
and never appear in a response, a log line, an error message or an audit entry.
Responses say only whether each secret is set (``secrets_set``). On PATCH, ``config`` is
merged into the stored one: an omitted secret keeps its value.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import channels as ch
from app.api.deps import AdminDep, DbDep
from app.api.instances import SecretBoxDep
from app.core import audit
from app.core.crypto import DecryptionError, SecretBox
from app.integrations import instance_config as ic
from app.models import Channel, ChannelType

router = APIRouter()
log = logging.getLogger(__name__)

# Upper bound on one test send, on top of the guard's per-request timeouts.
TEST_TIMEOUT_SECONDS = 30.0

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


def get_client_factory() -> ch.ClientFactory:
    """The HTTP client used to send. Tests override this dependency."""
    return ch.default_client_factory


ClientFactoryDep = Annotated[ch.ClientFactory, Depends(get_client_factory)]


class ChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["telegram"]
    name: Name
    # Every config field, secrets included. Validated against the type's schema.
    config: dict[str, Any] = Field(default_factory=dict)


class ChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    # Merged into the stored config: omitted fields, secrets included, stay unchanged.
    config: dict[str, Any] | None = None


class ChannelOut(BaseModel):
    id: int
    type: str
    name: str
    # Non-secret fields only.
    config: dict[str, Any]
    # For each secret field of the schema: whether a value is stored. Never the value.
    secrets_set: dict[str, bool]


class ChannelTestResult(BaseModel):
    ok: bool
    message: str  # never contains a secret


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")


def _get_row(db: Session, channel_id: int) -> Channel:
    row = db.get(Channel, channel_id)
    if row is None:
        raise _not_found()
    return row


def _validate(schema: type[BaseModel], config: dict[str, Any]) -> BaseModel:
    """Validate against the type's schema. Errors never echo the submitted values."""
    try:
        model = schema.model_validate(config)
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
    return model


def _stored_secrets(box: SecretBox, row: Channel) -> dict[str, str]:
    try:
        return ch.decrypt(box, row.id, row.secrets)
    except DecryptionError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this channel's stored secrets can't be decrypted (was the master key changed?)",
        ) from None


def _out(row: Channel, box: SecretBox) -> ChannelOut:
    schema = ch.CHANNEL_SCHEMAS[row.type]
    try:
        stored = ch.decrypt(box, row.id, row.secrets)
    except DecryptionError:
        stored = {}
    return ChannelOut(
        id=row.id,
        type=row.type.value,
        name=row.name,
        config=ic.public_config(row.config, schema),
        secrets_set={key: key in stored for key in sorted(ic.secret_field_keys(schema))},
    )


def _record(
    db: Session, auth: AdminDep, action: audit.Action, row: Channel, changes: dict[str, Any]
) -> None:
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=action,
        target_type="channel",
        target_id=row.id,
        user_id=auth.user.id,
        changes=changes,
        schema=ch.CHANNEL_SCHEMAS[row.type],
    )


@router.get("/channels")
def list_channels(_auth: AdminDep, db: DbDep, box: SecretBoxDep) -> list[ChannelOut]:
    rows = db.scalars(select(Channel).order_by(Channel.name, Channel.id))
    return [_out(row, box) for row in rows]


@router.post("/channels", status_code=status.HTTP_201_CREATED)
def create_channel(body: ChannelCreate, auth: AdminDep, db: DbDep, box: SecretBoxDep) -> ChannelOut:
    channel_type = ChannelType(body.type)
    model = _validate(ch.CHANNEL_SCHEMAS[channel_type], body.config)
    config, secrets = ic.split(model)
    row = Channel(type=channel_type, name=body.name, config=config)
    db.add(row)
    db.flush()  # the id is part of the encryption context
    row.secrets = ch.encrypt(box, row.id, secrets)
    _record(
        db,
        auth,
        audit.Action.CREATE,
        row,
        {
            "type": channel_type.value,
            "name": row.name,
            "config": {**config, **dict.fromkeys(secrets, audit.REDACTED)},
        },
    )
    db.commit()
    return _out(row, box)


@router.get("/channels/{channel_id}")
def get_channel(channel_id: int, _auth: AdminDep, db: DbDep, box: SecretBoxDep) -> ChannelOut:
    return _out(_get_row(db, channel_id), box)


@router.patch("/channels/{channel_id}")
def update_channel(
    channel_id: int, body: ChannelUpdate, auth: AdminDep, db: DbDep, box: SecretBoxDep
) -> ChannelOut:
    row = _get_row(db, channel_id)
    schema = ch.CHANNEL_SCHEMAS[row.type]
    sent = body.model_dump(exclude_unset=True)
    if "name" in sent and sent["name"] is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["body", "name"], "msg": "may not be null", "type": "value_error"}],
        )
    changes = audit.diff({"name": row.name}, {"name": sent.get("name", row.name)})
    if body.config is not None:
        merged = {**row.config, **_stored_secrets(box, row), **body.config}
        model = _validate(schema, merged)
        config, secrets = ic.split(model)
        config_changes = audit.diff(row.config, config)
        for key in body.config.keys() & ic.secret_field_keys(schema):
            config_changes[key] = audit.REDACTED
        if config_changes:
            changes["config"] = config_changes
        row.config = config
        row.secrets = ch.encrypt(box, row.id, secrets)
    if "name" in sent:
        row.name = sent["name"]
    _record(db, auth, audit.Action.UPDATE, row, changes)
    db.commit()
    return _out(row, box)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(channel_id: int, auth: AdminDep, db: DbDep) -> Response:
    row = _get_row(db, channel_id)
    _record(db, auth, audit.Action.DELETE, row, {"type": row.type.value, "name": row.name})
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: int, _auth: AdminDep, db: DbDep, box: SecretBoxDep, factory: ClientFactoryDep
) -> ChannelTestResult:
    """Send a fixed test message. Never raises for a failed send; the result says why."""
    row = _get_row(db, channel_id)
    stored = _stored_secrets(box, row)
    try:
        config = ch.CHANNEL_SCHEMAS[row.type].model_validate({**row.config, **stored})
    except ValidationError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the stored channel settings are no longer valid; edit it"
        ) from None
    secrets = list(stored.values())
    try:
        async with factory() as client:
            await asyncio.wait_for(
                ch.deliver(client, config, ch.TEST_MESSAGE), TEST_TIMEOUT_SECONDS
            )
    except ch.SendError as exc:
        return ChannelTestResult(ok=False, message=ch.scrub(str(exc), secrets))
    except TimeoutError:
        return ChannelTestResult(ok=False, message="sending the test message timed out")
    except Exception as exc:
        # Unexpected: report the type only, its text could hold the request URL.
        log.warning("test message to channel %s raised %s", row.id, type(exc).__name__)
        return ChannelTestResult(ok=False, message=f"sending failed ({type(exc).__name__})")
    return ChannelTestResult(ok=True, message="test message sent")

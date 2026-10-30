"""Network map links (M6.1): ``/links`` CRUD.

A link joins two instances (optionally naming the port on each end). Reading is open to
any user; create, update and delete are admin only (CSRF is enforced by the session
dependency) and audited. Deleting an instance deletes its links (database cascade).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import AdminDep, AuthDep, DbDep
from app.core import audit
from app.models import Instance, Link

router = APIRouter()


def _no_control_chars(value: str) -> str:
    if any(not ch.isprintable() for ch in value):
        raise ValueError("must not contain control characters")
    return value


Label = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(_no_control_chars),
]
Port = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_no_control_chars),
]


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a_instance_id: uuid.UUID
    b_instance_id: uuid.UUID
    label: Label | None = None
    a_port: Port | None = None
    b_port: Port | None = None


class LinkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a_instance_id: uuid.UUID | None = None
    b_instance_id: uuid.UUID | None = None
    label: Label | None = None
    a_port: Port | None = None
    b_port: Port | None = None


class LinkOut(BaseModel):
    id: int
    a_instance_id: uuid.UUID
    b_instance_id: uuid.UUID
    label: str | None
    a_port: str | None
    b_port: str | None


FIELDS = ("a_instance_id", "b_instance_id", "label", "a_port", "b_port")


def _out(row: Link) -> LinkOut:
    return LinkOut(
        id=row.id,
        a_instance_id=row.a_instance_id,
        b_instance_id=row.b_instance_id,
        label=row.label,
        a_port=row.a_port,
        b_port=row.b_port,
    )


def _snapshot(row: Link) -> dict[str, Any]:
    return {f: (str(v) if isinstance(v := getattr(row, f), uuid.UUID) else v) for f in FIELDS}


def _get(db: Session, link_id: int) -> Link:
    row = db.get(Link, link_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    return row


def _check_ends(db: Session, a: uuid.UUID, b: uuid.UUID) -> None:
    if a == b:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a link needs two devices")
    found = set(db.scalars(select(Instance.id).where(Instance.id.in_((a, b)))))
    if found != {a, b}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown device")


@router.get("/links")
def list_links(_auth: AuthDep, db: DbDep) -> list[LinkOut]:
    return [_out(r) for r in db.scalars(select(Link).order_by(Link.id))]


@router.post("/links", status_code=status.HTTP_201_CREATED)
def create_link(body: LinkIn, auth: AdminDep, db: DbDep) -> LinkOut:
    _check_ends(db, body.a_instance_id, body.b_instance_id)
    row = Link(**body.model_dump())
    db.add(row)
    db.flush()
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.CREATE,
        target_type="link",
        target_id=row.id,
        user_id=auth.user.id,
        changes=_snapshot(row),
    )
    db.commit()
    return _out(row)


@router.patch("/links/{link_id}")
def update_link(link_id: int, body: LinkPatch, auth: AdminDep, db: DbDep) -> LinkOut:
    row = _get(db, link_id)
    before = _snapshot(row)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is None and field in ("a_instance_id", "b_instance_id"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "device cannot be empty")
        setattr(row, field, value)
    _check_ends(db, row.a_instance_id, row.b_instance_id)
    changes = audit.diff(before, _snapshot(row))
    if changes:
        audit.record(
            db,
            actor_type=audit.ActorType.USER,
            action=audit.Action.UPDATE,
            target_type="link",
            target_id=row.id,
            user_id=auth.user.id,
            changes=changes,
        )
    db.commit()
    return _out(row)


@router.delete("/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_link(link_id: int, auth: AdminDep, db: DbDep) -> Response:
    row = _get(db, link_id)
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.DELETE,
        target_type="link",
        target_id=row.id,
        user_id=auth.user.id,
        changes=_snapshot(row),
    )
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

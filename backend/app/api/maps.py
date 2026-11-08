"""Network maps (M9.1; maintainer-approved 2026-11-07).

Any logged-in user reads; every write is admin only. A map's drawing is one validated
JSON document (``app.api.map_document``). Saves carry the revision they were based on:
a stale revision answers 409 and changes nothing. Create, rename/move and delete are
audited with the name (``target_type`` ``network_map``); drawing saves are not. There is
no default map, and the last map may be deleted. Positions stay dense from 0.
"""

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from app.api.deps import AdminDep, AuthDep, DbDep
from app.api.map_document import MAX_DOCUMENT_BYTES, MapDocument
from app.core import audit
from app.core.db import utcnow
from app.models import NetworkMap

router = APIRouter()

MAX_MAPS = 50
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class MapCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    copy_from: int | None = None


class MapPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    position: Annotated[int, Field(ge=0)] | None = None


class MapPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: MapDocument
    revision: Annotated[int, Field(ge=1)]


class MapSummary(BaseModel):
    id: int
    name: str
    position: int


class MapOut(BaseModel):
    id: int
    name: str
    position: int
    document: dict[str, Any]
    revision: int
    updated_at: datetime


def _out(row: NetworkMap) -> MapOut:
    return MapOut(
        id=row.id,
        name=row.name,
        position=row.position,
        document=row.document,
        revision=row.revision,
        updated_at=row.updated_at,
    )


def _dump(doc: MapDocument) -> dict[str, Any]:
    data = doc.model_dump(mode="json", by_alias=True)
    if len(json.dumps(data)) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "document is too large")
    return data


def _ordered(db: Session) -> list[NetworkMap]:
    return list(db.scalars(select(NetworkMap).order_by(NetworkMap.position, NetworkMap.id)))


def _renumber(rows: list[NetworkMap]) -> None:
    for position, row in enumerate(rows):
        if row.position != position:
            row.position = position


def _get(db: Session, map_id: int) -> NetworkMap:
    row = db.get(NetworkMap, map_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "map not found")
    return row


def _audit(
    db: Session, auth: AdminDep, action: audit.Action, row: NetworkMap, changes: dict[str, Any]
) -> None:
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=action,
        target_type="network_map",
        target_id=row.id,
        user_id=auth.user.id,
        changes=changes,
    )


@router.get("/maps")
def list_maps(_auth: AuthDep, db: DbDep) -> list[MapSummary]:
    return [MapSummary(id=r.id, name=r.name, position=r.position) for r in _ordered(db)]


@router.post("/maps", status_code=status.HTTP_201_CREATED)
def create_map(body: MapCreate, auth: AdminDep, db: DbDep) -> MapOut:
    rows = _ordered(db)
    if len(rows) >= MAX_MAPS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"at most {MAX_MAPS} maps are allowed")
    document: dict[str, Any] = _dump(MapDocument())
    if body.copy_from is not None:
        source = db.get(NetworkMap, body.copy_from)
        if source is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown map to copy")
        document = source.document
    row = NetworkMap(name=body.name, document=document, position=len(rows))
    db.add(row)
    db.flush()
    changes: dict[str, Any] = {"name": row.name}
    if body.copy_from is not None:
        changes["copy_from"] = body.copy_from
    _audit(db, auth, audit.Action.CREATE, row, changes)
    db.commit()
    return _out(row)


@router.get("/maps/{map_id}")
def get_map(map_id: int, _auth: AuthDep, db: DbDep) -> MapOut:
    return _out(_get(db, map_id))


@router.put("/maps/{map_id}")
def put_map(map_id: int, body: MapPut, _auth: AdminDep, db: DbDep) -> MapOut:
    data = _dump(body.document)
    row = _get(db, map_id)
    # One conditional UPDATE, so two admins saving the same revision at once can't both
    # win: the second matches no row and gets the 409.
    result: CursorResult[Any] = db.execute(  # type: ignore[assignment]
        update(NetworkMap)
        .where(NetworkMap.id == map_id, NetworkMap.revision == body.revision)
        .values(document=data, revision=NetworkMap.revision + 1, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "the map was changed by someone else")
    db.commit()
    db.refresh(row)
    return _out(row)


@router.patch("/maps/{map_id}")
def patch_map(map_id: int, body: MapPatch, auth: AdminDep, db: DbDep) -> MapSummary:
    row = _get(db, map_id)
    before = {"name": row.name, "position": row.position}
    if body.name is not None:
        row.name = body.name
    if body.position is not None:
        rows = [r for r in _ordered(db) if r.id != row.id]
        rows.insert(min(body.position, len(rows)), row)
        _renumber(rows)
    changes = audit.diff(before, {"name": row.name, "position": row.position})
    if changes:
        _audit(db, auth, audit.Action.UPDATE, row, changes)
    db.commit()
    return MapSummary(id=row.id, name=row.name, position=row.position)


@router.delete("/maps/{map_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_map(map_id: int, auth: AdminDep, db: DbDep) -> Response:
    row = _get(db, map_id)
    rows = _ordered(db)
    _audit(db, auth, audit.Action.DELETE, row, {"name": row.name})
    db.delete(row)
    _renumber([r for r in rows if r.id != row.id])
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

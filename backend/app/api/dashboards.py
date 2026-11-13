"""Dashboards (M5.2; several dashboards, maintainer-approved 2026-11-02).

A dashboard stores its card layout as JSON: a bounded list of cards, each placed on a
12-column grid. A card belongs to an instance, or to no instance (``instance_id`` null:
a dashboard-wide card such as "Devices healthy").

Dashboards are global: no owner, no sharing. Any logged-in user may read them; creating,
renaming, moving, editing and deleting are admin only (maintainer-approved 2026-11-13;
CSRF is enforced by the session dependency). The
first dashboard by ``(position, id)`` is the main one; positions stay dense from 0.
Create, rename/move and delete are audited with the name; layout saves are not
(maintainer decision, 2026-10-30: too noisy for a cosmetic change). When no dashboard
exists, the first read of the list or of dashboard 1 creates "Default".
"""

import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import AdminDep, AuthDep, DbDep
from app.core import audit
from app.models import Dashboard

router = APIRouter()

DEFAULT_DASHBOARD_ID = 1
MAX_DASHBOARDS = 50
MAX_CARDS = 500
MAX_LAYOUT_BYTES = 256 * 1024
GRID_COLUMNS = 12

Scalar = str | int | float | bool | None
SettingKey = Annotated[str, Field(max_length=64)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class CardPlacement(BaseModel):
    """One card: which instance (or none) and card type, where it sits, and its settings."""

    model_config = ConfigDict(extra="forbid")
    instance_id: Annotated[str, Field(min_length=1, max_length=64)] | None
    card_type: Annotated[str, Field(min_length=1, max_length=64)]
    x: Annotated[int, Field(ge=0, lt=GRID_COLUMNS)]
    y: Annotated[int, Field(ge=0, le=10_000)]
    w: Annotated[int, Field(ge=1, le=GRID_COLUMNS)]
    h: Annotated[int, Field(ge=1, le=100)]
    settings: Annotated[dict[SettingKey, Scalar], Field(max_length=32)] = {}


class Layout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Bumped by the frontend when it upgrades a saved layout (2 = dashboard-wide cards).
    version: Annotated[int, Field(ge=1, le=100)] = 1
    cards: Annotated[list[CardPlacement], Field(max_length=MAX_CARDS)] = []


class DashboardPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layout: Layout


class DashboardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    copy_from: int | None = None


class DashboardPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    position: Annotated[int, Field(ge=0)] | None = None


class DashboardSummary(BaseModel):
    id: int
    name: str
    position: int


class DashboardOut(BaseModel):
    id: int
    name: str
    layout: Layout


def _layout(row: Dashboard) -> Layout:
    try:
        return Layout.model_validate(row.layout)
    except ValueError:  # a stored layout of an unknown shape: start empty
        return Layout()


def _out(row: Dashboard) -> DashboardOut:
    return DashboardOut(id=row.id, name=row.name, layout=_layout(row))


def _ordered(db: Session) -> list[Dashboard]:
    return list(db.scalars(select(Dashboard).order_by(Dashboard.position, Dashboard.id)))


def _renumber(rows: list[Dashboard]) -> None:
    for position, row in enumerate(rows):
        if row.position != position:
            row.position = position


def _ensure_one(db: Session) -> None:
    """Create "Default" when there is no dashboard at all (a fresh install).

    The id comes from the database, so it is 1 on a table that never had a row. Two
    first reads at the same moment could create two; harmless, either can be deleted.
    """
    if db.scalar(select(func.count()).select_from(Dashboard)) == 0:
        db.add(Dashboard(name="Default", layout={"cards": []}, position=0))
        db.commit()


def _get(db: Session, dashboard_id: int) -> Dashboard:
    row = db.get(Dashboard, dashboard_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dashboard not found")
    return row


def _audit(
    db: Session, auth: AuthDep, action: audit.Action, row: Dashboard, changes: dict[str, object]
) -> None:
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=action,
        target_type="dashboard",
        target_id=row.id,
        user_id=auth.user.id,
        changes=changes,
    )


@router.get("/dashboards")
def list_dashboards(_auth: AuthDep, db: DbDep) -> list[DashboardSummary]:
    _ensure_one(db)
    return [DashboardSummary(id=r.id, name=r.name, position=r.position) for r in _ordered(db)]


@router.post("/dashboards", status_code=status.HTTP_201_CREATED)
def create_dashboard(body: DashboardCreate, auth: AdminDep, db: DbDep) -> DashboardOut:
    rows = _ordered(db)
    if len(rows) >= MAX_DASHBOARDS:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"at most {MAX_DASHBOARDS} dashboards are allowed"
        )
    layout = Layout()
    if body.copy_from is not None:
        source = db.get(Dashboard, body.copy_from)
        if source is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown dashboard to copy")
        layout = _layout(source)
    row = Dashboard(name=body.name, layout=layout.model_dump(mode="json"), position=len(rows))
    db.add(row)
    db.flush()
    changes: dict[str, object] = {"name": row.name}
    if body.copy_from is not None:
        changes["copy_from"] = body.copy_from
    _audit(db, auth, audit.Action.CREATE, row, changes)
    db.commit()
    return _out(row)


@router.get("/dashboards/{dashboard_id}")
def get_dashboard(dashboard_id: int, _auth: AuthDep, db: DbDep) -> DashboardOut:
    if dashboard_id == DEFAULT_DASHBOARD_ID:
        _ensure_one(db)
    return _out(_get(db, dashboard_id))


@router.put("/dashboards/{dashboard_id}")
def put_dashboard(
    dashboard_id: int, body: DashboardPut, _auth: AdminDep, db: DbDep
) -> DashboardOut:
    layout = body.layout.model_dump(mode="json")
    if len(json.dumps(layout)) > MAX_LAYOUT_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "layout is too large")
    row = _get(db, dashboard_id)
    row.layout = layout
    db.commit()
    return _out(row)


@router.patch("/dashboards/{dashboard_id}")
def patch_dashboard(
    dashboard_id: int, body: DashboardPatch, auth: AdminDep, db: DbDep
) -> DashboardSummary:
    row = _get(db, dashboard_id)
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
    return DashboardSummary(id=row.id, name=row.name, position=row.position)


@router.delete("/dashboards/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dashboard(dashboard_id: int, auth: AdminDep, db: DbDep) -> Response:
    row = _get(db, dashboard_id)
    rows = _ordered(db)
    if len(rows) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "the last dashboard can't be deleted")
    _audit(db, auth, audit.Action.DELETE, row, {"name": row.name})
    db.delete(row)
    _renumber([r for r in rows if r.id != row.id])
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

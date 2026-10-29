"""Dashboards (M5.2): ``GET/PUT /dashboards/{id}``.

A dashboard stores its card layout as JSON: a bounded list of cards, each placed on a
12-column grid. GET is open to any user; PUT is admin only (CSRF is enforced by the
session dependency) and audited. Dashboard 1, "Default", is created on its first GET.
"""

import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from app.api.deps import AdminDep, AuthDep, DbDep
from app.core import audit
from app.models import Dashboard

router = APIRouter()

DEFAULT_DASHBOARD_ID = 1
MAX_CARDS = 500
MAX_LAYOUT_BYTES = 256 * 1024
GRID_COLUMNS = 12

Scalar = str | int | float | bool | None
SettingKey = Annotated[str, Field(max_length=64)]


class CardPlacement(BaseModel):
    """One card: which instance and card type, where it sits, and its settings."""

    model_config = ConfigDict(extra="forbid")
    instance_id: Annotated[str, Field(min_length=1, max_length=64)]
    card_type: Annotated[str, Field(min_length=1, max_length=64)]
    x: Annotated[int, Field(ge=0, lt=GRID_COLUMNS)]
    y: Annotated[int, Field(ge=0, le=10_000)]
    w: Annotated[int, Field(ge=1, le=GRID_COLUMNS)]
    h: Annotated[int, Field(ge=1, le=100)]
    settings: Annotated[dict[SettingKey, Scalar], Field(max_length=32)] = {}


class Layout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cards: Annotated[list[CardPlacement], Field(max_length=MAX_CARDS)] = []


class DashboardPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layout: Layout


class DashboardOut(BaseModel):
    id: int
    name: str
    layout: Layout


def _out(row: Dashboard) -> DashboardOut:
    try:
        layout = Layout.model_validate(row.layout)
    except ValueError:  # a stored layout of an unknown shape: start empty
        layout = Layout()
    return DashboardOut(id=row.id, name=row.name, layout=layout)


@router.get("/dashboards/{dashboard_id}")
def get_dashboard(dashboard_id: int, _auth: AuthDep, db: DbDep) -> DashboardOut:
    row = db.get(Dashboard, dashboard_id)
    if row is None and dashboard_id == DEFAULT_DASHBOARD_ID:
        try:
            db.add(Dashboard(id=DEFAULT_DASHBOARD_ID, name="Default", layout={"cards": []}))
            db.commit()
        except IntegrityError:  # another request created it first
            db.rollback()
        row = db.get(Dashboard, dashboard_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dashboard not found")
    return _out(row)


@router.put("/dashboards/{dashboard_id}")
def put_dashboard(dashboard_id: int, body: DashboardPut, auth: AdminDep, db: DbDep) -> DashboardOut:
    layout = body.layout.model_dump(mode="json")
    if len(json.dumps(layout)) > MAX_LAYOUT_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "layout is too large")
    row = db.get(Dashboard, dashboard_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dashboard not found")
    row.layout = layout
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.UPDATE,
        target_type="dashboard",
        target_id=row.id,
        user_id=auth.user.id,
        changes={"cards": len(body.layout.cards)},
    )
    db.commit()
    return _out(row)

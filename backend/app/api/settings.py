"""Time settings (maintainer-approved 2026-11-07): one timezone for everyone."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import AdminDep, AuthDep, DbDep
from app.core import audit, timezone
from app.models import Setting

router = APIRouter()


class TimeSettings(BaseModel):
    timezone: str
    # The backend's current time, UTC. The clock is the host's; Vantage never sets it.
    server_time: datetime
    clock_source: Literal["host"] = "host"
    timezones: list[str]


class TimePut(BaseModel):
    timezone: str


def _view(name: str) -> TimeSettings:
    return TimeSettings(
        timezone=name, server_time=datetime.now(UTC), timezones=timezone.available()
    )


@router.get("/settings/time")
def get_time(_auth: AuthDep, db: DbDep) -> TimeSettings:
    return _view(timezone.stored_name(db))


@router.put("/settings/time")
def put_time(body: TimePut, auth: AdminDep, db: DbDep) -> TimeSettings:
    if not timezone.is_valid(body.timezone):
        raise HTTPException(422, "unknown timezone")
    old = timezone.stored_name(db)
    row = db.get(Setting, timezone.SETTING_KEY)
    if row is None:
        db.add(Setting(key=timezone.SETTING_KEY, value=body.timezone))
    else:
        row.value = body.timezone
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.UPDATE,
        target_type="settings",
        target_id="timezone",
        user_id=auth.user.id,
        changes={"from": old, "to": body.timezone},
    )
    db.commit()
    timezone.set_zone(body.timezone)
    return _view(body.timezone)

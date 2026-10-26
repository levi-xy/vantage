"""Audit log, read-only, admins only (Contract 3: ``GET /audit``). The UI is M3.4."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import AdminDep, DbDep
from app.models import AuditLog

router = APIRouter()

MAX_LIMIT = 200


class AuditEntry(BaseModel):
    id: int
    user_id: int | None
    action: str
    target_type: str
    target_id: str | None
    at: datetime
    # Already redacted when written: secret fields hold "<changed>".
    changes: dict[str, Any]


@router.get("/audit")
def list_audit(
    _auth: AdminDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[AuditEntry]:
    """Newest first. Page backwards by passing the last ``id`` seen as ``before_id``."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    return [
        AuditEntry(
            id=row.id,
            user_id=row.user_id,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            at=row.at,
            changes=row.changes,
        )
        for row in db.scalars(stmt)
    ]

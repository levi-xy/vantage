"""What is running (maintainer-approved 2026-11-13): ``GET /about``, any logged-in user.

Read-only. Every value that can't be known is null; no error text, no addresses.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import version
from app.api.deps import AuthDep, DbDep
from app.api.query import PrometheusDep

router = APIRouter()
log = logging.getLogger(__name__)


class About(BaseModel):
    # The release version (VANTAGE_RELEASE, else the build version); the version the image
    # was built as (an rc's for a promoted final release); its commit (None = unknown).
    backend_version: str
    backend_build_version: str
    backend_commit: str | None
    backend_build_date: str | None
    schema_version: str | None
    prometheus_version: str | None
    started_at: datetime


@router.get("/about")
async def about(_auth: AuthDep, db: DbDep, prometheus: PrometheusDep, request: Request) -> About:
    schema: str | None = None
    try:
        value = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        schema = value if isinstance(value, str) else None
    except SQLAlchemyError:
        log.debug("alembic_version unreadable")
    info = version.current()
    return About(
        backend_version=info.release,
        backend_build_version=info.build,
        backend_commit=info.commit,
        backend_build_date=info.build_date,
        schema_version=schema,
        prometheus_version=await prometheus.version(),
        started_at=request.app.state.started_at,
    )

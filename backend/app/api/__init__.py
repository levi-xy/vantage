"""HTTP API v1 (AGENTS.md Contract 3)."""

from fastapi import APIRouter

from app.api import audit, auth, dashboards, instances, integrations, users

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(audit.router)
api_router.include_router(dashboards.router)
api_router.include_router(integrations.router)
api_router.include_router(instances.router)
api_router.include_router(users.router)

__all__ = ["api_router"]

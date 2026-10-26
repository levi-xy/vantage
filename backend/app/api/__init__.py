"""HTTP API v1 (AGENTS.md Contract 3)."""

from fastapi import APIRouter

from app.api import audit, auth

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(audit.router)

__all__ = ["api_router"]

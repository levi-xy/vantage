"""HTTP API v1 (AGENTS.md Contract 3)."""

from fastapi import APIRouter

from app.api import auth

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)

__all__ = ["api_router"]

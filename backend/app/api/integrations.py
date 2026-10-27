"""Integration types (Contract 3: ``GET /integrations``)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import AuthDep
from app.integrations.registry import IntegrationInfo, Registry

router = APIRouter()


def get_registry(request: Request) -> Registry:
    registry: Registry = request.app.state.registry
    return registry


RegistryDep = Annotated[Registry, Depends(get_registry)]


@router.get("/integrations")
def list_integrations(_auth: AuthDep, registry: RegistryDep) -> list[IntegrationInfo]:
    """Every registered integration type, with its config schema (secrets ``writeOnly``)."""
    return registry.info()

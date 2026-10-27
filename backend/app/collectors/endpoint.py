"""``GET /metrics``: polled metrics and Vantage's own, for Prometheus only (Contract 3).

Protected by the Prometheus bearer token (``core/prometheus_auth.py``, shared with
``/sd/targets``): 401 without the right token, 503 when none is configured.
"""

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.registry import CollectorRegistry

from app.core.prometheus_auth import PrometheusAuthDep

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, _auth: PrometheusAuthDep) -> Response:
    # async on purpose: the store is only touched from the event loop.
    registry: CollectorRegistry = request.app.state.metrics_registry
    return Response(
        generate_latest(registry),
        media_type=CONTENT_TYPE_LATEST,
        headers={"Cache-Control": "no-store"},
    )

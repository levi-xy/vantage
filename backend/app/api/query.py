"""PromQL query proxy (Contract 3: ``POST /query``). Any logged-in user; CSRF checked."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import AuthDep
from app.query.proxy import PrometheusClient, QueryError, QueryGate, QueryRequest, QueryResult

router = APIRouter()


def get_prometheus(request: Request) -> PrometheusClient:
    client: PrometheusClient = request.app.state.prometheus
    return client


PrometheusDep = Annotated[PrometheusClient, Depends(get_prometheus)]


def get_query_gate(request: Request) -> QueryGate:
    gate: QueryGate = request.app.state.query_gate
    return gate


QueryGateDep = Annotated[QueryGate, Depends(get_query_gate)]


@router.post("/query")
async def query(
    body: QueryRequest, auth: AuthDep, prometheus: PrometheusDep, gate: QueryGateDep
) -> QueryResult:
    """Instant query without ``start``/``end``, range query with them, within the budget.

    Body: ``{query, start?, end?, step?}`` (Unix seconds; ``step`` in seconds). Response::

        {"result_type": "vector" | "matrix" | "scalar" | "string",
         "series": [{"labels": {...}, "values": [[unix_seconds, "value"], ...]}],
         "warnings": ["..."]}

    An instant query gives one ``values`` pair per series; a scalar or string result is
    one series with no labels. Values are strings, as Prometheus sends them, so ``NaN``
    and ``+Inf`` survive JSON. Over budget: 422 with a short ``detail``. Too many queries
    already running (in total, or for this session): 429 at once, nothing waits.
    """
    key = auth.session.id
    if not gate.acquire(key):
        raise HTTPException(
            429, "too many queries in progress; try again", headers={"Retry-After": "1"}
        )
    try:
        return await prometheus.query(body)
    except QueryError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    finally:
        gate.release(key)

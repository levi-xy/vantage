"""PromQL query proxy (M5.1): budget checks, the call to Prometheus, a stable result shape.

The browser never talks to Prometheus (hard rule 4); cards send PromQL to
``POST /api/v1/query`` and this module forwards it to the bundled Prometheus.

Prometheus is Vantage's own internal service, not a device, so this client does not go
through ``net_guard``. That opens no SSRF path: the base URL comes only from server
config (``VANTAGE_PROMETHEUS_URL``), the paths are fixed, and a request supplies only the
PromQL text and numbers, sent as form fields. Redirects are not followed and proxy
environment variables are ignored.
"""

import asyncio
import json
import logging
import math
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# --- budget (documented in docs/SECURITY.md) -------------------------------------------
MAX_QUERY_LENGTH = 2_000  # characters of PromQL
MAX_RANGE_SECONDS = 31 * 24 * 3600  # longest range query (default retention is 30 days)
MIN_STEP_SECONDS = 15.0  # half the 30 s scrape interval; finer steps add nothing
MAX_POINTS_PER_SERIES = 1_100  # (end - start) / step + 1
DEFAULT_POINTS = 300  # used to pick a step when a range query gives none
MAX_SERIES = 500  # series in one result
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # Prometheus' JSON answer
QUERY_TIMEOUT_SECONDS = 10  # Prometheus' own evaluation timeout
TOTAL_TIMEOUT_SECONDS = 15.0  # the whole call to Prometheus
MAX_WARNINGS = 10
MAX_WARNING_LENGTH = 300

ResultType = Literal["vector", "matrix", "scalar", "string"]


class QueryRequest(BaseModel):
    """``{query, start?, end?, step?}``. Times are Unix seconds; ``step`` is seconds.

    No ``start``/``end`` = instant query (now); both = range query.
    """

    model_config = {"extra": "forbid"}

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    start: float | None = Field(default=None, allow_inf_nan=False)
    end: float | None = Field(default=None, allow_inf_nan=False)
    step: float | None = Field(default=None, allow_inf_nan=False)


class Series(BaseModel):
    """One series. ``values`` are ``[unix_seconds, "value"]`` pairs.

    Values stay strings, as Prometheus sends them, so ``NaN`` and ``+Inf`` survive JSON.
    An instant query gives one pair per series; a scalar or string result is one series
    with no labels.
    """

    labels: dict[str, str]
    values: list[tuple[float, str]]


class QueryResult(BaseModel):
    result_type: ResultType
    series: list[Series]
    warnings: list[str] = []


class QueryError(Exception):
    """Refused or failed query. ``message`` is short and safe to return to the client."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def check_budget(req: QueryRequest) -> dict[str, str]:
    """Validate the time range and step; return the form fields for Prometheus."""
    if req.start is None and req.end is None:
        if req.step is not None:
            raise QueryError(422, "step needs start and end")
        return {"query": req.query}
    if req.start is None or req.end is None:
        raise QueryError(422, "start and end go together")
    span = req.end - req.start
    if span < 0:
        raise QueryError(422, "end is before start")
    if span > MAX_RANGE_SECONDS:
        raise QueryError(422, f"range is longer than {MAX_RANGE_SECONDS // 86400} days")
    step = req.step
    if step is None:
        step = max(MIN_STEP_SECONDS, math.ceil(span / (DEFAULT_POINTS - 1)))
    if step < MIN_STEP_SECONDS:
        raise QueryError(422, f"step is shorter than {MIN_STEP_SECONDS:g} seconds")
    if math.floor(span / step) + 1 > MAX_POINTS_PER_SERIES:
        raise QueryError(422, f"more than {MAX_POINTS_PER_SERIES} points per series")
    return {"query": req.query, "start": repr(req.start), "end": repr(req.end), "step": repr(step)}


class PrometheusClient:
    """Calls the configured Prometheus. Tests pass an ``httpx.MockTransport``."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query(self, req: QueryRequest) -> QueryResult:
        form = check_budget(req)
        path = "/api/v1/query_range" if "start" in form else "/api/v1/query"
        form["timeout"] = f"{QUERY_TIMEOUT_SECONDS}s"
        # One more than allowed, so "too many" is detectable without a huge answer.
        form["limit"] = str(MAX_SERIES + 1)
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                status_code, body = await self._post(path, form)
        except TimeoutError:
            raise QueryError(504, "query timed out") from None
        except httpx.HTTPError as exc:
            # Class name only: the exception text names Prometheus' address.
            log.warning("Prometheus unreachable: %s", type(exc).__name__)
            raise QueryError(503, "metrics backend unavailable") from None
        return _parse(status_code, body)

    async def _post(self, path: str, form: dict[str, str]) -> tuple[int, bytes]:
        async with self._client.stream("POST", path, data=form) as response:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise QueryError(422, "result is too large; narrow the query")
                chunks.append(chunk)
            return response.status_code, b"".join(chunks)


def _parse(status_code: int, body: bytes) -> QueryResult:
    try:
        payload: Any = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        log.warning("Prometheus answered HTTP %d without JSON", status_code)
        raise QueryError(502, "metrics backend error")

    if payload.get("status") != "success":
        # Prometheus' own error text is not passed on: keep its internals out of responses.
        error_type = payload.get("errorType")
        if error_type == "bad_data":
            raise QueryError(400, "invalid PromQL query")
        if error_type in ("timeout", "canceled"):
            raise QueryError(504, "query timed out")
        if error_type == "execution":
            raise QueryError(422, "query could not be executed")
        log.warning("Prometheus error: HTTP %d, %s", status_code, str(error_type)[:40])
        raise QueryError(502, "metrics backend error")

    try:
        data = payload["data"]
        result_type = data["resultType"]
        raw = data["result"]
        if result_type in ("scalar", "string"):
            series = [Series(labels={}, values=[_point(raw)])]
        elif result_type == "vector":
            series = [
                Series(labels=item["metric"], values=[_point(item["value"])])
                for item in raw
                if "value" in item  # native-histogram samples aren't supported yet
            ]
        elif result_type == "matrix":
            series = [
                Series(labels=item["metric"], values=[_point(p) for p in item.get("values", [])])
                for item in raw
            ]
        else:
            raise ValueError("unknown result type")
    except (KeyError, TypeError, ValueError):
        log.warning("Prometheus answered with an unexpected shape")
        raise QueryError(502, "metrics backend error") from None

    if len(series) > MAX_SERIES:
        raise QueryError(422, f"more than {MAX_SERIES} series; narrow the query")
    warnings = [
        str(w)[:MAX_WARNING_LENGTH]
        for w in (payload.get("warnings") or [])[:MAX_WARNINGS]
        if isinstance(w, str)
    ]
    return QueryResult(result_type=result_type, series=series, warnings=warnings)


def _point(raw: Any) -> tuple[float, str]:
    ts, value = raw
    if not isinstance(value, str):
        raise ValueError("value is not a string")
    return float(ts), value

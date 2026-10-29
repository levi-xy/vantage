"""Cloudflare account: per-zone traffic analytics and Cloudflare Tunnel status.

**Polled**: Vantage calls the Cloudflare API at ``api.cloudflare.com`` (the integration's
only ``cloud_hosts`` entry) through ``ctx.http_client()``, the SSRF-guarded client, and
exposes ``vantage_cloudflare_*`` on its own ``/metrics``. The guard lets that one name
through only over HTTPS with certificate and hostname verification on, and only when
every DNS answer is a public address. Nothing else is ever contacted.

Read-only (Hard rule 2), enforced by :func:`_request` before anything is sent:

* ``GET`` only, and only the two REST paths in :data:`ALLOWED_GETS` (list zones, list
  tunnels).
* ``POST`` only to ``/client/v4/graphql`` (the GraphQL Analytics API has no other way
  to be read; maintainer-approved exception, 2026-10-29, ``docs/INTEGRATIONS.md``
  section 7). The body is built by :func:`graphql_body` from a document in
  :data:`QUERY_DOCUMENTS` (constants in this file) and variables limited to zone IDs, a
  time window and a limit. A document that is not one of those constants, or is not a
  single ``query`` operation, is refused before sending, so no mutation or subscription
  can ever go out.

The API token should be read-only (``docs/integrations/cloudflare.md``): Account
Analytics Read, Zone Analytics Read, Zone Read and Cloudflare Tunnel Read.

Secret: the API token, a top-level ``SecretStr``. It goes into the ``Authorization``
header for requests to ``api.cloudflare.com`` and nowhere else (redirects are not
followed). Messages are fixed text plus the API host; never the token, the account ID,
a URL, or Cloudflare's own error text.

Rate limits: one poll is two REST requests plus one GraphQL query per
:data:`ZONES_PER_QUERY` zones, every :data:`POLL_INTERVAL_SECONDS`. An HTTP 429 (or a
GraphQL rate-limit error) pauses every request of that instance for the ``Retry-After``
the API sent (bounded), and the instance shows the reason meanwhile.
"""

import asyncio
import datetime as dt
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.core.net_guard import NetGuardError
from app.integrations.base import (
    CardSpec,
    Category,
    FirewallHint,
    InstanceContext,
    Integration,
    Sample,
    TestResult,
)
from app.integrations.builtin.common import USER_AGENT, connection_failure, is_tls_failure
from app.integrations.builtin.ifmib import MAX_LABEL_TEXT, clean_text
from app.integrations.errors import PollFailure

TYPE_ID = "cloudflare"
PREFIX = f"vantage_{TYPE_ID}"

API_HOST = "api.cloudflare.com"
API_PORT = 443
WHERE = f"{API_HOST}:{API_PORT}"
API_ROOT = "/client/v4"
PATH_GRAPHQL = f"{API_ROOT}/graphql"

# --- metric names: vantage_<type_id>_<metric> (Contract 1) -------------------------------

METRIC_ZONES = f"{PREFIX}_zones"
METRIC_ZONES_OMITTED = f"{PREFIX}_zones_omitted"
METRIC_TUNNELS = f"{PREFIX}_tunnels"
METRIC_TUNNELS_OMITTED = f"{PREFIX}_tunnels_omitted"

# Value 1; the only series that carries the zone's name (label exception, section 9).
METRIC_ZONE_INFO = f"{PREFIX}_zone_info"
METRIC_ZONE_ACTIVE = f"{PREFIX}_zone_active"
METRIC_ZONE_PAUSED = f"{PREFIX}_zone_paused"

# 1 when every analytics query of this poll succeeded, else 0 (and the zone traffic
# series below are left out or partial).
METRIC_ANALYTICS_UP = f"{PREFIX}_analytics_up"
# End of the analytics window (the last complete UTC hour) the zone values cover.
METRIC_WINDOW_END = f"{PREFIX}_analytics_window_end_timestamp_seconds"
# Totals over the analytics window (one hour), per zone. Gauges: each poll replaces them.
METRIC_ZONE_REQUESTS = f"{PREFIX}_zone_requests"
METRIC_ZONE_CACHED_REQUESTS = f"{PREFIX}_zone_cached_requests"
METRIC_ZONE_BYTES = f"{PREFIX}_zone_bytes"
METRIC_ZONE_CACHED_BYTES = f"{PREFIX}_zone_cached_bytes"
METRIC_ZONE_THREATS = f"{PREFIX}_zone_threats"
METRIC_ZONE_PAGE_VIEWS = f"{PREFIX}_zone_page_views"
METRIC_ZONE_BY_STATUS_CLASS = f"{PREFIX}_zone_requests_by_status_class"
METRIC_ZONE_BY_STATUS = f"{PREFIX}_zone_requests_by_status"
METRIC_ZONE_BY_COUNTRY = f"{PREFIX}_zone_requests_by_country"
METRIC_ZONE_THREATS_BY_COUNTRY = f"{PREFIX}_zone_threats_by_country"

# Value 1; the only series that carries the tunnel's name (label exception, section 9).
METRIC_TUNNEL_INFO = f"{PREFIX}_tunnel_info"
METRIC_TUNNEL_HEALTHY = f"{PREFIX}_tunnel_healthy"
METRIC_TUNNEL_STATUS = f"{PREFIX}_tunnel_status"  # one series per TUNNEL_STATUSES value
METRIC_TUNNEL_CONNECTIONS = f"{PREFIX}_tunnel_connections"

# --- bounds ------------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 300
# The guarded client stops any request after 15 s in total.
MAX_TIMEOUT_SECONDS = 15
# Cloudflare's own page-size maximum for listing zones; one page is all we read.
ZONES_PAGE = 50
MAX_ZONES = ZONES_PAGE
TUNNELS_PAGE = 100
MAX_TUNNELS = TUNNELS_PAGE
# Zones per GraphQL query; more zones means more queries (still a handful per poll).
ZONES_PER_QUERY = 10
MAX_TOP_COUNTRIES = 25
MAX_TOP_STATUSES = 20
OTHER = "other"
STATUS_CLASSES = ("1xx", "2xx", "3xx", "4xx", "5xx", OTHER)
TUNNEL_STATUSES = ("healthy", "degraded", "down", "inactive")
# Real answers are a few KB to a few hundred KB.
MAX_BODY_BYTES = 4 * 1024 * 1024
_PARSE_IN_THREAD_BYTES = 64 * 1024
# The collector cuts a poll off after min(interval, 60) s; stop first with a clear message.
POLL_BUDGET_SECONDS = 50.0
# Backoff after a 429: Retry-After (whole seconds) is honoured within these bounds; one
# poll interval when the API sends none, or something we can't read.
MIN_BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 3600
DEFAULT_BACKOFF_SECONDS = POLL_INTERVAL_SECONDS

# --- identifiers -------------------------------------------------------------------------
# Account and zone IDs are 32 lowercase hex digits; tunnel IDs are UUIDs. Each is checked
# before it becomes a label, part of a path or a GraphQL variable.
_HEX32 = r"[0-9a-f]{32}"
_HEX32_RE = re.compile(_HEX32)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_COUNTRY_RE = re.compile(r"[A-Z0-9]{2}")  # ISO 3166-1 alpha-2, plus Cloudflare's XX / T1
_TOKEN_RE = re.compile(r"[!-~]{1,256}")  # printable ASCII, no spaces (it goes into a header)
_WINDOW_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00Z")


def zones_path(account_id: str) -> str:
    return (
        f"{API_ROOT}/zones?account.id={account_id}&per_page={ZONES_PAGE}&page=1"
        "&order=name&direction=asc"
    )


def tunnels_path(account_id: str) -> str:
    return (
        f"{API_ROOT}/accounts/{account_id}/cfd_tunnel?is_deleted=false"
        f"&per_page={TUNNELS_PAGE}&page=1"
    )


# Every GET the integration may send: exactly these paths, with this query.
ALLOWED_GETS: tuple[re.Pattern[str], ...] = (
    re.compile(re.escape(zones_path("ACCOUNT")).replace("ACCOUNT", _HEX32)),
    re.compile(re.escape(tunnels_path("ACCOUNT")).replace("ACCOUNT", _HEX32)),
)

# --- GraphQL ---------------------------------------------------------------------------
# The one query document: the last complete hour of HTTP traffic for up to
# ZONES_PER_QUERY zones (httpRequests1hGroups, available on every plan). Values are never
# written into the document; they travel only as the variables below.
ZONE_TRAFFIC_QUERY = """\
query VantageZoneTraffic($zoneTags: [string!], $start: Time!, $end: Time!, $limit: uint64!) {
  viewer {
    zones(filter: {zoneTag_in: $zoneTags}) {
      zoneTag
      httpRequests1hGroups(limit: $limit, filter: {datetime_geq: $start, datetime_lt: $end}) {
        sum {
          requests
          cachedRequests
          bytes
          cachedBytes
          threats
          pageViews
          countryMap {
            clientCountryName
            requests
            threats
          }
          responseStatusMap {
            edgeResponseStatus
            requests
          }
        }
      }
    }
  }
}
"""

QUERY_DOCUMENTS: frozenset[str] = frozenset({ZONE_TRAFFIC_QUERY})
GRAPHQL_VARIABLES = frozenset({"zoneTags", "start", "end", "limit"})
MAX_GRAPHQL_LIMIT = 10

_GRAPHQL_COMMENT = re.compile(r"#[^\n]*")
_GRAPHQL_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_FORBIDDEN_OPERATIONS = frozenset({"mutation", "subscription", "fragment"})

logger = logging.getLogger(__name__)


def is_single_query(document: str) -> bool:
    """Whether ``document`` is exactly one GraphQL ``query`` operation.

    Conservative on purpose: it refuses anything with the words ``mutation``,
    ``subscription`` or ``fragment`` (even as a field name), string literals (values
    belong in variables), an anonymous ``{ ... }`` shorthand, or more than one top-level
    selection set.
    """
    text = _GRAPHQL_COMMENT.sub(" ", document).strip()
    if '"' in text or not re.match(r"query\b", text):
        return False
    if _FORBIDDEN_OPERATIONS & set(_GRAPHQL_NAME.findall(text)):
        return False
    depth = 0
    closed = 0
    for char in text:
        if char == "{":
            if depth == 0 and closed:
                return False  # a second top-level block
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
            if depth == 0:
                closed += 1
    return depth == 0 and closed == 1 and text.endswith("}")


class GraphQLRefused(ValueError):
    """A GraphQL request that the read-only rules don't allow. Nothing was sent."""


def _check_variables(variables: Mapping[str, Any]) -> None:
    if set(variables) - GRAPHQL_VARIABLES:
        raise GraphQLRefused("unexpected GraphQL variable")
    tags = variables.get("zoneTags")
    if tags is not None and (
        not isinstance(tags, list)
        or not 1 <= len(tags) <= ZONES_PER_QUERY
        or not all(isinstance(t, str) and _HEX32_RE.fullmatch(t) for t in tags)
    ):
        raise GraphQLRefused("zoneTags must be 1 to 10 zone IDs")
    for key in ("start", "end"):
        value = variables.get(key)
        if value is not None and not (isinstance(value, str) and _WINDOW_TIME_RE.fullmatch(value)):
            raise GraphQLRefused(f"{key} must be a whole UTC hour")
    limit = variables.get("limit")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_GRAPHQL_LIMIT
    ):
        raise GraphQLRefused("limit out of range")


def graphql_body(document: str, variables: Mapping[str, Any]) -> bytes:
    """The JSON body of a GraphQL request, or :class:`GraphQLRefused`.

    Only a document from :data:`QUERY_DOCUMENTS` (a constant of this module) that is a
    single ``query`` operation, with only the allowed variables.
    """
    if document not in QUERY_DOCUMENTS or not is_single_query(document):
        raise GraphQLRefused("only Vantage's own read-only GraphQL queries may be sent")
    _check_variables(variables)
    return json.dumps({"query": document, "variables": dict(variables)}).encode()


def _check_graphql_payload(content: bytes) -> None:
    """Defence in depth at the point of sending: the body is one of ours."""
    try:
        payload = json.loads(content)
    except ValueError:
        raise GraphQLRefused("GraphQL body is not JSON") from None
    if not isinstance(payload, dict) or set(payload) != {"query", "variables"}:
        raise GraphQLRefused("GraphQL body has unexpected fields")
    document = payload["query"]
    variables = payload["variables"]
    if not isinstance(document, str) or not isinstance(variables, dict):
        raise GraphQLRefused("GraphQL body has unexpected fields")
    graphql_body(document, variables)


def allowed_request(method: str, target: str) -> bool:
    """Whether ``method`` and ``target`` (path and query) may be sent at all. A ``POST``
    also needs its body to pass :func:`graphql_body`."""
    if method == "GET":
        return any(pattern.fullmatch(target) for pattern in ALLOWED_GETS)
    return method == "POST" and target == PATH_GRAPHQL


# --- failures --------------------------------------------------------------------------


class CloudflareAuthFailed(PollFailure):
    """HTTP 400/401/403 on the zone list: the token is invalid, expired or lacks Zone Read."""


class CloudflareTunnelsForbidden(PollFailure):
    """HTTP 403 on the tunnel list: the token lacks Cloudflare Tunnel Read."""


class CloudflareRateLimited(PollFailure):
    """HTTP 429 (or a GraphQL rate-limit error): requests pause for Retry-After."""


class CloudflareTlsUntrusted(PollFailure):
    """The TLS handshake failed (for example, a TLS-intercepting proxy)."""


class CloudflareUnreachable(PollFailure):
    """The connection was refused or the API is unreachable."""


class CloudflareTimeout(PollFailure):
    """A request got no answer within the configured timeout."""


class CloudflareRequestFailed(PollFailure):
    """Any other HTTP failure (an unexpected status, a broken connection)."""


class CloudflareBadAnswer(PollFailure):
    """The answer was not the Cloudflare API's JSON (a proxy, too large, malformed)."""


class CloudflarePollTooSlow(PollFailure):
    """The whole poll took longer than :data:`POLL_BUDGET_SECONDS`."""


class _HttpStatus(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _NotTheApi(Exception):
    """The body was not the API's JSON envelope. Internal."""


class _RateLimited(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(seconds)
        self.seconds = seconds


class _GraphQLErrors(Exception):
    """GraphQL answered with ``errors``. Their text is never shown."""


# --- rate-limit backoff ----------------------------------------------------------------
# Deadlines (monotonic) per instance. Only instances that were rate limited in the last
# MAX_BACKOFF_SECONDS are in here; expired entries are pruned on every look-up.

_backoff_until: dict[uuid.UUID, float] = {}


def _monotonic() -> float:
    return time.monotonic()


def backoff_remaining(instance_id: uuid.UUID) -> int:
    """Whole seconds this instance still waits before its next request (0 = none)."""
    now = _monotonic()
    for key in [k for k, until in _backoff_until.items() if until <= now]:
        del _backoff_until[key]
    until = _backoff_until.get(instance_id)
    return 0 if until is None else max(1, math.ceil(until - now))


def _start_backoff(instance_id: uuid.UUID, seconds: int) -> None:
    _backoff_until[instance_id] = _monotonic() + seconds


def retry_after_seconds(value: str | None) -> int:
    """``Retry-After`` in whole seconds, bounded; the default when absent or unreadable
    (an HTTP date is not parsed: the default applies)."""
    if value is None or not re.fullmatch(r"[0-9]{1,7}", value.strip()):
        return DEFAULT_BACKOFF_SECONDS
    return min(max(int(value.strip()), MIN_BACKOFF_SECONDS), MAX_BACKOFF_SECONDS)


def _msg_rate_limited(seconds: int) -> str:
    return (
        f"Cloudflare API rate limit reached: Vantage sends no request for {seconds} s "
        "(as the API asked)"
    )


def _msg_waiting(seconds: int) -> str:
    return f"Cloudflare API rate limit: waiting {seconds} s more before the next request"


# --- config ----------------------------------------------------------------------------


class CloudflareConfig(BaseModel):
    """What the admin enters. The JSON schema of this model drives the UI form.

    There is no host field: the integration only ever talks to ``api.cloudflare.com``.
    The account ID is plaintext config, shown to every user (it is not a credential).
    """

    account_id: str = Field(
        title="Account ID",
        description=(
            "The 32-character account ID, shown in the Cloudflare dashboard (Account home, "
            "or the right-hand column of a zone's Overview). Stored in plaintext."
        ),
        max_length=64,  # room for pasted whitespace; the validator wants 32 hex digits
    )
    api_token: SecretStr = Field(
        title="API token",
        description=(
            "A Cloudflare API token with read-only permissions only: Account Analytics "
            "Read, Zone Analytics Read, Zone Read and Cloudflare Tunnel Read, limited to "
            "this account. Never use a Global API Key or a token that can edit anything."
        ),
    )
    include_analytics: bool = Field(
        default=True,
        title="Zone traffic analytics",
        description="Traffic, cache, status codes, countries and threats per zone (GraphQL).",
    )
    include_tunnels: bool = Field(
        default=True,
        title="Tunnel status",
        description="Status and connection count of each Cloudflare Tunnel in the account.",
    )
    max_zones: int = Field(
        default=10,
        ge=1,
        le=MAX_ZONES,
        title="Zones per poll (at most)",
        description="The first zones by name are reported, up to this many.",
    )
    top_countries: int = Field(
        default=10,
        ge=1,
        le=MAX_TOP_COUNTRIES,
        title="Countries per zone",
        description="The countries with the most requests; the rest are summed as 'other'.",
    )
    top_status_codes: int = Field(
        default=10,
        ge=1,
        le=MAX_TOP_STATUSES,
        title="Status codes per zone",
        description="The status codes with the most requests; the rest are summed as 'other'.",
    )
    timeout_seconds: int = Field(
        default=10, ge=1, le=MAX_TIMEOUT_SECONDS, title="Timeout per request (seconds)"
    )

    @field_validator("account_id")
    @classmethod
    def _check_account_id(cls, value: str) -> str:
        value = value.strip().lower()
        if not _HEX32_RE.fullmatch(value):
            raise ValueError("must be the 32-character hexadecimal account ID")
        return value

    @field_validator("api_token")
    @classmethod
    def _check_token(cls, value: SecretStr) -> SecretStr:
        # Never echo the value. Printable ASCII without spaces also keeps it from
        # breaking out of the Authorization header.
        if not _TOKEN_RE.fullmatch(value.get_secret_value()):
            raise ValueError(
                "the API token must be printable ASCII without spaces, at most 256 characters"
            )
        return value


# --- requests --------------------------------------------------------------------------


def _client(ctx: InstanceContext, cfg: CloudflareConfig) -> httpx.AsyncClient:
    # Verification stays on (the guard refuses a cloud host otherwise). The token is
    # read here, at the point of use, and goes only into this header.
    token = cfg.api_token.get_secret_value()
    return ctx.http_client(
        verify=True,
        base_url=f"https://{API_HOST}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )


async def _read_limited(response: httpx.Response) -> bytes | None:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _request(
    client: httpx.AsyncClient,
    method: Literal["GET", "POST"],
    target: str,
    timeout: float,  # noqa: ASYNC109
    content: bytes | None = None,
) -> Any:
    """Send one allowlisted request and return the parsed JSON body.

    Refuses (before sending) anything :func:`allowed_request` doesn't allow, and any
    POST whose body isn't one of our GraphQL queries. Raises :class:`_RateLimited` on
    429, :class:`_HttpStatus` on another non-200, :class:`_NotTheApi` on a body that
    isn't JSON; transport errors pass through.
    """
    if not allowed_request(method, target):
        raise GraphQLRefused("not an allowlisted Cloudflare API request")
    headers: dict[str, str] = {}
    if method == "POST":
        if content is None:
            raise GraphQLRefused("a POST needs a GraphQL body")
        _check_graphql_payload(content)
        headers["Content-Type"] = "application/json"
    elif content is not None:
        raise GraphQLRefused("a GET carries no body")
    async with (
        asyncio.timeout(timeout),
        client.stream(method, target, content=content, headers=headers) as response,
    ):
        if response.status_code == 429:
            raise _RateLimited(retry_after_seconds(response.headers.get("retry-after")))
        if response.status_code != 200:
            raise _HttpStatus(response.status_code)
        body = await _read_limited(response)
    if body is None:
        raise _NotTheApi("too large")
    try:
        if len(body) > _PARSE_IN_THREAD_BYTES:
            return await asyncio.to_thread(json.loads, body)
        return json.loads(body)
    except (ValueError, RecursionError):
        raise _NotTheApi("not JSON") from None


async def _get_result(client: httpx.AsyncClient, target: str, timeout: float) -> Any:  # noqa: ASYNC109
    """A REST GET: the ``{success, result, result_info}`` envelope, checked."""
    payload = await _request(client, "GET", target, timeout)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise _NotTheApi("not a successful envelope")
    if "result" not in payload:
        raise _NotTheApi("no result")
    return payload


def _is_rate_limit_error(errors: list[Any]) -> bool:
    """Classify GraphQL errors (their text is only looked at, never shown)."""
    for error in errors:
        if not isinstance(error, dict):
            continue
        message = error.get("message")
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        texts = [t.lower() for t in (message, code) if isinstance(t, str)]
        if any("rate limit" in t or "ratelimit" in t or "budget" in t for t in texts):
            return True
    return False


async def _graphql(
    client: httpx.AsyncClient,
    variables: Mapping[str, Any],
    timeout: float,  # noqa: ASYNC109
) -> dict[str, Any]:
    """Run :data:`ZONE_TRAFFIC_QUERY`; its ``data``. GraphQL errors raise
    :class:`_GraphQLErrors` (or :class:`_RateLimited` when they say so)."""
    body = graphql_body(ZONE_TRAFFIC_QUERY, variables)
    payload = await _request(client, "POST", PATH_GRAPHQL, timeout, content=body)
    if not isinstance(payload, dict):
        raise _NotTheApi("not an object")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        if _is_rate_limit_error(errors):
            raise _RateLimited(DEFAULT_BACKOFF_SECONDS)
        raise _GraphQLErrors()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _NotTheApi("no data")
    return data


# --- parsing ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _total(payload: Mapping[str, Any], found: int) -> int:
    info = payload.get("result_info")
    count = info.get("total_count") if isinstance(info, dict) else None
    if isinstance(count, int) and not isinstance(count, bool) and count >= found:
        return count
    return found


@dataclass(frozen=True)
class Zone:
    id: str
    name: str  # cleaned and capped; only ever on the info series
    active: bool
    paused: bool


def parse_zones(payload: Mapping[str, Any]) -> tuple[list[Zone], int]:
    """The zone list (in the API's order, by name) and the account's zone count.
    Entries without a valid zone ID are skipped."""
    result = payload.get("result")
    if not isinstance(result, list):
        raise _NotTheApi("zones is not a list")
    zones: list[Zone] = []
    seen: set[str] = set()
    for entry in result:
        if not isinstance(entry, dict):
            continue
        zone_id = entry.get("id")
        if not isinstance(zone_id, str) or not _HEX32_RE.fullmatch(zone_id) or zone_id in seen:
            continue
        seen.add(zone_id)
        zones.append(
            Zone(
                id=zone_id,
                name=clean_text(entry.get("name"), MAX_LABEL_TEXT),
                active=entry.get("status") == "active",
                paused=entry.get("paused") is True,
            )
        )
    return zones, _total(payload, len(zones))


@dataclass(frozen=True)
class Tunnel:
    id: str
    name: str  # cleaned and capped; only ever on the info series
    status: str  # one of TUNNEL_STATUSES, or "" if Cloudflare sent something else
    connections: int


def parse_tunnels(payload: Mapping[str, Any]) -> tuple[list[Tunnel], int]:
    """The tunnel list and the account's (non-deleted) tunnel count, sorted by ID."""
    result = payload.get("result")
    if not isinstance(result, list):
        raise _NotTheApi("tunnels is not a list")
    tunnels: dict[str, Tunnel] = {}
    for entry in result:
        if not isinstance(entry, dict) or entry.get("deleted_at"):
            continue
        tunnel_id = entry.get("id")
        if not isinstance(tunnel_id, str) or not _UUID_RE.fullmatch(tunnel_id):
            continue
        status = entry.get("status")
        connections = entry.get("connections")
        tunnels[tunnel_id] = Tunnel(
            id=tunnel_id,
            name=clean_text(entry.get("name"), MAX_LABEL_TEXT),
            status=status if status in TUNNEL_STATUSES else "",
            connections=len(connections) if isinstance(connections, list) else 0,
        )
    found = [tunnels[key] for key in sorted(tunnels)]
    return found, _total(payload, len(found))


@dataclass
class Traffic:
    """One zone's totals over the analytics window."""

    requests: float = 0.0
    cached_requests: float = 0.0
    bytes: float = 0.0
    cached_bytes: float = 0.0
    threats: float = 0.0
    page_views: float = 0.0


@dataclass
class ZoneAnalytics:
    traffic: Traffic
    countries: dict[str, tuple[float, float]]  # code -> (requests, threats)
    statuses: dict[str, float]  # "200", ... or OTHER -> requests


_TRAFFIC_FIELDS = (
    ("requests", "requests"),
    ("cached_requests", "cachedRequests"),
    ("bytes", "bytes"),
    ("cached_bytes", "cachedBytes"),
    ("threats", "threats"),
    ("page_views", "pageViews"),
)


def _add_groups(analytics: ZoneAnalytics, groups: list[Any]) -> None:
    for group in groups:
        summed = group.get("sum") if isinstance(group, dict) else None
        if not isinstance(summed, dict):
            continue
        for attr, key in _TRAFFIC_FIELDS:
            number = _number(summed.get(key))
            if number is not None:
                setattr(analytics.traffic, attr, getattr(analytics.traffic, attr) + number)
        countries = summed.get("countryMap")
        for entry in countries if isinstance(countries, list) else ():
            if not isinstance(entry, dict):
                continue
            code = entry.get("clientCountryName")
            key = code if isinstance(code, str) and _COUNTRY_RE.fullmatch(code) else OTHER
            requests = _number(entry.get("requests")) or 0.0
            threats = _number(entry.get("threats")) or 0.0
            before = analytics.countries.get(key, (0.0, 0.0))
            analytics.countries[key] = (before[0] + requests, before[1] + threats)
        statuses = summed.get("responseStatusMap")
        for entry in statuses if isinstance(statuses, list) else ():
            if not isinstance(entry, dict):
                continue
            status = entry.get("edgeResponseStatus")
            valid = (
                isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
            )
            key = str(status) if valid else OTHER
            requests = _number(entry.get("requests")) or 0.0
            analytics.statuses[key] = analytics.statuses.get(key, 0.0) + requests


def parse_analytics(data: Mapping[str, Any], wanted: set[str]) -> dict[str, ZoneAnalytics]:
    """``data.viewer.zones`` of :data:`ZONE_TRAFFIC_QUERY`, by zone ID. Zones we didn't
    ask for are ignored; a zone with no traffic in the window has zero totals."""
    viewer = data.get("viewer")
    zones = viewer.get("zones") if isinstance(viewer, dict) else None
    if not isinstance(zones, list):
        raise _NotTheApi("no zones in the analytics answer")
    found: dict[str, ZoneAnalytics] = {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        tag = zone.get("zoneTag")
        if not isinstance(tag, str) or tag not in wanted:
            continue
        analytics = found.setdefault(tag, ZoneAnalytics(Traffic(), {}, {}))
        groups = zone.get("httpRequests1hGroups")
        if isinstance(groups, list):
            _add_groups(analytics, groups)
    return found


def _gauge(name: str, value: float, labels: dict[str, str] | None = None) -> Sample:
    return Sample(name=name, labels=dict(labels or {}), value=value, type="gauge")


def _top(values: dict[str, float], n: int) -> tuple[list[tuple[str, float]], float]:
    """The ``n`` largest (ties by key), and the sum of the rest (``OTHER`` included)."""
    ranked = sorted(
        ((k, v) for k, v in values.items() if k != OTHER), key=lambda kv: (-kv[1], kv[0])
    )
    rest = sum(v for _, v in ranked[n:]) + values.get(OTHER, 0.0)
    return ranked[:n], rest


def zone_analytics_samples(
    zone_id: str, analytics: ZoneAnalytics, cfg: CloudflareConfig
) -> list[Sample]:
    labels = {"zone_id": zone_id}
    traffic = analytics.traffic
    samples = [
        _gauge(METRIC_ZONE_REQUESTS, traffic.requests, labels),
        _gauge(METRIC_ZONE_CACHED_REQUESTS, traffic.cached_requests, labels),
        _gauge(METRIC_ZONE_BYTES, traffic.bytes, labels),
        _gauge(METRIC_ZONE_CACHED_BYTES, traffic.cached_bytes, labels),
        _gauge(METRIC_ZONE_THREATS, traffic.threats, labels),
        _gauge(METRIC_ZONE_PAGE_VIEWS, traffic.page_views, labels),
    ]
    classes = dict.fromkeys(STATUS_CLASSES, 0.0)
    for code, requests in analytics.statuses.items():
        classes[f"{code[0]}xx" if code != OTHER else OTHER] += requests
    for status_class, requests in classes.items():
        samples.append(
            _gauge(METRIC_ZONE_BY_STATUS_CLASS, requests, {**labels, "status_class": status_class})
        )
    top_statuses, other_statuses = _top(analytics.statuses, cfg.top_status_codes)
    for code, requests in [*top_statuses, (OTHER, other_statuses)]:
        samples.append(_gauge(METRIC_ZONE_BY_STATUS, requests, {**labels, "status": code}))
    requests_by_country = {k: v[0] for k, v in analytics.countries.items()}
    top_countries, other_requests = _top(requests_by_country, cfg.top_countries)
    chosen = {code for code, _ in top_countries}
    other_threats = sum(v[1] for k, v in analytics.countries.items() if k not in chosen)
    for code, requests in [*top_countries, (OTHER, other_requests)]:
        threats = other_threats if code == OTHER else analytics.countries[code][1]
        country = {**labels, "country": code}
        samples.append(_gauge(METRIC_ZONE_BY_COUNTRY, requests, country))
        samples.append(_gauge(METRIC_ZONE_THREATS_BY_COUNTRY, threats, country))
    return samples


def zone_samples(zone: Zone) -> list[Sample]:
    labels = {"zone_id": zone.id}
    return [
        _gauge(METRIC_ZONE_ACTIVE, 1.0 if zone.active else 0.0, labels),
        _gauge(METRIC_ZONE_PAUSED, 1.0 if zone.paused else 0.0, labels),
        # The zone's name (a domain, but stored as admin text) only on the info series,
        # cleaned and capped (INTEGRATIONS.md section 9). Dashboards join it on zone_id.
        _gauge(METRIC_ZONE_INFO, 1.0, {**labels, "zone_name": zone.name}),
    ]


def tunnel_samples(tunnel: Tunnel) -> list[Sample]:
    labels = {"tunnel_id": tunnel.id}
    samples = [
        _gauge(METRIC_TUNNEL_HEALTHY, 1.0 if tunnel.status == "healthy" else 0.0, labels),
        _gauge(METRIC_TUNNEL_CONNECTIONS, float(tunnel.connections), labels),
        _gauge(METRIC_TUNNEL_INFO, 1.0, {**labels, "tunnel_name": tunnel.name}),
    ]
    for status in TUNNEL_STATUSES:
        samples.append(
            _gauge(
                METRIC_TUNNEL_STATUS,
                1.0 if tunnel.status == status else 0.0,
                {**labels, "status": status},
            )
        )
    return samples


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def analytics_window(now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """The last complete UTC hour: [start, end)."""
    end = (now or _utcnow()).astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    return end - dt.timedelta(hours=1), end


def _iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _variables(zone_ids: list[str], start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    return {"zoneTags": zone_ids, "start": _iso(start), "end": _iso(end), "limit": 1}


# --- messages --------------------------------------------------------------------------
# Fixed text plus the API host: safe to show (see PollFailure).


def _msg_auth(status: int) -> str:
    return (
        f"Cloudflare API refused the API token (HTTP {status}): check that it is active "
        "and not expired, and that it has Zone Read on this account"
    )


def _msg_tunnels(status: int) -> str:
    if status == 404:
        return "Cloudflare API answered HTTP 404 for the account's tunnels: check the account ID"
    return (
        f"the API token can't read Cloudflare Tunnels (HTTP {status}): add the Cloudflare "
        "Tunnel Read permission, or turn off 'Tunnel status'"
    )


def _msg_status(status: int) -> str:
    return f"Cloudflare API at {WHERE} answered HTTP {status}"


_MSG_BAD_ANSWER = (
    f"{WHERE} answered, but not like the Cloudflare API (is something in between, such as a proxy?)"
)
_MSG_TLS = (
    f"TLS handshake with {WHERE} failed: its certificate could not be verified (is a "
    "TLS-intercepting proxy in the way?)"
)
_MSG_ANALYTICS = (
    "the API token can't read zone analytics: add the Zone Analytics Read and Account "
    "Analytics Read permissions, or turn off 'Zone traffic analytics'"
)


@contextmanager
def _poll_failures(instance_id: uuid.UUID) -> Iterator[None]:
    """Turn request failures into PollFailures with fixed, secret-free messages. A 429
    starts the instance's backoff. Guard refusals pass through."""
    try:
        yield
    except NetGuardError:
        raise
    except _RateLimited as exc:
        _start_backoff(instance_id, exc.seconds)
        raise CloudflareRateLimited(_msg_rate_limited(exc.seconds)) from None
    except _HttpStatus as exc:
        raise CloudflareRequestFailed(_msg_status(exc.status)) from None
    except _NotTheApi:
        raise CloudflareBadAnswer(_MSG_BAD_ANSWER) from None
    except (TimeoutError, httpx.TimeoutException):
        raise CloudflareTimeout(f"timed out waiting for {WHERE}") from None
    except httpx.ConnectError as exc:
        if is_tls_failure(exc):
            raise CloudflareTlsUntrusted(_MSG_TLS) from None
        raise CloudflareUnreachable(
            f"could not connect to {WHERE} (refused or unreachable)"
        ) from None
    except httpx.HTTPError as exc:
        raise CloudflareRequestFailed(f"request to {WHERE} failed ({type(exc).__name__})") from None


async def _list_zones(client: httpx.AsyncClient, cfg: CloudflareConfig) -> tuple[list[Zone], int]:
    try:
        payload = await _get_result(client, zones_path(cfg.account_id), cfg.timeout_seconds)
    except _HttpStatus as exc:
        if exc.status in (400, 401, 403):
            raise CloudflareAuthFailed(_msg_auth(exc.status)) from None
        raise
    return parse_zones(payload)


async def _list_tunnels(
    client: httpx.AsyncClient, cfg: CloudflareConfig
) -> tuple[list[Tunnel], int]:
    try:
        payload = await _get_result(client, tunnels_path(cfg.account_id), cfg.timeout_seconds)
    except _HttpStatus as exc:
        if exc.status in (403, 404):
            raise CloudflareTunnelsForbidden(_msg_tunnels(exc.status)) from None
        raise
    return parse_tunnels(payload)


# --- the integration -------------------------------------------------------------------


class Cloudflare(Integration):
    type_id = TYPE_ID
    display_name = "Cloudflare account"
    category: ClassVar[Category] = "cloud"
    version = "0.1.0"
    config_schema = CloudflareConfig
    cloud_hosts = (API_HOST,)

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        """List zones (is the token valid, can it read zones), list tunnels (if on), and
        run the analytics query for the first zone (if on)."""
        cfg = ctx.config_as(CloudflareConfig)
        waiting = backoff_remaining(ctx.instance_id)
        if waiting:
            return TestResult(ok=False, message=_msg_waiting(waiting))
        tunnels: list[Tunnel] = []
        try:
            async with _client(ctx, cfg) as client:
                zones, zone_count = await _list_zones(client, cfg)
                if cfg.include_tunnels:
                    tunnels, _ = await _list_tunnels(client, cfg)
                if cfg.include_analytics and zones:
                    start, end = analytics_window()
                    try:
                        await _graphql(
                            client, _variables([zones[0].id], start, end), cfg.timeout_seconds
                        )
                    except (_GraphQLErrors, _HttpStatus):
                        return TestResult(ok=False, message=_MSG_ANALYTICS)
        except (CloudflareAuthFailed, CloudflareTunnelsForbidden) as exc:
            return TestResult(ok=False, message=str(exc))
        except _RateLimited as exc:
            _start_backoff(ctx.instance_id, exc.seconds)
            return TestResult(ok=False, message=_msg_rate_limited(exc.seconds))
        except _HttpStatus as exc:
            return TestResult(ok=False, message=_msg_status(exc.status))
        except _NotTheApi:
            return TestResult(ok=False, message=_MSG_BAD_ANSWER)
        except httpx.ConnectError as exc:
            if is_tls_failure(exc):
                return TestResult(ok=False, message=_MSG_TLS)
            return connection_failure(exc, where=WHERE, hint=self._hint())
        except Exception as exc:
            return connection_failure(exc, where=WHERE, hint=self._hint())
        parts = [f"{zone_count} zone{'' if zone_count == 1 else 's'}"]
        if cfg.include_tunnels:
            healthy = sum(1 for t in tunnels if t.status == "healthy")
            parts.append(
                f"{len(tunnels)} tunnel{'' if len(tunnels) == 1 else 's'} ({healthy} healthy)"
            )
        if cfg.include_analytics:
            parts.append("analytics readable" if zones else "no zone to read analytics for")
        return TestResult(ok=True, message="Cloudflare API: token accepted; " + ", ".join(parts))

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        """List zones, list tunnels (if on), then one analytics query per
        :data:`ZONES_PER_QUERY` reported zones (if on).

        A failure of the REST requests, or a timeout, raises a :class:`PollFailure`. An
        analytics query answered with GraphQL errors or an error status (other than
        429) leaves the traffic series out and sets ``analytics_up`` to 0. A 429
        anywhere starts the instance's backoff and raises
        :class:`CloudflareRateLimited`; polls during the backoff send nothing.
        """
        cfg = ctx.config_as(CloudflareConfig)
        waiting = backoff_remaining(ctx.instance_id)
        if waiting:
            raise CloudflareRateLimited(_msg_waiting(waiting))
        samples: list[Sample] = []
        tunnels: list[Tunnel] = []
        tunnel_count = 0
        analytics: dict[str, ZoneAnalytics] = {}
        analytics_ok = True
        start, end = analytics_window()
        try:
            async with asyncio.timeout(POLL_BUDGET_SECONDS):
                with _poll_failures(ctx.instance_id):
                    async with _client(ctx, cfg) as client:
                        zones, zone_count = await _list_zones(client, cfg)
                        reported = zones[: cfg.max_zones]
                        if cfg.include_tunnels:
                            tunnels, tunnel_count = await _list_tunnels(client, cfg)
                        if cfg.include_analytics:
                            ids = [zone.id for zone in reported]
                            for i in range(0, len(ids), ZONES_PER_QUERY):
                                chunk = ids[i : i + ZONES_PER_QUERY]
                                try:
                                    data = await _graphql(
                                        client, _variables(chunk, start, end), cfg.timeout_seconds
                                    )
                                    analytics.update(parse_analytics(data, set(chunk)))
                                except (_GraphQLErrors, _HttpStatus, _NotTheApi):
                                    analytics_ok = False
                                    break
        except TimeoutError:
            raise CloudflarePollTooSlow(
                f"Cloudflare poll took longer than {POLL_BUDGET_SECONDS:g} s ({WHERE} may be "
                "slow, or the account has many zones)"
            ) from None
        if not analytics_ok:
            # The instance id only: no config value, no Cloudflare text.
            logger.warning(
                "cloudflare instance %s: an analytics query failed; traffic series left out",
                ctx.instance_id,
            )
        for zone in reported:
            samples += zone_samples(zone)
            if zone.id in analytics:
                samples += zone_analytics_samples(zone.id, analytics[zone.id], cfg)
        reported_tunnels = tunnels[:MAX_TUNNELS]
        for tunnel in reported_tunnels:
            samples += tunnel_samples(tunnel)
        samples += [
            _gauge(METRIC_ZONES, float(zone_count)),
            _gauge(METRIC_ZONES_OMITTED, float(zone_count - len(reported))),
        ]
        if cfg.include_tunnels:
            samples += [
                _gauge(METRIC_TUNNELS, float(tunnel_count)),
                _gauge(METRIC_TUNNELS_OMITTED, float(tunnel_count - len(reported_tunnels))),
            ]
        if cfg.include_analytics:
            samples += [
                _gauge(METRIC_ANALYTICS_UP, 1.0 if analytics_ok else 0.0),
                _gauge(METRIC_WINDOW_END, end.timestamp()),
            ]
        return samples

    def poll_interval_seconds(self) -> int:
        return POLL_INTERVAL_SECONDS

    def cards(self) -> list[CardSpec]:
        return [
            CardSpec(card_type="cloudflare-zones", title="Cloudflare zones", default_size=(4, 3)),
            CardSpec(
                card_type="cloudflare-tunnels", title="Cloudflare tunnels", default_size=(4, 2)
            ),
        ]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint()]

    @staticmethod
    def _hint() -> FirewallHint:
        return FirewallHint(
            protocol="tcp",
            port=API_PORT,
            destination=API_HOST,
            reason="The Vantage backend calls the Cloudflare API (HTTPS) on this port.",
        )

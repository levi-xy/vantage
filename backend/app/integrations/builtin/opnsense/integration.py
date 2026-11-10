"""OPNsense: interfaces, gateways, services and resources from the REST API.

**Polled**: Vantage calls the OPNsense API on every poll through ``ctx.http_client()``,
the SSRF-guarded client, and exposes ``vantage_opnsense_*`` on its own ``/metrics``.

Read-only (Hard rule 2): only ``GET``, and only the paths in :data:`ENDPOINTS`. Every
request goes through :func:`_get_json`, which refuses any other path before it is sent.
No ``start``/``stop``/``restart``/``reconfigure``/``set``/``add``/``del``/``flush``/``kill``
endpoint, no firmware check or update and no streaming endpoint is ever called. (The
update-available flag needs a firmware check, which does work on the firewall, so it is
left out.) The API user needs read-only page privileges (``docs/integrations/opnsense.md``).

Secrets: the API key and the API secret, both top-level ``SecretStr``. They go into the
HTTP Basic ``Authorization`` header for requests to the configured host and nowhere else
(redirects are not followed). Messages name only the host and port as configured.

TLS: certificate verification is on by default. For OPNsense's own self-signed
certificate the admin pastes it (or its CA) and only that is trusted, hostname checks
included. Turning verification off is a separate, explicit, clearly labelled opt-in; the
two can't be combined (the same options as the Proxmox integration).

Partial failure: if one endpoint is refused (the API user lacks that page's privilege) or
answers something odd, the others are still reported and the gap shows in
``vantage_opnsense_endpoint_ok{endpoint=...}`` (0) and in the log. A bad key (401), an
unreachable host or a TLS failure fails the whole poll with a clear message.
"""

import asyncio
import base64
import json
import logging
import math
import re
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Self

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

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
from app.integrations.builtin.common import (
    USER_AGENT,
    checked_host,
    connection_failure,
    host_port,
    is_tls_failure,
)
from app.integrations.builtin.ifmib import MAX_LABEL_TEXT, clean_text
from app.integrations.builtin.proxmox.integration import (
    MAX_CA_PEM_CHARS,
    _cached_pinned_context,
    _normalise_pem,
)
from app.integrations.errors import PollFailure

TYPE_ID = "opnsense"
PREFIX = f"vantage_{TYPE_ID}"

METRIC_ENDPOINT_OK = f"{PREFIX}_endpoint_ok"
METRIC_INFO = f"{PREFIX}_info"
METRIC_UPTIME = f"{PREFIX}_uptime_seconds"
METRIC_LOAD = (f"{PREFIX}_load1", f"{PREFIX}_load5", f"{PREFIX}_load15")
METRIC_MEM_USED = f"{PREFIX}_memory_used_bytes"
METRIC_MEM_TOTAL = f"{PREFIX}_memory_total_bytes"
METRIC_PF_STATES = f"{PREFIX}_pf_states"
METRIC_IF_UP = f"{PREFIX}_interface_up"
METRIC_IF_RX = f"{PREFIX}_interface_receive_bytes"  # counter
METRIC_IF_TX = f"{PREFIX}_interface_transmit_bytes"  # counter
METRIC_IF_INFO = f"{PREFIX}_interface_info"
METRIC_GW_UP = f"{PREFIX}_gateway_up"
METRIC_GW_DELAY = f"{PREFIX}_gateway_delay_seconds"
METRIC_GW_STDDEV = f"{PREFIX}_gateway_stddev_seconds"
METRIC_GW_LOSS = f"{PREFIX}_gateway_loss_percent"
METRIC_SVC_RUNNING = f"{PREFIX}_service_running"
METRIC_INTERFACES = f"{PREFIX}_interfaces"
METRIC_GATEWAYS = f"{PREFIX}_gateways"
METRIC_SERVICES = f"{PREFIX}_services"
METRIC_OMITTED = f"{PREFIX}_items_omitted"  # label: kind

DEFAULT_PORT = 443
MAX_TIMEOUT_SECONDS = 15
MAX_INTERFACES = 64
MAX_GATEWAYS = 32
MAX_SERVICES = 128
MAX_BODY_BYTES = 4 * 1024 * 1024
_PARSE_IN_THREAD_BYTES = 64 * 1024
# Eight sequential GETs, each allowed up to 15 s: a poll has its own, shorter budget.
POLL_BUDGET_SECONDS = 50.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Endpoint:
    key: str  # the `endpoint` label of endpoint_ok: fixed names, never device text
    path: str
    privilege: str | None  # the OPNsense page privilege that grants it; None = not reported


ENDPOINT_GATEWAYS = Endpoint("gateways", "/api/routes/gateway/status", "System: Gateways")
ENDPOINT_SERVICES = Endpoint("services", "/api/core/service/search", "Status: Services")
ENDPOINT_IF_STATS = Endpoint(
    "interfaces", "/api/diagnostics/interface/get_interface_statistics", "Diagnostics: Netstat"
)
ENDPOINT_IF_NAMES = Endpoint(
    "interface_names", "/api/diagnostics/interface/get_interface_names", None
)
ENDPOINT_RESOURCES = Endpoint(
    "memory", "/api/diagnostics/system/system_resources", "Lobby: Dashboard"
)
ENDPOINT_TIME = Endpoint("time", "/api/diagnostics/system/system_time", "Lobby: Dashboard")
ENDPOINT_INFO = Endpoint("system", "/api/diagnostics/system/system_information", "Lobby: Dashboard")
ENDPOINT_PF = Endpoint(
    "pf", "/api/diagnostics/firewall/pf_statistics/info", "Diagnostics: Firewall statistics"
)
ENDPOINTS: tuple[Endpoint, ...] = (
    ENDPOINT_INFO,
    ENDPOINT_TIME,
    ENDPOINT_RESOURCES,
    ENDPOINT_PF,
    ENDPOINT_IF_STATS,
    ENDPOINT_IF_NAMES,
    ENDPOINT_GATEWAYS,
    ENDPOINT_SERVICES,
)
ALLOWED_PATHS: frozenset[str] = frozenset(e.path for e in ENDPOINTS)

_KEY_RE = re.compile(r"[!-9;-~]{1,256}")  # printable ASCII, no spaces, no ":" (it splits Basic)
_SECRET_RE = re.compile(r"[!-~]{1,256}")  # printable ASCII, no spaces
_IF_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}")
_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,63}")  # gateways and services
_SKIPPED_INTERFACE_PREFIXES = ("lo", "pflog", "pfsync", "enc")
_UPTIME_RE = re.compile(r"(?:(\d+)\s+days?,\s*)?(\d+):(\d{1,2}):(\d{1,2})")
_LEADING_NUMBER_RE = re.compile(r"\s*(-?\d+(?:\.\d+)?)")

IFF_UP = 0x1
IFF_RUNNING = 0x40


# --- failures --------------------------------------------------------------------------


class OpnsenseAuthFailed(PollFailure):
    """HTTP 401: the API key or secret is wrong, or the key was removed."""


class OpnsenseNoAccess(PollFailure):
    """No endpoint could be read: the API user lacks every page privilege."""


class OpnsenseTlsUntrusted(PollFailure):
    """The TLS handshake failed: the certificate is not trusted, or the host doesn't match."""


class OpnsenseUnreachable(PollFailure):
    """The connection was refused or the host is unreachable."""


class OpnsenseTimeout(PollFailure):
    """A request got no answer within the configured timeout."""


class OpnsenseRequestFailed(PollFailure):
    """Any other HTTP failure (an unexpected status, a broken connection)."""


class OpnsenseBadAnswer(PollFailure):
    """The answer was not the OPNsense API's JSON (wrong port, a proxy, too large)."""


class OpnsensePollTooSlow(PollFailure):
    """The whole poll took longer than :data:`POLL_BUDGET_SECONDS`."""


class _HttpStatus(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _NotTheApi(Exception):
    """The body was not a JSON object. Internal."""


# --- config ----------------------------------------------------------------------------


class OpnsenseConfig(BaseModel):
    """What the admin enters. Both the API key and the API secret are secrets."""

    host: str = Field(
        title="Host",
        description=(
            "IP address or DNS name of the OPNsense web interface / API. Must be on a "
            "private network (RFC 1918, CGNAT or IPv6 ULA). Stored in plaintext and "
            "shown to every user."
        ),
        max_length=253,
        examples=["10.0.0.1"],
    )
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535, title="API port (HTTPS)")
    api_key: SecretStr = Field(
        title="API key",
        description=(
            "The key of a read-only API user (System > Access > Users > API keys). "
            "Write-only: never shown again."
        ),
    )
    api_secret: SecretStr = Field(
        title="API secret",
        description="The secret OPNsense shows once, in the downloaded key file.",
    )
    tls_ca_certificate: str | None = Field(
        default=None,
        title="Trusted certificate (PEM)",
        description=(
            "For OPNsense's own self-signed certificate: paste that certificate (or its "
            "CA). Only it is then trusted, and the host must still match it. Leave empty "
            "if the firewall has a certificate from a public or system-trusted CA. Never "
            "paste a private key here: this field is stored in plaintext."
        ),
        max_length=MAX_CA_PEM_CHARS,
    )
    tls_insecure_skip_verify: bool = Field(
        default=False,
        title="INSECURE: don't verify the TLS certificate",
        description=(
            "Not recommended. Sends the API key and secret to whatever answers on this "
            "address, without checking who it is. Use the trusted certificate field "
            "instead; turn this on only as a last resort, on a network you control."
        ),
    )
    timeout_seconds: int = Field(
        default=10, ge=1, le=MAX_TIMEOUT_SECONDS, title="Timeout per request (seconds)"
    )

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return checked_host(value)

    @field_validator("api_key")
    @classmethod
    def _check_key(cls, value: SecretStr) -> SecretStr:
        # Never echo the value. Printable ASCII also keeps it out of header injection;
        # a colon would split the Basic credentials wrongly.
        if not _KEY_RE.fullmatch(value.get_secret_value()):
            raise ValueError(
                "the API key must be printable ASCII without spaces or colons, "
                "at most 256 characters"
            )
        return value

    @field_validator("api_secret")
    @classmethod
    def _check_secret(cls, value: SecretStr) -> SecretStr:
        if not _SECRET_RE.fullmatch(value.get_secret_value()):
            raise ValueError(
                "the API secret must be printable ASCII without spaces, at most 256 characters"
            )
        return value

    @field_validator("tls_ca_certificate")
    @classmethod
    def _check_ca(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _normalise_pem(value)

    @model_validator(mode="after")
    def _one_tls_choice(self) -> Self:
        if self.tls_ca_certificate is not None and self.tls_insecure_skip_verify:
            raise ValueError(
                "choose either a trusted certificate or not verifying the certificate, not both"
            )
        return self

    def where(self) -> str:
        return host_port(self.host, self.port)

    def tls_verify(self) -> bool | ssl.SSLContext:
        if self.tls_insecure_skip_verify:
            return False
        if self.tls_ca_certificate is None:
            return True
        return _cached_pinned_context(self.tls_ca_certificate)


# --- requests --------------------------------------------------------------------------


def allowed_path(target: str) -> bool:
    return target in ALLOWED_PATHS


def _client(ctx: InstanceContext, cfg: OpnsenseConfig) -> httpx.AsyncClient:
    # Read at the point of use; goes only into this header, to the configured host.
    pair = f"{cfg.api_key.get_secret_value()}:{cfg.api_secret.get_secret_value()}"
    token = base64.b64encode(pair.encode("ascii")).decode("ascii")
    return ctx.http_client(
        verify=cfg.tls_verify(),
        base_url=f"https://{cfg.where()}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
        },
    )


async def _read_limited(response: httpx.Response) -> bytes | None:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _get_json(client: httpx.AsyncClient, path: str, timeout: float) -> dict[str, Any]:  # noqa: ASYNC109
    """GET an allowlisted path and return its JSON object.

    Raises :class:`_HttpStatus` for a non-200 answer, :class:`_NotTheApi` for a body that
    isn't a JSON object, and lets transport errors through.
    """
    if not allowed_path(path):
        raise ValueError("not an allowlisted OPNsense API path")
    async with asyncio.timeout(timeout), client.stream("GET", path) as response:
        if response.status_code != 200:
            raise _HttpStatus(response.status_code)
        body = await _read_limited(response)
    if body is None:
        raise _NotTheApi("too large")
    try:
        if len(body) > _PARSE_IN_THREAD_BYTES:
            payload = await asyncio.to_thread(json.loads, body)
        else:
            payload = json.loads(body)
    except (ValueError, RecursionError):
        raise _NotTheApi("not JSON") from None
    if not isinstance(payload, dict):
        raise _NotTheApi("not an object")
    return payload


@dataclass
class Fetched:
    """What each endpoint gave: its JSON object, or ``None`` with the status/problem."""

    data: dict[str, dict[str, Any]]
    failed: dict[str, int | None]  # endpoint key -> HTTP status (None = not the API)

    def get(self, endpoint: Endpoint) -> dict[str, Any] | None:
        return self.data.get(endpoint.key)


async def _fetch_all(client: httpx.AsyncClient, timeout: float) -> Fetched:  # noqa: ASYNC109
    """Every endpoint, once. A 401 raises (the credentials are bad); any other error status
    or odd body only leaves that endpoint out. Transport errors raise."""
    fetched = Fetched(data={}, failed={})
    for endpoint in ENDPOINTS:
        try:
            fetched.data[endpoint.key] = await _get_json(client, endpoint.path, timeout)
        except _HttpStatus as exc:
            if exc.status == 401:
                raise
            fetched.failed[endpoint.key] = exc.status
        except _NotTheApi:
            fetched.failed[endpoint.key] = None
    return fetched


# --- parsing ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """A finite, non-negative number; strings may carry a unit ("1.2 ms", "0.0 %")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        match = _LEADING_NUMBER_RE.match(value)
        if match is None:
            return None
        value = float(match.group(1))
    if not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _sample(
    samples: list[Sample],
    name: str,
    value: float | None,
    labels: dict[str, str] | None = None,
    kind: Literal["gauge", "counter"] = "gauge",
) -> None:
    if value is not None:
        samples.append(Sample(name=name, labels=dict(labels or {}), value=value, type=kind))


def _flag(samples: list[Sample], name: str, value: bool, labels: dict[str, str]) -> None:
    samples.append(
        Sample(name=name, labels=dict(labels), value=1.0 if value else 0.0, type="gauge")
    )


def _rows(payload: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    rows = payload.get(key) if payload else None
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def system_samples(
    info: dict[str, Any] | None,
    clock: dict[str, Any] | None,
    resources: dict[str, Any] | None,
    pf: dict[str, Any] | None,
) -> list[Sample]:
    samples: list[Sample] = []
    versions = info.get("versions") if info else None
    if isinstance(versions, list) and versions and isinstance(versions[0], str):
        version = clean_text(versions[0], 64)
        if version:
            samples.append(
                Sample(name=METRIC_INFO, labels={"version": version}, value=1.0, type="gauge")
            )
    if clock:
        _sample(samples, METRIC_UPTIME, parse_uptime(clock.get("uptime")))
        loads = clock.get("loadavg")
        if isinstance(loads, str):
            parts = [part.strip() for part in loads.split(",")]
            if len(parts) == len(METRIC_LOAD):
                for metric, part in zip(METRIC_LOAD, parts, strict=True):
                    _sample(samples, metric, _number(part))
    memory = resources.get("memory") if resources else None
    if isinstance(memory, dict):
        _sample(samples, METRIC_MEM_USED, _memory_bytes(memory, "used"))
        _sample(samples, METRIC_MEM_TOTAL, _memory_bytes(memory, "total"))
    _sample(samples, METRIC_PF_STATES, pf_state_count(pf))
    return samples


def _memory_bytes(memory: dict[str, Any], key: str) -> float | None:
    """``total``/``used`` are bytes; ``*_frmt`` is the same in MiB (a fallback)."""
    value = _number(memory.get(key))
    if value is not None:
        return value
    formatted = _number(memory.get(f"{key}_frmt"))
    return None if formatted is None else formatted * 1024 * 1024


def parse_uptime(raw: Any) -> float | None:
    """OPNsense formats uptime as "3 days, 04:05:06" or "04:05:06"."""
    if not isinstance(raw, str):
        return None
    match = _UPTIME_RE.search(raw[:64])
    if match is None:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return float(((days * 24 + hours) * 60 + minutes) * 60 + seconds)


def pf_state_count(payload: dict[str, Any] | None, _depth: int = 0) -> float | None:
    """The state-table entry count inside ``pfctl -s info``'s parsed output."""
    if not payload or _depth > 4:
        return None
    table = payload.get("state-table")
    if isinstance(table, dict):
        for key in ("current-entries", "entries", "current entries"):
            count = _number(table.get(key))
            if count is not None:
                return count
    for value in payload.values():
        if isinstance(value, dict):
            found = pf_state_count(value, _depth + 1)
            if found is not None:
                return found
    return None


def interface_samples(
    stats: dict[str, Any] | None, names: dict[str, Any] | None
) -> tuple[list[Sample], int]:
    """Per-interface samples (at most :data:`MAX_INTERFACES`) and how many were left out."""
    entries: dict[str, dict[str, Any]] = {}
    statistics = stats.get("statistics") if stats else None
    if isinstance(statistics, dict):
        for entry in statistics.values():
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not _IF_NAME_RE.fullmatch(name):
                continue
            if name.startswith(_SKIPPED_INTERFACE_PREFIXES) and not name[-1:].isalpha():
                continue
            entries[name] = entry
    ordered = sorted(entries)
    samples: list[Sample] = []
    for name in ordered[:MAX_INTERFACES]:
        entry = entries[name]
        labels = {"interface": name}
        flags = _flags(entry.get("flags"))
        if flags is not None:
            _flag(samples, METRIC_IF_UP, bool(flags & IFF_UP and flags & IFF_RUNNING), labels)
        _sample(samples, METRIC_IF_RX, _number(entry.get("received-bytes")), labels, "counter")
        _sample(samples, METRIC_IF_TX, _number(entry.get("sent-bytes")), labels, "counter")
        description = names.get(name) if names else None
        samples.append(
            Sample(
                name=METRIC_IF_INFO,
                labels={**labels, "interface_name": clean_text(description, MAX_LABEL_TEXT)},
                value=1.0,
                type="gauge",
            )
        )
    samples.append(Sample(name=METRIC_INTERFACES, value=float(len(ordered)), type="gauge"))
    return samples, max(0, len(ordered) - MAX_INTERFACES)


def _flags(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip(), 0)
        except ValueError:
            return None
    return None


def gateway_samples(payload: dict[str, Any] | None) -> tuple[list[Sample], int]:
    gateways: dict[str, dict[str, Any]] = {}
    for row in _rows(payload, "items"):
        name = row.get("name")
        if isinstance(name, str) and _NAME_RE.fullmatch(name):
            gateways[name] = row
    ordered = sorted(gateways)
    samples: list[Sample] = []
    for name in ordered[:MAX_GATEWAYS]:
        row = gateways[name]
        labels = {"gateway": name}
        status = row.get("status")
        if isinstance(status, str):
            # "none" = online; "loss" / "delay" are degraded but passing traffic.
            _flag(samples, METRIC_GW_UP, status.lower() not in ("down", "force_down"), labels)
        delay = _number(row.get("delay"))  # milliseconds, e.g. "1.2 ms"
        _sample(samples, METRIC_GW_DELAY, None if delay is None else delay / 1000, labels)
        stddev = _number(row.get("stddev"))
        _sample(samples, METRIC_GW_STDDEV, None if stddev is None else stddev / 1000, labels)
        _sample(samples, METRIC_GW_LOSS, _number(row.get("loss")), labels)
    samples.append(Sample(name=METRIC_GATEWAYS, value=float(len(ordered)), type="gauge"))
    return samples, max(0, len(ordered) - MAX_GATEWAYS)


def service_samples(payload: dict[str, Any] | None) -> tuple[list[Sample], int]:
    services: dict[str, dict[str, Any]] = {}
    for row in _rows(payload, "rows"):
        name = row.get("id") if isinstance(row.get("id"), str) else row.get("name")
        if isinstance(name, str) and _NAME_RE.fullmatch(name):
            services[name] = row
    ordered = sorted(services)
    samples: list[Sample] = []
    for name in ordered[:MAX_SERVICES]:
        running = services[name].get("running")
        if isinstance(running, bool | int):
            _flag(samples, METRIC_SVC_RUNNING, bool(running), {"service": name})
    samples.append(Sample(name=METRIC_SERVICES, value=float(len(ordered)), type="gauge"))
    return samples, max(0, len(ordered) - MAX_SERVICES)


def build_samples(fetched: Fetched) -> list[Sample]:
    samples = system_samples(
        fetched.get(ENDPOINT_INFO),
        fetched.get(ENDPOINT_TIME),
        fetched.get(ENDPOINT_RESOURCES),
        fetched.get(ENDPOINT_PF),
    )
    parts = (
        (
            "interfaces",
            interface_samples(fetched.get(ENDPOINT_IF_STATS), fetched.get(ENDPOINT_IF_NAMES)),
        ),
        ("gateways", gateway_samples(fetched.get(ENDPOINT_GATEWAYS))),
        ("services", service_samples(fetched.get(ENDPOINT_SERVICES))),
    )
    for kind, (part, omitted) in parts:
        samples += part
        samples.append(
            Sample(name=METRIC_OMITTED, labels={"kind": kind}, value=float(omitted), type="gauge")
        )
    for endpoint in ENDPOINTS:
        _flag(samples, METRIC_ENDPOINT_OK, endpoint.key in fetched.data, {"endpoint": endpoint.key})
    return samples


# --- messages --------------------------------------------------------------------------
# Fixed text plus the host:port as configured: safe to show (see PollFailure).


def _msg_auth(where: str) -> str:
    return (
        f"OPNsense API at {where} refused the API key (HTTP 401): check the key and secret, "
        "and that the API key hasn't been removed"
    )


def _msg_tls(where: str) -> str:
    return (
        f"certificate not trusted: the TLS certificate of {where} could not be verified. "
        "Paste the firewall's certificate into 'Trusted certificate', and make sure the "
        "host is a name or address the certificate covers"
    )


def _msg_bad_answer(where: str) -> str:
    return (
        f"{where} answered, but not like the OPNsense API (check the port, and that "
        "nothing sits in between)"
    )


def _msg_timeout(where: str) -> str:
    return f"timed out waiting for {where}"


def _msg_unreachable(where: str) -> str:
    return f"could not connect to {where} (refused or unreachable)"


def _msg_no_access(where: str) -> str:
    return (
        f"the API user can't read any page on {where} (HTTP 403): grant it the read-only "
        "privileges listed in the OPNsense integration guide"
    )


def _msg_missing(fetched: Fetched) -> str:
    """Which page privileges the key lacks (names are fixed text, not device text)."""
    missing = sorted(
        {
            endpoint.privilege
            for endpoint in ENDPOINTS
            if endpoint.privilege and fetched.failed.get(endpoint.key) == 403
        }
    )
    return ", ".join(missing)


def _ascii(text: str, limit: int) -> str:
    cleaned = "".join(c for c in text if c.isascii() and c.isprintable())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


@contextmanager
def _poll_failures(where: str) -> Iterator[None]:
    """Turn request failures into PollFailures with fixed, secret-free messages."""
    try:
        yield
    except NetGuardError:
        raise
    except _HttpStatus as exc:
        if exc.status == 401:
            raise OpnsenseAuthFailed(_msg_auth(where)) from None
        raise OpnsenseRequestFailed(f"OPNsense API at {where} answered HTTP {exc.status}") from None
    except _NotTheApi:
        raise OpnsenseBadAnswer(_msg_bad_answer(where)) from None
    except (TimeoutError, httpx.TimeoutException):
        raise OpnsenseTimeout(_msg_timeout(where)) from None
    except httpx.ConnectError as exc:
        if is_tls_failure(exc):
            raise OpnsenseTlsUntrusted(_msg_tls(where)) from None
        raise OpnsenseUnreachable(_msg_unreachable(where)) from None
    except httpx.HTTPError as exc:
        raise OpnsenseRequestFailed(f"request to {where} failed ({type(exc).__name__})") from None


def _nothing_readable(where: str, fetched: Fetched) -> PollFailure:
    if any(status == 403 for status in fetched.failed.values()):
        return OpnsenseNoAccess(_msg_no_access(where))
    return OpnsenseBadAnswer(_msg_bad_answer(where))


# --- the integration -------------------------------------------------------------------


class Opnsense(Integration):
    type_id = TYPE_ID
    display_name = "OPNsense"
    category: ClassVar[Category] = "firewall"
    version = "0.1.0"
    config_schema = OpnsenseConfig

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        """Reads every endpoint once. Says which pages the API user can't read."""
        cfg = ctx.config_as(OpnsenseConfig)
        where = cfg.where()
        try:
            async with _client(ctx, cfg) as client:
                fetched = await _fetch_all(client, float(cfg.timeout_seconds))
        except _HttpStatus:
            return TestResult(ok=False, message=_msg_auth(where))
        except httpx.ConnectError as exc:
            if is_tls_failure(exc):
                return TestResult(ok=False, message=_msg_tls(where))
            return connection_failure(exc, where=where, hint=self._hint(cfg))
        except Exception as exc:
            return connection_failure(exc, where=where, hint=self._hint(cfg))
        if not fetched.data:
            error = _nothing_readable(where, fetched)
            return TestResult(ok=False, message=str(error))
        info = fetched.get(ENDPOINT_INFO)
        versions = info.get("versions") if info else None
        label = "OPNsense"
        if isinstance(versions, list) and versions and isinstance(versions[0], str):
            label = _ascii(versions[0], 48) or label
        message = f"{label}: read {len(fetched.data)} of {len(ENDPOINTS)} API endpoints"
        missing = _msg_missing(fetched)
        if missing:
            message += f". The API user lacks these privileges: {missing}"
        return TestResult(ok=True, message=message)

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        cfg = ctx.config_as(OpnsenseConfig)
        where = cfg.where()
        try:
            async with asyncio.timeout(POLL_BUDGET_SECONDS):
                with _poll_failures(where):
                    async with _client(ctx, cfg) as client:
                        fetched = await _fetch_all(client, float(cfg.timeout_seconds))
        except TimeoutError:
            raise OpnsensePollTooSlow(
                f"OPNsense poll of {where} took longer than {POLL_BUDGET_SECONDS:g} s "
                "(the firewall may be busy)"
            ) from None
        if not fetched.data:
            raise _nothing_readable(where, fetched)
        if fetched.failed:
            # Endpoint keys and status codes only: no config value, no device text.
            logger.warning(
                "opnsense instance %s: endpoints left out: %s",
                ctx.instance_id,
                ", ".join(f"{k}={v}" for k, v in sorted(fetched.failed.items())),
            )
        return build_samples(fetched)

    def poll_interval_seconds(self) -> int:
        return 60

    def cards(self) -> list[CardSpec]:
        return [
            CardSpec(card_type="opnsense-overview", title="OPNsense", default_size=(4, 4)),
            CardSpec(card_type="opnsense-interfaces", title="Interfaces", default_size=(4, 3)),
            CardSpec(card_type="opnsense-gateways", title="Gateways", default_size=(4, 3)),
            CardSpec(card_type="opnsense-services", title="Services", default_size=(4, 3)),
        ]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint(ctx.config_as(OpnsenseConfig))]

    @staticmethod
    def _hint(cfg: OpnsenseConfig) -> FirewallHint:
        return FirewallHint(
            protocol="tcp",
            port=cfg.port,
            destination=cfg.host,
            reason="The Vantage backend calls the OPNsense API (HTTPS) on this port.",
        )

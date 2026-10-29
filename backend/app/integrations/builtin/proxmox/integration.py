"""Proxmox VE: nodes, guests (QEMU and LXC) and storage pools from the REST API.

**Polled**: Vantage calls the Proxmox VE API on every poll through ``ctx.http_client()``,
the SSRF-guarded client, and exposes ``vantage_proxmox_*`` on its own ``/metrics``.

Read-only (Hard rule 2): only ``GET``, and only the paths in :data:`ALLOWED_PATHS`.
Every request goes through :func:`_get_json`, which refuses any other path before it is
sent. No console, VNC, exec, task or configuration endpoint is ever called. The API
token should have the ``PVEAuditor`` role only (``docs/integrations/proxmox.md``).

Secret: the API token secret, a top-level ``SecretStr``. It goes into the
``Authorization`` header for requests to the configured host and nowhere else
(redirects are not followed). Messages name only the host and port as configured.

TLS: certificate verification is on by default. For Proxmox's own self-signed
certificate the admin pastes the cluster CA (or the node's certificate) and only that
certificate is trusted, hostname checks included. Turning verification off is a
separate, explicit, clearly labelled opt-in; the two can't be combined.
"""

import asyncio
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
from app.integrations.errors import PollFailure

TYPE_ID = "proxmox"
PREFIX = f"vantage_{TYPE_ID}"

# Metric names: vantage_<type_id>_<metric> (Contract 1).
METRIC_NODES = f"{PREFIX}_nodes"
METRIC_NODES_OMITTED = f"{PREFIX}_nodes_omitted"
METRIC_GUESTS = f"{PREFIX}_guests"
METRIC_GUESTS_OMITTED = f"{PREFIX}_guests_omitted"
METRIC_STORAGES = f"{PREFIX}_storages"
METRIC_STORAGES_OMITTED = f"{PREFIX}_storages_omitted"

METRIC_NODE_UP = f"{PREFIX}_node_up"
METRIC_NODE_CPU = f"{PREFIX}_node_cpu_ratio"
METRIC_NODE_CPUS = f"{PREFIX}_node_cpus"
METRIC_NODE_MEM_USED = f"{PREFIX}_node_memory_used_bytes"
METRIC_NODE_MEM_TOTAL = f"{PREFIX}_node_memory_total_bytes"
METRIC_NODE_ROOT_USED = f"{PREFIX}_node_root_disk_used_bytes"
METRIC_NODE_ROOT_TOTAL = f"{PREFIX}_node_root_disk_total_bytes"
METRIC_NODE_UPTIME = f"{PREFIX}_node_uptime_seconds"
METRIC_NODE_LOAD = (f"{PREFIX}_node_load1", f"{PREFIX}_node_load5", f"{PREFIX}_node_load15")

METRIC_GUEST_RUNNING = f"{PREFIX}_guest_running"
METRIC_GUEST_CPU = f"{PREFIX}_guest_cpu_ratio"
METRIC_GUEST_CPUS = f"{PREFIX}_guest_cpus"
METRIC_GUEST_MEM_USED = f"{PREFIX}_guest_memory_used_bytes"
METRIC_GUEST_MEM_MAX = f"{PREFIX}_guest_memory_max_bytes"
METRIC_GUEST_DISK_USED = f"{PREFIX}_guest_disk_used_bytes"
METRIC_GUEST_DISK_MAX = f"{PREFIX}_guest_disk_max_bytes"
METRIC_GUEST_NET_IN = f"{PREFIX}_guest_network_receive_bytes"  # counter
METRIC_GUEST_NET_OUT = f"{PREFIX}_guest_network_transmit_bytes"  # counter
METRIC_GUEST_DISK_READ = f"{PREFIX}_guest_disk_read_bytes"  # counter
METRIC_GUEST_DISK_WRITE = f"{PREFIX}_guest_disk_written_bytes"  # counter
METRIC_GUEST_UPTIME = f"{PREFIX}_guest_uptime_seconds"
# Value 1; the only series that carries the guest's name (label exception, section 9).
METRIC_GUEST_INFO = f"{PREFIX}_guest_info"

METRIC_STORAGE_ACTIVE = f"{PREFIX}_storage_active"
METRIC_STORAGE_USED = f"{PREFIX}_storage_used_bytes"
METRIC_STORAGE_TOTAL = f"{PREFIX}_storage_total_bytes"

DEFAULT_PORT = 8006
# The guarded client stops any request after 15 s in total.
MAX_TIMEOUT_SECONDS = 15
# Proxmox supports clusters of up to a few dozen nodes; more than this is not a PVE answer.
MAX_NODES = 32
# Per-poll caps on what is reported, so one instance stays well inside the collector's
# 10 000 samples per poll: 12 per guest, 3 per storage, 10 per node.
MAX_GUESTS = 500
MAX_STORAGES = 200
# Real answers are tens to hundreds of KB (a few hundred bytes per guest).
MAX_BODY_BYTES = 4 * 1024 * 1024
# Bodies larger than this are parsed in a worker thread, off the event loop.
_PARSE_IN_THREAD_BYTES = 64 * 1024
MAX_CA_CERTIFICATES = 8
# A PVE CA is about 2 KB of PEM; 16 KiB holds several, and bounds the parse.
MAX_CA_PEM_CHARS = 16_384

# A poll is two cluster-wide GETs plus two per online node, each allowed up to 15 s:
# far past the collector's cut-off of min(interval, 60) s for a cluster. So a poll has
# its own, shorter budget and fails with a clear message when it runs out, instead of
# the collector's generic "poll timed out" (the same approach as snmp_device).
POLL_BUDGET_SECONDS = 50.0

API_ROOT = "/api2/json"
PATH_VERSION = f"{API_ROOT}/version"
PATH_NODES = f"{API_ROOT}/nodes"
PATH_GUESTS = f"{API_ROOT}/cluster/resources?type=vm"

# Proxmox node names are host names; storage ids follow PVE's pve-storage-id format.
# Both are validated before they become a label or part of a request path.
_NODE_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_NODE_RE = re.compile(_NODE_NAME)
_STORAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")
MAX_VMID = 999_999_999

# Every request the integration may send: GET, exactly these paths (with this query).
ALLOWED_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(re.escape(PATH_VERSION)),
    re.compile(re.escape(PATH_NODES)),
    re.compile(re.escape(PATH_GUESTS)),
    re.compile(rf"{re.escape(PATH_NODES)}/{_NODE_NAME}/(?:status|storage)"),
)

# user@realm!tokenname, as Proxmox shows it when the token is created.
_TOKEN_ID_RE = re.compile(
    # printable ASCII only (it goes into a header), without the separators : / @ ! =
    r"(?:(?![:/@!=])[!-~]){1,64}@[A-Za-z][A-Za-z0-9._-]{0,63}![A-Za-z][A-Za-z0-9._-]{0,63}"
)
_TOKEN_SECRET_RE = re.compile(r"[!-~]{1,256}")  # printable ASCII, no spaces
# One line of a PEM body. A single character class, matched with fullmatch on a line
# that is already stripped: linear time, nothing to backtrack over.
_BASE64_LINE_RE = re.compile(r"[A-Za-z0-9+/=]+")
_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"

logger = logging.getLogger(__name__)


# --- failures --------------------------------------------------------------------------


class ProxmoxAuthFailed(PollFailure):
    """HTTP 401: the token id or secret is wrong, or the token expired or was removed."""


class ProxmoxForbidden(PollFailure):
    """HTTP 403: the token is valid but lacks the permission for a required path."""


class ProxmoxTlsUntrusted(PollFailure):
    """The TLS handshake failed: the certificate is not trusted, or the host doesn't match."""


class ProxmoxUnreachable(PollFailure):
    """The connection was refused or the host is unreachable."""


class ProxmoxTimeout(PollFailure):
    """A request got no answer within the configured timeout."""


class ProxmoxRequestFailed(PollFailure):
    """Any other HTTP failure (an unexpected status, a broken connection)."""


class ProxmoxBadAnswer(PollFailure):
    """The answer was not the Proxmox VE API's JSON (wrong port, a proxy, too large)."""


class ProxmoxPollTooSlow(PollFailure):
    """The whole poll took longer than :data:`POLL_BUDGET_SECONDS`."""


class _HttpStatus(Exception):
    """A non-200 answer. Internal: callers turn it into a message they write."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _NotTheApi(Exception):
    """The body was not the Proxmox API's ``{"data": ...}`` JSON. Internal."""


# --- config ----------------------------------------------------------------------------


class ProxmoxConfig(BaseModel):
    """What the admin enters. The JSON schema of this model drives the UI form.

    ``host`` is found by name by the SSRF check on save, resolved with ``port``. The
    token secret is the only secret; the token id and the CA certificate are plaintext
    config, shown to every user (neither is a credential).
    """

    host: str = Field(
        title="Host",
        description=(
            "IP address or DNS name of a Proxmox VE node. In a cluster any node will do: "
            "it answers for the whole cluster. Must be on a private network (RFC 1918, "
            "CGNAT or IPv6 ULA). Stored in plaintext and shown to every user."
        ),
        max_length=253,
        examples=["10.0.0.50"],
    )
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535, title="API port (HTTPS)")
    token_id: str = Field(
        title="API token ID",
        description=(
            "The full token ID, user@realm!tokenname, as Proxmox shows it when the token "
            "is created. Give the token the PVEAuditor role only (read-only). Stored in "
            "plaintext."
        ),
        max_length=200,
        examples=["vantage@pve!monitoring"],
    )
    token_secret: SecretStr = Field(
        title="API token secret",
        description="The secret Proxmox shows once, when the token is created.",
    )
    tls_ca_certificate: str | None = Field(
        default=None,
        title="Trusted certificate (PEM)",
        description=(
            "For Proxmox's own self-signed certificate: paste the cluster CA "
            "(/etc/pve/pve-root-ca.pem on any node), or the node's certificate. Only this "
            "certificate is then trusted, and the host must still match it. Leave empty "
            "if the node has a certificate from a public or system-trusted CA. Never "
            "paste a private key here: this field is stored in plaintext."
        ),
        max_length=MAX_CA_PEM_CHARS,
    )
    tls_insecure_skip_verify: bool = Field(
        default=False,
        title="INSECURE: don't verify the TLS certificate",
        description=(
            "Not recommended. Sends the API token to whatever answers on this address, "
            "without checking who it is. Use the trusted certificate field instead; turn "
            "this on only as a last resort, on a network you control."
        ),
    )
    timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=MAX_TIMEOUT_SECONDS,
        title="Timeout per request (seconds)",
    )
    max_guests: int = Field(
        default=250,
        ge=1,
        le=MAX_GUESTS,
        title="Guests per poll (at most)",
        description=(
            "The guests (VMs and containers) with the lowest IDs are reported, up to "
            "this many, so a large cluster can't produce unbounded metrics."
        ),
    )
    max_storages: int = Field(
        default=100,
        ge=1,
        le=MAX_STORAGES,
        title="Storage pools per poll (at most)",
        description="Counted per node: a shared storage seen by three nodes counts three times.",
    )

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return checked_host(value)

    @field_validator("token_id")
    @classmethod
    def _check_token_id(cls, value: str) -> str:
        value = value.strip()
        if not _TOKEN_ID_RE.fullmatch(value):
            raise ValueError("must look like user@realm!tokenname")
        return value

    @field_validator("token_secret")
    @classmethod
    def _check_token_secret(cls, value: SecretStr) -> SecretStr:
        # Never echo the value. Printable ASCII without spaces also keeps it from
        # breaking out of the Authorization header.
        if not _TOKEN_SECRET_RE.fullmatch(value.get_secret_value()):
            raise ValueError(
                "the token secret must be printable ASCII without spaces, at most 256 characters"
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
        """What the guarded client verifies the server certificate against."""
        if self.tls_insecure_skip_verify:
            return False
        if self.tls_ca_certificate is None:
            return True
        return pinned_context(self.tls_ca_certificate)


def _normalise_pem(value: str) -> str:
    """One or more PEM certificates, nothing else; refused (without echoing) otherwise."""
    blocks = _pem_blocks(value)
    pem = "\n".join(
        f"{_PEM_BEGIN}\n" + "\n".join(_wrap(block)) + f"\n{_PEM_END}" for block in blocks
    )
    try:
        pinned_context(pem)
    except (ssl.SSLError, ValueError):
        raise ValueError("is not a valid PEM certificate") from None
    return pem + "\n"


def _pem_blocks(value: str) -> list[str]:
    """The base64 body of each certificate, parsed line by line (no regex over the
    whole text, so no backtracking). Refuses anything that isn't certificates, so a
    pasted private key never lands in the plaintext config."""
    if len(value) > MAX_CA_PEM_CHARS:  # the field's max_length already says so
        raise ValueError(f"at most {MAX_CA_PEM_CHARS} characters")
    only_certs = "must contain only PEM certificates, nothing else (never a private key)"
    blocks: list[str] = []
    current: list[str] | None = None
    for raw in value.splitlines():
        line = raw.strip()
        if current is None:
            if not line:
                continue
            if line != _PEM_BEGIN:
                raise ValueError(only_certs)
            if len(blocks) == MAX_CA_CERTIFICATES:
                raise ValueError(f"at most {MAX_CA_CERTIFICATES} certificates")
            current = []
        elif line == _PEM_END:
            if not current:
                raise ValueError("is not a valid PEM certificate")
            blocks.append("".join(current))
            current = None
        elif line and _BASE64_LINE_RE.fullmatch(line):
            current.append(line)
        else:
            raise ValueError("is not a valid PEM certificate")
    if current is not None:
        raise ValueError("is not a valid PEM certificate (no END CERTIFICATE line)")
    if not blocks:
        raise ValueError("must be one or more PEM certificates (-----BEGIN CERTIFICATE-----)")
    return blocks


def _wrap(base64: str) -> list[str]:
    return [base64[i : i + 64] for i in range(0, len(base64), 64)]


def pinned_context(pem: str) -> ssl.SSLContext:
    """A context that trusts exactly the given certificates, with hostname checks on.

    ``create_default_context(cadata=...)`` loads only these certificates, not the
    system store. ``VERIFY_X509_PARTIAL_CHAIN`` lets a pasted node certificate (not a
    CA) act as the trust anchor, so pinning the node's own certificate works too.
    TLS 1.2 is the floor (PVE 8 offers 1.2 and 1.3).
    """
    context = ssl.create_default_context(cadata=pem)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return context


# --- requests --------------------------------------------------------------------------


def allowed_path(target: str) -> bool:
    """Whether ``target`` (path and query) is one the integration may request."""
    return any(pattern.fullmatch(target) for pattern in ALLOWED_PATHS)


def node_path(node: str, what: str) -> str:
    """``/nodes/{node}/{what}``, for a node name already checked by :data:`_NODE_RE`."""
    return f"{PATH_NODES}/{node}/{what}"


def _client(ctx: InstanceContext, cfg: ProxmoxConfig) -> httpx.AsyncClient:
    # The secret is read here, at the point of use, and goes only into this header, to
    # the configured host. Redirects are not followed, so it can't follow one elsewhere.
    secret = cfg.token_secret.get_secret_value()
    return ctx.http_client(
        verify=cfg.tls_verify(),
        base_url=f"https://{cfg.where()}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"PVEAPIToken={cfg.token_id}={secret}",
        },
    )


async def _read_limited(response: httpx.Response) -> bytes | None:
    """The body, or ``None`` if it's larger than :data:`MAX_BODY_BYTES`."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _get_json(client: httpx.AsyncClient, path: str, timeout: float) -> Any:  # noqa: ASYNC109
    """GET an allowlisted path and return its ``data``.

    Raises :class:`_HttpStatus` for a non-200 answer, :class:`_NotTheApi` for a body
    that isn't the API's JSON, and lets transport errors through.
    """
    if not allowed_path(path):
        # Defence in depth: every caller builds its path from the constants above.
        raise ValueError("not an allowlisted Proxmox API path")
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
    if not isinstance(payload, dict) or "data" not in payload:
        raise _NotTheApi("no data")
    return payload["data"]


# --- parsing ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """A finite, non-negative number from the API (Proxmox sends loadavg as strings)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _sample(
    samples: list[Sample],
    name: str,
    value: Any,
    labels: dict[str, str],
    kind: Literal["gauge", "counter"] = "gauge",
) -> None:
    """Append a sample if ``value`` is a usable number; a missing field is skipped."""
    number = _number(value)
    if number is not None:
        samples.append(Sample(name=name, labels=dict(labels), value=number, type=kind))


@dataclass(frozen=True)
class Node:
    name: str
    online: bool


def parse_nodes(data: Any) -> list[Node]:
    """``GET /nodes``: valid node names, sorted. Entries that aren't usable are skipped."""
    nodes: dict[str, Node] = {}
    if not isinstance(data, list):
        raise _NotTheApi("nodes is not a list")
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("node")
        if not isinstance(name, str) or not _NODE_RE.fullmatch(name):
            continue
        nodes[name] = Node(name=name, online=entry.get("status") == "online")
    return [nodes[name] for name in sorted(nodes)]


def node_samples(node: Node, status: Any) -> list[Sample]:
    """Per-node samples: up from ``/nodes``, the rest from ``/nodes/{node}/status``."""
    labels = {"node": node.name}
    samples = [
        Sample(name=METRIC_NODE_UP, labels=labels, value=1.0 if node.online else 0.0, type="gauge")
    ]
    if not isinstance(status, dict):
        return samples
    _sample(samples, METRIC_NODE_CPU, status.get("cpu"), labels)
    cpuinfo = status.get("cpuinfo")
    if isinstance(cpuinfo, dict):
        _sample(samples, METRIC_NODE_CPUS, cpuinfo.get("cpus"), labels)
    for field_name, used_metric, total_metric in (
        ("memory", METRIC_NODE_MEM_USED, METRIC_NODE_MEM_TOTAL),
        ("rootfs", METRIC_NODE_ROOT_USED, METRIC_NODE_ROOT_TOTAL),
    ):
        part = status.get(field_name)
        if isinstance(part, dict):
            _sample(samples, used_metric, part.get("used"), labels)
            _sample(samples, total_metric, part.get("total"), labels)
    _sample(samples, METRIC_NODE_UPTIME, status.get("uptime"), labels)
    loadavg = status.get("loadavg")
    if isinstance(loadavg, list) and len(loadavg) == len(METRIC_NODE_LOAD):
        for metric, value in zip(METRIC_NODE_LOAD, loadavg, strict=True):
            _sample(samples, metric, value, labels)
    return samples


@dataclass(frozen=True)
class Guest:
    vmid: int
    kind: str  # "qemu" or "lxc"
    node: str
    entry: dict[str, Any]


def parse_guests(data: Any) -> list[Guest]:
    """``GET /cluster/resources?type=vm``: QEMU VMs and LXC containers, sorted by vmid.

    Templates are skipped (they never run). Entries without a usable vmid, type or node
    are skipped. Free-form fields (name, tags, the lock, the HA state) are not read.
    """
    if not isinstance(data, list):
        raise _NotTheApi("resources is not a list")
    guests: dict[int, Guest] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        vmid = entry.get("vmid")
        node = entry.get("node")
        if kind not in ("qemu", "lxc") or isinstance(vmid, bool) or not isinstance(vmid, int):
            continue
        if not 1 <= vmid <= MAX_VMID or not isinstance(node, str) or not _NODE_RE.fullmatch(node):
            continue
        if entry.get("template") in (1, True):
            continue
        guests[vmid] = Guest(vmid=vmid, kind=kind, node=node, entry=entry)
    return [guests[vmid] for vmid in sorted(guests)]


def guest_samples(guest: Guest) -> list[Sample]:
    labels = {"node": guest.node, "vmid": str(guest.vmid), "guest_type": guest.kind}
    entry = guest.entry
    samples: list[Sample] = []
    status = entry.get("status")
    if isinstance(status, str):
        samples.append(
            Sample(
                name=METRIC_GUEST_RUNNING,
                labels=dict(labels),
                value=1.0 if status == "running" else 0.0,
                type="gauge",
            )
        )
    fields: tuple[tuple[str, str, Literal["gauge", "counter"]], ...] = (
        (METRIC_GUEST_CPU, "cpu", "gauge"),
        (METRIC_GUEST_CPUS, "maxcpu", "gauge"),
        (METRIC_GUEST_MEM_USED, "mem", "gauge"),
        (METRIC_GUEST_MEM_MAX, "maxmem", "gauge"),
        (METRIC_GUEST_DISK_USED, "disk", "gauge"),
        (METRIC_GUEST_DISK_MAX, "maxdisk", "gauge"),
        (METRIC_GUEST_NET_IN, "netin", "counter"),
        (METRIC_GUEST_NET_OUT, "netout", "counter"),
        (METRIC_GUEST_DISK_READ, "diskread", "counter"),
        (METRIC_GUEST_DISK_WRITE, "diskwrite", "counter"),
        (METRIC_GUEST_UPTIME, "uptime", "gauge"),
    )
    for metric, key, kind in fields:
        _sample(samples, metric, entry.get(key), labels, kind)
    # The guest's name is free-form text: only on the info series (value 1), cleaned and
    # capped like the SNMP labels, under the maintainer's label exception (INTEGRATIONS.md
    # section 9). Dashboards join it on vmid. Tags and other text are not read.
    samples.append(
        Sample(
            name=METRIC_GUEST_INFO,
            labels={**labels, "guest_name": clean_text(entry.get("name"), MAX_LABEL_TEXT)},
            value=1.0,
            type="gauge",
        )
    )
    return samples


@dataclass(frozen=True)
class Storage:
    node: str
    storage: str
    entry: dict[str, Any]


def parse_storages(node: str, data: Any) -> list[Storage]:
    """``GET /nodes/{node}/storage``: storages with a valid id, sorted by id."""
    if not isinstance(data, list):
        return []
    found: dict[str, Storage] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        storage = entry.get("storage")
        if isinstance(storage, str) and _STORAGE_RE.fullmatch(storage):
            found[storage] = Storage(node=node, storage=storage, entry=entry)
    return [found[name] for name in sorted(found)]


def storage_samples(storage: Storage) -> list[Sample]:
    labels = {"node": storage.node, "storage": storage.storage}
    entry = storage.entry
    samples: list[Sample] = []
    active = entry.get("active")
    if isinstance(active, int | bool):
        samples.append(
            Sample(
                name=METRIC_STORAGE_ACTIVE,
                labels=dict(labels),
                value=1.0 if active else 0.0,
                type="gauge",
            )
        )
    _sample(samples, METRIC_STORAGE_USED, entry.get("used"), labels)
    _sample(samples, METRIC_STORAGE_TOTAL, entry.get("total"), labels)
    return samples


def _count(name: str, value: int) -> Sample:
    return Sample(name=name, value=float(value), type="gauge")


# --- messages --------------------------------------------------------------------------
# Fixed text plus the host:port as configured: safe to show (see PollFailure).


def _msg_auth(where: str) -> str:
    return (
        f"Proxmox VE API at {where} refused the API token (HTTP 401): check the token ID "
        "and secret, and that the token hasn't expired or been removed"
    )


def _msg_forbidden(where: str) -> str:
    return (
        f"the API token lacks permission on {where} (HTTP 403): give it the PVEAuditor "
        "role on / (for a privilege-separated token, on the token itself)"
    )


def _msg_tls(where: str) -> str:
    return (
        f"certificate not trusted: the TLS certificate of {where} could not be verified. "
        "Paste the Proxmox CA (/etc/pve/pve-root-ca.pem) into 'Trusted certificate', and "
        "make sure the host is a name or address the certificate covers"
    )


def _msg_status(where: str, status: int) -> str:
    return f"Proxmox VE API at {where} answered HTTP {status}"


def _msg_bad_answer(where: str) -> str:
    return (
        f"{where} answered, but not like the Proxmox VE API (check the port, and that "
        "nothing sits in between)"
    )


def _msg_timeout(where: str) -> str:
    return f"timed out waiting for {where}"


def _msg_unreachable(where: str) -> str:
    return f"could not connect to {where} (refused or unreachable)"


def _ascii(text: str, limit: int) -> str:
    """Device text for a test message: printable ASCII, bounded."""
    cleaned = "".join(c for c in text if c.isascii() and c.isprintable())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


@contextmanager
def _poll_failures(where: str) -> Iterator[None]:
    """Turn request failures into PollFailures with fixed, secret-free messages. No
    exception's own text is shown. Guard refusals pass through (the collector shows the
    guard's own safe message)."""
    try:
        yield
    except NetGuardError:
        raise
    except _HttpStatus as exc:
        if exc.status == 401:
            raise ProxmoxAuthFailed(_msg_auth(where)) from None
        if exc.status == 403:
            raise ProxmoxForbidden(_msg_forbidden(where)) from None
        raise ProxmoxRequestFailed(_msg_status(where, exc.status)) from None
    except _NotTheApi:
        raise ProxmoxBadAnswer(_msg_bad_answer(where)) from None
    except (TimeoutError, httpx.TimeoutException):
        raise ProxmoxTimeout(_msg_timeout(where)) from None
    except httpx.ConnectError as exc:
        if is_tls_failure(exc):
            raise ProxmoxTlsUntrusted(_msg_tls(where)) from None
        raise ProxmoxUnreachable(_msg_unreachable(where)) from None
    except httpx.HTTPError as exc:
        raise ProxmoxRequestFailed(f"request to {where} failed ({type(exc).__name__})") from None


# --- the integration -------------------------------------------------------------------


class Proxmox(Integration):
    type_id = TYPE_ID
    display_name = "Proxmox VE"
    category: ClassVar[Category] = "hypervisor"
    version = "0.1.0"
    config_schema = ProxmoxConfig

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        """``GET /version`` (is it Proxmox, is the token valid), then ``GET /nodes`` (can
        the token see anything: Proxmox filters what a token may not read, rather than
        answering 403)."""
        cfg = ctx.config_as(ProxmoxConfig)
        where = cfg.where()
        timeout = float(cfg.timeout_seconds)
        try:
            async with _client(ctx, cfg) as client:
                version = await _get_json(client, PATH_VERSION, timeout)
                nodes = parse_nodes(await _get_json(client, PATH_NODES, timeout))
        except _HttpStatus as exc:
            if exc.status == 401:
                return TestResult(ok=False, message=_msg_auth(where))
            if exc.status == 403:
                return TestResult(ok=False, message=_msg_forbidden(where))
            return TestResult(
                ok=False,
                message=f"{_msg_status(where, exc.status)} (is this the Proxmox VE API port?)",
            )
        except _NotTheApi:
            return TestResult(ok=False, message=_msg_bad_answer(where))
        except httpx.ConnectError as exc:
            if is_tls_failure(exc):
                return TestResult(ok=False, message=_msg_tls(where))
            return connection_failure(exc, where=where, hint=self._hint(cfg))
        except Exception as exc:
            return connection_failure(exc, where=where, hint=self._hint(cfg))
        release = version.get("version") if isinstance(version, dict) else None
        label = f"Proxmox VE {_ascii(release, 32)}" if isinstance(release, str) else "Proxmox VE"
        if not nodes:
            return TestResult(
                ok=False,
                message=(
                    f"{label} accepted the token, but it can't see any node: give it the "
                    "PVEAuditor role on / (for a privilege-separated token, on the token "
                    "itself)"
                ),
            )
        online = sum(1 for node in nodes if node.online)
        plural = "" if len(nodes) == 1 else "s"
        return TestResult(
            ok=True, message=f"{label}: {len(nodes)} node{plural} visible, {online} online"
        )

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        """``GET /nodes`` and ``GET /cluster/resources?type=vm``, then
        ``GET /nodes/{node}/status`` and ``GET /nodes/{node}/storage`` for each online
        node (at most :data:`MAX_NODES`).

        A failure of the two cluster-wide requests raises a :class:`PollFailure` (401,
        403, TLS, timeout, ...), which marks the instance ``down`` with its message. A
        per-node request answered with an error status (a node that just went away, a
        token without access to it) only leaves that node's details out. More than
        :data:`POLL_BUDGET_SECONDS` in all raises :class:`ProxmoxPollTooSlow`.
        """
        cfg = ctx.config_as(ProxmoxConfig)
        where = cfg.where()
        timeout = float(cfg.timeout_seconds)
        samples: list[Sample] = []
        skipped = 0
        try:
            async with asyncio.timeout(POLL_BUDGET_SECONDS):
                with _poll_failures(where):
                    async with _client(ctx, cfg) as client:
                        nodes = parse_nodes(await _get_json(client, PATH_NODES, timeout))
                        guests = parse_guests(await _get_json(client, PATH_GUESTS, timeout))
                        polled_nodes = nodes[:MAX_NODES]
                        storages: list[Storage] = []
                        for node in polled_nodes:
                            status: Any = None
                            if node.online:
                                status = await _optional(
                                    client, node_path(node.name, "status"), timeout
                                )
                                stored = await _optional(
                                    client, node_path(node.name, "storage"), timeout
                                )
                                skipped += (status is None) + (stored is None)
                                storages += parse_storages(node.name, stored)
                            samples += node_samples(node, status)
        except TimeoutError:
            raise ProxmoxPollTooSlow(
                f"Proxmox VE poll of {where} took longer than {POLL_BUDGET_SECONDS:g} s (the "
                "API or a node may be slow, or the cluster has many nodes)"
            ) from None
        if skipped:
            # The instance id and a count only: no config value, no device text.
            logger.warning(
                "proxmox instance %s: %d per-node requests were answered with an error "
                "and left out",
                ctx.instance_id,
                skipped,
            )
        reported_guests = guests[: cfg.max_guests]
        reported_storages = storages[: cfg.max_storages]
        for guest in reported_guests:
            samples += guest_samples(guest)
        for storage in reported_storages:
            samples += storage_samples(storage)
        samples += [
            _count(METRIC_NODES, len(nodes)),
            _count(METRIC_NODES_OMITTED, len(nodes) - len(polled_nodes)),
            _count(METRIC_GUESTS, len(guests)),
            _count(METRIC_GUESTS_OMITTED, len(guests) - len(reported_guests)),
            _count(METRIC_STORAGES, len(storages)),
            _count(METRIC_STORAGES_OMITTED, len(storages) - len(reported_storages)),
        ]
        return samples

    def poll_interval_seconds(self) -> int:
        return 60

    def cards(self) -> list[CardSpec]:
        return [
            CardSpec(card_type="proxmox-node", title="Proxmox node", default_size=(4, 3)),
            CardSpec(card_type="proxmox-guests", title="Guests", default_size=(4, 3)),
        ]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint(ctx.config_as(ProxmoxConfig))]

    @staticmethod
    def _hint(cfg: ProxmoxConfig) -> FirewallHint:
        return FirewallHint(
            protocol="tcp",
            port=cfg.port,
            destination=cfg.host,
            reason="The Vantage backend calls the Proxmox VE API (HTTPS) on this port.",
        )


async def _optional(client: httpx.AsyncClient, path: str, timeout: float) -> Any:  # noqa: ASYNC109
    """A per-node request: ``None`` if it was answered with an error status other than
    401 (which means the token itself is bad, so the whole poll fails). Transport
    failures still raise: the API host stopped answering mid-poll."""
    try:
        return await _get_json(client, path, timeout)
    except _HttpStatus as exc:
        if exc.status == 401:
            raise
        return None

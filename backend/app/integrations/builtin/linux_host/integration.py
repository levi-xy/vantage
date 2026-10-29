"""Linux host: a machine running Prometheus' node_exporter.

Reference integration (M2.5) and the template for **scraped** integrations: Vantage
never polls it. ``scrape_targets()`` tells Prometheus where node_exporter listens;
``/sd/targets`` checks each target with the SSRF guard before serving it and adds the
four standard ``vantage_*`` labels (Contracts 1 and 5). Exporter metrics keep their
native names (``node_cpu_seconds_total``, ...).

Because there is no ``poll()``, the collector reports the instance's status as
``unknown``; Prometheus' own ``up`` series says whether the scrape works.

``test_connection()`` is the one place Vantage itself talks to the exporter: one ``GET``
of the metrics page through the guarded client, to confirm it is node_exporter before
the admin saves. On a NAS it also says what storage the exporter sees (md arrays, ZFS
pools, temperature sensors) and names any array or pool that isn't healthy, so a missing
mount in the exporter's container shows up before the admin relies on the dashboard.

Prometheus' config is fixed (Contract 4), so a target can't carry per-device scrape
credentials or TLS settings: node_exporter must answer without authentication, and over
https only with a certificate Prometheus trusts.
"""

import asyncio
from typing import ClassVar, Literal, NamedTuple

import httpx
from prometheus_client.parser import text_string_to_metric_families
from pydantic import BaseModel, Field, field_validator

from app.integrations.base import (
    CardSpec,
    Category,
    FirewallHint,
    InstanceContext,
    Integration,
    ScrapeTarget,
    TestResult,
)
from app.integrations.builtin.common import (
    USER_AGENT,
    checked_host,
    connection_failure,
    host_port,
)

METRICS_PATH = "/metrics"
# Limit on the connection test's request; below the guarded client's 15 s cap.
TIMEOUT_SECONDS = 10
# node_exporter's page is typically well under 1 MiB; stop reading long before memory
# becomes a concern if something else answers on the port.
MAX_BODY_BYTES = 8 * 1024 * 1024
# Longest device-supplied text (version, node name) quoted in a message.
_MAX_QUOTED = 64
# Most unhealthy arrays/pools named in a message; the rest are counted.
_MAX_NAMED = 5


class LinuxHostConfig(BaseModel):
    """What the admin enters.

    ``host`` is found by name by the SSRF check on save, together with ``port``. There
    are no secrets: Prometheus scrapes the target with no credentials.
    """

    host: str = Field(
        title="Host",
        description="IP address or DNS name of the machine running node_exporter.",
        max_length=253,
        examples=["10.0.0.20"],
    )
    port: int = Field(default=9100, ge=1, le=65535, title="Port")
    scheme: Literal["http", "https"] = Field(
        default="http",
        title="Scheme",
        description="https only if Prometheus trusts the exporter's certificate.",
    )

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return checked_host(value)

    @property
    def address(self) -> str:
        return host_port(self.host, self.port)


def _quote(text: str) -> str:
    """Device-supplied text made safe to show: printable ASCII, bounded length."""
    cleaned = "".join(c for c in text if c.isascii() and c.isprintable())
    return cleaned[:_MAX_QUOTED] or "?"


class NodeExporterInfo(NamedTuple):
    """What a node_exporter page says about the host. Text fields are made safe to show."""

    version: str
    nodename: str
    md_arrays: int = 0
    zfs_pools: int = 0
    temp_sensors: int = 0
    unhealthy: tuple[str, ...] = ()  # md arrays and ZFS pools that aren't healthy

    def summary(self) -> str:
        """The connection test's message."""
        text = f"node_exporter {self.version} on {self.nodename}"
        found = [
            _count(self.md_arrays, "md array"),
            _count(self.zfs_pools, "ZFS pool"),
            _count(self.temp_sensors, "temperature sensor"),
        ]
        found = [part for part in found if part]
        if found:
            text += ": " + ", ".join(found)
        if self.unhealthy:
            named = ", ".join(self.unhealthy[:_MAX_NAMED])
            more = len(self.unhealthy) - _MAX_NAMED
            text += f"; not healthy: {named}" + (f" and {more} more" if more > 0 else "")
        return text


def _count(n: int, noun: str) -> str:
    return "" if n == 0 else f"{n} {noun}" + ("" if n == 1 else "s")


def describe_node_exporter(text: str) -> NodeExporterInfo | None:
    """What a node_exporter metrics page says, or ``None`` if the page isn't Prometheus
    text format or isn't node_exporter's.

    Storage, read from node_exporter's native metrics:

    - md arrays: ``node_md_disks_required`` (one series per array). An array is not
      healthy if ``node_md_degraded`` > 0, ``node_md_disks{state="failed"}`` > 0 or
      ``node_md_state{state="inactive"}`` is 1.
    - ZFS pools: ``node_zfs_zpool_state`` (one series per pool and state, ``1`` on the
      current state). A pool is not healthy unless its current state is ``online``.
    - temperature sensors: ``node_hwmon_temp_celsius`` series.

    ``ValueError`` from the parser (malformed exposition) counts as "not metrics".
    """
    version: str | None = None
    nodename = "?"
    md_arrays: set[str] = set()
    pools: set[str] = set()
    sensors = 0
    unhealthy: set[str] = set()
    try:
        for family in text_string_to_metric_families(text):
            name = family.name
            if name == "node_exporter_build_info" and family.samples:
                version = family.samples[0].labels.get("version", "?")
            elif name == "node_uname_info" and family.samples:
                nodename = family.samples[0].labels.get("nodename", "?")
            elif name == "node_md_disks_required":
                md_arrays.update(s.labels.get("device", "?") for s in family.samples)
            elif name in ("node_md_degraded", "node_md_disks", "node_md_state"):
                for s in family.samples:
                    state = s.labels.get("state")
                    bad = (
                        (name == "node_md_degraded" and s.value > 0)
                        or (name == "node_md_disks" and state == "failed" and s.value > 0)
                        or (name == "node_md_state" and state == "inactive" and s.value == 1)
                    )
                    if bad:
                        unhealthy.add(s.labels.get("device", "?"))
            elif name == "node_zfs_zpool_state":
                for s in family.samples:
                    pool = s.labels.get("zpool", "?")
                    pools.add(pool)
                    if s.value == 1 and s.labels.get("state") != "online":
                        unhealthy.add(pool)
            elif name == "node_hwmon_temp_celsius":
                sensors += len(family.samples)
    except ValueError:
        return None
    if version is None:
        return None
    return NodeExporterInfo(
        version=_quote(version),
        nodename=_quote(nodename),
        md_arrays=len(md_arrays),
        zfs_pools=len(pools),
        temp_sensors=sensors,
        unhealthy=tuple(sorted({_quote(n) for n in unhealthy})),
    )


async def _read_limited(response: httpx.Response) -> bytes | None:
    """The body, or ``None`` if it's larger than :data:`MAX_BODY_BYTES`."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


class LinuxHost(Integration):
    type_id = "linux_host"
    display_name = "Linux host (node_exporter)"
    category: ClassVar[Category] = "host"
    version = "0.2.0"
    config_schema = LinuxHostConfig

    def scrape_targets(self, ctx: InstanceContext) -> list[ScrapeTarget]:
        # Synchronous and network-free: /sd/targets does the guard check for every target.
        cfg = ctx.config_as(LinuxHostConfig)
        return [ScrapeTarget(address=cfg.address, metrics_path=METRICS_PATH, scheme=cfg.scheme)]

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        cfg = ctx.config_as(LinuxHostConfig)
        url = f"{cfg.scheme}://{cfg.address}{METRICS_PATH}"
        try:
            # As in http_check: asyncio.timeout bounds the whole exchange (DNS, connect,
            # TLS, body); the guarded client's own 15 s cap sits underneath.
            # verify stays on: Prometheus will verify the certificate too.
            async with (
                asyncio.timeout(TIMEOUT_SECONDS),
                ctx.http_client(headers={"User-Agent": USER_AGENT}) as client,
                client.stream("GET", url) as response,
            ):
                status = response.status_code
                body = await _read_limited(response) if status == 200 else b""
        except Exception as exc:
            return connection_failure(exc, where=cfg.address, hint=self._hint(cfg))
        if status != 200:
            return TestResult(
                ok=False, message=f"{cfg.address}{METRICS_PATH} answered HTTP {status}"
            )
        if body is None:
            return TestResult(ok=False, message="the metrics page is too large to be node_exporter")
        found = describe_node_exporter(body.decode("utf-8", errors="replace"))
        if found is None:
            return TestResult(
                ok=False,
                message=f"{cfg.address} answered, but not with node_exporter metrics",
            )
        return TestResult(ok=True, message=found.summary())

    def cards(self) -> list[CardSpec]:
        return [CardSpec(card_type="linux-host", title="Linux host", default_size=(4, 3))]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint(ctx.config_as(LinuxHostConfig))]

    @staticmethod
    def _hint(cfg: LinuxHostConfig) -> FirewallHint:
        return FirewallHint(
            protocol="tcp",
            port=cfg.port,
            destination=cfg.host,
            reason="Prometheus scrapes node_exporter on this port (and Vantage tests it).",
        )

"""SMART add-on: a machine running smartctl_exporter.

Scrape-only, like ``linux_host``: Vantage never polls. ``scrape_targets()`` tells
Prometheus where smartctl_exporter listens; ``/sd/targets`` checks each target with the
SSRF guard and adds the four standard ``vantage_*`` labels (Contracts 1 and 5).
Exporter metrics keep their native names (``smartctl_device_smart_status``,
``smartctl_device_temperature``, ...).

``test_connection()`` makes one ``GET /metrics`` through the guarded client to confirm
the page is smartctl_exporter's. Read-only: Vantage never talks to a disk, and the
exporter itself only runs ``smartctl`` queries.

Prometheus' config is fixed (Contract 4): the exporter must answer without
authentication, and over https only with a certificate Prometheus trusts.
"""

import asyncio
from typing import ClassVar, Literal

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
# Stop reading long before memory matters if something else answers on the port.
MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_QUOTED = 64


class SmartExporterConfig(BaseModel):
    """What the admin enters. No secrets: Prometheus scrapes with no credentials."""

    host: str = Field(
        title="Host",
        description="IP address or DNS name of the machine running smartctl_exporter.",
        max_length=253,
        examples=["10.0.0.20"],
    )
    port: int = Field(default=9633, ge=1, le=65535, title="Port")
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


def describe_smartctl_exporter(text: str) -> tuple[str, int] | None:
    """``(smartctl version, disk count)`` from a metrics page, or ``None`` if the page
    isn't Prometheus text format or isn't smartctl_exporter's."""
    version: str | None = None
    disks: set[str] = set()
    try:
        for family in text_string_to_metric_families(text):
            if family.name == "smartctl_version" and family.samples:
                version = family.samples[0].labels.get("smartctl_version", "?")
            elif family.name == "smartctl_device":
                disks.update(s.labels.get("device", "?") for s in family.samples)
    except ValueError:
        return None
    if version is None:
        return None
    return _quote(version), len(disks)


async def _read_limited(response: httpx.Response) -> bytes | None:
    """The body, or ``None`` if it's larger than :data:`MAX_BODY_BYTES`."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


class SmartExporter(Integration):
    type_id = "smart_exporter"
    display_name = "SMART disk health (smartctl_exporter)"
    category: ClassVar[Category] = "storage"
    version = "0.1.0"
    config_schema = SmartExporterConfig

    def scrape_targets(self, ctx: InstanceContext) -> list[ScrapeTarget]:
        # Synchronous and network-free: /sd/targets does the guard check for every target.
        cfg = ctx.config_as(SmartExporterConfig)
        return [ScrapeTarget(address=cfg.address, metrics_path=METRICS_PATH, scheme=cfg.scheme)]

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        cfg = ctx.config_as(SmartExporterConfig)
        url = f"{cfg.scheme}://{cfg.address}{METRICS_PATH}"
        try:
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
            return TestResult(
                ok=False, message="the metrics page is too large to be smartctl_exporter"
            )
        found = describe_smartctl_exporter(body.decode("utf-8", errors="replace"))
        if found is None:
            return TestResult(
                ok=False,
                message=f"{cfg.address} answered, but not with smartctl_exporter metrics",
            )
        version, disks = found
        return TestResult(ok=True, message=f"smartctl {version}, {disks} disk(s) reported")

    def cards(self) -> list[CardSpec]:
        return [CardSpec(card_type="smart-disks", title="Disk health", default_size=(4, 3))]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint(ctx.config_as(SmartExporterConfig))]

    @staticmethod
    def _hint(cfg: SmartExporterConfig) -> FirewallHint:
        return FirewallHint(
            protocol="tcp",
            port=cfg.port,
            destination=cfg.host,
            reason="Prometheus scrapes smartctl_exporter on this port (and Vantage tests it).",
        )

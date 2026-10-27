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
the admin saves.

Prometheus' config is fixed (Contract 4), so a target can't carry per-device scrape
credentials or TLS settings: node_exporter must answer without authentication, and over
https only with a certificate Prometheus trusts.
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
# node_exporter's page is typically well under 1 MiB; stop reading long before memory
# becomes a concern if something else answers on the port.
MAX_BODY_BYTES = 8 * 1024 * 1024
# Longest device-supplied text (version, node name) quoted in a message.
_MAX_QUOTED = 64


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


def describe_node_exporter(text: str) -> tuple[str, str] | None:
    """``(version, nodename)`` from a node_exporter metrics page, or ``None`` if the page
    isn't Prometheus text format or isn't node_exporter's.

    ``ValueError`` from the parser (malformed exposition) counts as "not metrics".
    """
    version: str | None = None
    nodename = "?"
    try:
        for family in text_string_to_metric_families(text):
            if family.name == "node_exporter_build_info" and family.samples:
                version = family.samples[0].labels.get("version", "?")
            elif family.name == "node_uname_info" and family.samples:
                nodename = family.samples[0].labels.get("nodename", "?")
    except ValueError:
        return None
    if version is None:
        return None
    return _quote(version), _quote(nodename)


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
    version = "0.1.0"
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
        version, nodename = found
        return TestResult(ok=True, message=f"node_exporter {version} on {nodename}")

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

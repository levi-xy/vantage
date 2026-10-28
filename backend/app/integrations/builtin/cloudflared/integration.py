"""cloudflared: the Cloudflare Tunnel connector's local metrics endpoint.

Scrape-only, like ``linux_host``. cloudflared serves Prometheus metrics itself
(``--metrics host:port``); Prometheus scrapes them under their native names
(``cloudflared_tunnel_ha_connections``, ``cloudflared_tunnel_total_requests``,
``cloudflared_tunnel_request_errors``, ``cloudflared_proxy_connect_latency``, ...) and
``/sd/targets`` adds the four standard ``vantage_*`` labels.

This is the connector's *local* endpoint on a private address. It is not the Cloudflare
cloud API, so there are no ``cloud_hosts``.

``test_connection()`` makes one ``GET /metrics`` through the guarded client and checks
the page is cloudflared's.
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
TIMEOUT_SECONDS = 10
MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_QUOTED = 64
# cloudflared's own families all start with this; the Go runtime's do not.
_FAMILY_PREFIX = "cloudflared_"


class CloudflaredConfig(BaseModel):
    """What the admin enters. No secrets: the metrics endpoint is unauthenticated."""

    host: str = Field(
        title="Host",
        description="IP address or DNS name of the machine running cloudflared.",
        max_length=253,
        examples=["10.0.0.40"],
    )
    port: int = Field(
        default=20241,
        ge=1,
        le=65535,
        title="Metrics port",
        description="The port given to cloudflared's --metrics option.",
    )
    scheme: Literal["http", "https"] = Field(
        default="http",
        title="Scheme",
        description="cloudflared serves plain http; https only behind a proxy Prometheus trusts.",
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


def describe_cloudflared(text: str) -> str | None:
    """The cloudflared version (``"?"`` if not reported) from a metrics page, or ``None``
    if the page isn't Prometheus text format or has no cloudflared metric."""
    seen = False
    version = "?"
    try:
        for family in text_string_to_metric_families(text):
            if family.name.startswith(_FAMILY_PREFIX):
                seen = True
            if family.name in ("build_info", "cloudflared_build_info") and family.samples:
                version = family.samples[0].labels.get("version", "?")
    except ValueError:
        return None
    return _quote(version) if seen else None


async def _read_limited(response: httpx.Response) -> bytes | None:
    """The body, or ``None`` if it's larger than :data:`MAX_BODY_BYTES`."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


class Cloudflared(Integration):
    type_id = "cloudflared"
    display_name = "Cloudflare Tunnel (cloudflared)"
    category: ClassVar[Category] = "tunnel"
    version = "0.1.0"
    config_schema = CloudflaredConfig

    def scrape_targets(self, ctx: InstanceContext) -> list[ScrapeTarget]:
        cfg = ctx.config_as(CloudflaredConfig)
        return [ScrapeTarget(address=cfg.address, metrics_path=METRICS_PATH, scheme=cfg.scheme)]

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        cfg = ctx.config_as(CloudflaredConfig)
        url = f"{cfg.scheme}://{cfg.address}{METRICS_PATH}"
        try:
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
            return TestResult(ok=False, message="the metrics page is too large to be cloudflared")
        version = describe_cloudflared(body.decode("utf-8", errors="replace"))
        if version is None:
            return TestResult(
                ok=False, message=f"{cfg.address} answered, but not with cloudflared metrics"
            )
        return TestResult(ok=True, message=f"cloudflared {version}")

    def cards(self) -> list[CardSpec]:
        return [CardSpec(card_type="cloudflared", title="Cloudflare Tunnel", default_size=(4, 3))]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint(ctx.config_as(CloudflaredConfig))]

    @staticmethod
    def _hint(cfg: CloudflaredConfig) -> FirewallHint:
        return FirewallHint(
            protocol="tcp",
            port=cfg.port,
            destination=cfg.host,
            reason="Prometheus scrapes cloudflared's metrics port (and Vantage tests it).",
        )

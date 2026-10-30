"""pfSense: a firewall running the Telegraf package with the ``prometheus_client`` output.

A **scraped** integration, like ``linux_host``: Vantage never polls it. Prometheus scrapes
Telegraf's ``/metrics`` (native Telegraf names: ``net_bytes_recv``, ``ping_*``,
``pf_entries``, ``cpu_usage_*``, ``mem_*``). ``/sd/targets`` guard-checks the target and
adds the four standard ``vantage_*`` labels. The collector reports status ``unknown``.

``test_connection()`` makes one ``GET /metrics`` through the guarded client and checks the
page looks like pfSense's Telegraf output. Nothing is ever sent to pfSense's own web UI or
API, so there is nothing that could change its state.

Prometheus' config is fixed (Contract 4): Telegraf must answer without authentication.
"""

import asyncio
import re
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

# Telegraf metric names that identify the output and that the cards read.
IFACE_METRIC = "net_bytes_recv"  # label: interface
RTT_METRIC = "ping_average_response_ms"  # label: url (the monitored gateway address)
LOSS_METRIC = "ping_percent_packet_loss"
STATES_METRIC = "pf_entries"
CPU_IDLE_METRIC = "cpu_usage_idle"
MEM_TOTAL_METRIC = "mem_total"
# Telegraf's ping input emits the RTT families (and ``ping_ttl``) only when a reply came
# back. A gateway with 100% loss has only loss, packet counts and result code, so gateways
# are counted from any of these, keyed by ``url``.
GATEWAY_METRICS = frozenset(
    {RTT_METRIC, LOSS_METRIC, "ping_packets_transmitted", "ping_result_code"}
)
_RECOGNISED = GATEWAY_METRICS | {IFACE_METRIC, STATES_METRIC, CPU_IDLE_METRIC, MEM_TOTAL_METRIC}


# Display settings for interfaces: OS interface names as Telegraf reports them (igc0, vtnet1,
# lagg0.10). Used by the cards only, never as metric labels.
MAX_INTERFACE_SETTINGS = 64
_INTERFACE = re.compile(r"[A-Za-z0-9_.:-]{1,32}")


class PfSenseConfig(BaseModel):
    """What the admin enters: where Telegraf's ``prometheus_client`` output listens.

    No secrets: Prometheus scrapes with no credentials.
    """

    host: str = Field(
        title="Host",
        description="IP address or DNS name of the pfSense firewall running Telegraf.",
        max_length=253,
        examples=["10.0.0.1"],
    )
    port: int = Field(
        default=9273,
        ge=1,
        le=65535,
        title="Port",
        description="Telegraf's prometheus_client listen port.",
    )
    scheme: Literal["http", "https"] = Field(
        default="http",
        title="Scheme",
        description="https only if Prometheus trusts Telegraf's certificate.",
    )

    interface_names: list[str] = Field(
        default_factory=list,
        max_length=MAX_INTERFACE_SETTINGS,
        title="Interface names",
        description=(
            "Optional. One per line: the interface as Telegraf reports it, '=', and the "
            "name to show, e.g. igc0=WAN. Interfaces without a name show as reported."
        ),
        examples=[["igc0=WAN", "igc1=LAN"]],
    )
    hidden_interfaces: list[str] = Field(
        default_factory=list,
        max_length=MAX_INTERFACE_SETTINGS,
        title="Hidden interfaces",
        description=(
            "Optional. One per line: interfaces (as Telegraf reports them) to leave off the "
            "dashboard, e.g. igc3. Their data is still collected."
        ),
        examples=[["igc3"]],
    )

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return checked_host(value)

    @field_validator("interface_names")
    @classmethod
    def _check_names(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            interface, sep, name = value.partition("=")
            interface, name = interface.strip(), " ".join(name.split())
            if not sep or not _INTERFACE.fullmatch(interface) or not 1 <= len(name) <= 32:
                raise ValueError(
                    f"{value[:40]!r}: use interface=name, e.g. igc0=WAN (name up to 32 characters)"
                )
            if not name.isprintable():
                raise ValueError(f"{interface}: the name has characters that can't be shown")
            cleaned.append(f"{interface}={name}")
        return cleaned

    @field_validator("hidden_interfaces")
    @classmethod
    def _check_hidden(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        for value in cleaned:
            if not _INTERFACE.fullmatch(value):
                raise ValueError(f"{value[:40]!r} is not an interface name like igc0")
        return cleaned

    @property
    def address(self) -> str:
        return host_port(self.host, self.port)


def describe_telegraf(text: str) -> tuple[int, int] | None:
    """``(interfaces, gateways)`` counted in a Telegraf metrics page, or ``None`` if the
    page isn't Prometheus text, or has none of the metrics the cards read.

    Only counts are returned: device-supplied label text never reaches a message.
    """
    interfaces: set[str] = set()
    gateways: set[str] = set()
    seen = False
    try:
        for family in text_string_to_metric_families(text):
            # Match on the family name: the parser renames counter samples to ``*_total``
            # (Telegraf's ``net_bytes_recv`` is a counter), Prometheus keeps them as sent.
            if family.name not in _RECOGNISED:
                continue
            seen = True
            for sample in family.samples:
                if family.name == IFACE_METRIC:
                    interfaces.add(sample.labels.get("interface", ""))
                elif family.name in GATEWAY_METRICS and sample.labels.get("url"):
                    gateways.add(sample.labels["url"])
    except ValueError:
        return None
    return (len(interfaces), len(gateways)) if seen else None


async def _read_limited(response: httpx.Response) -> bytes | None:
    """The body, or ``None`` if it's larger than :data:`MAX_BODY_BYTES`."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


class PfSense(Integration):
    type_id = "pfsense"
    display_name = "pfSense (Telegraf)"
    category: ClassVar[Category] = "firewall"
    version = "0.1.0"
    config_schema = PfSenseConfig

    def scrape_targets(self, ctx: InstanceContext) -> list[ScrapeTarget]:
        cfg = ctx.config_as(PfSenseConfig)
        return [ScrapeTarget(address=cfg.address, metrics_path=METRICS_PATH, scheme=cfg.scheme)]

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        cfg = ctx.config_as(PfSenseConfig)
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
            return TestResult(ok=False, message="the metrics page is too large to be Telegraf's")
        found = describe_telegraf(body.decode("utf-8", errors="replace"))
        if found is None:
            return TestResult(
                ok=False,
                message=f"{cfg.address} answered, but not with Telegraf firewall metrics",
            )
        interfaces, gateways = found
        return TestResult(
            ok=True,
            message=f"Telegraf metrics: {interfaces} interfaces, {gateways} gateways",
        )

    def cards(self) -> list[CardSpec]:
        return [
            # "pfsense-overview" kept as the id so saved dashboards keep the card.
            CardSpec(card_type="pfsense-overview", title="Resources", default_size=(4, 3)),
            CardSpec(card_type="pfsense-interfaces", title="Interfaces", default_size=(4, 3)),
            CardSpec(card_type="pfsense-gateways", title="Gateways", default_size=(4, 2)),
        ]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint(ctx.config_as(PfSenseConfig))]

    @staticmethod
    def _hint(cfg: PfSenseConfig) -> FirewallHint:
        return FirewallHint(
            protocol="tcp",
            port=cfg.port,
            destination=cfg.host,
            reason=(
                "Prometheus scrapes Telegraf's prometheus_client on this port "
                "(and the Vantage backend tests it). Allow it on the pfSense "
                "interface facing Vantage."
            ),
        )

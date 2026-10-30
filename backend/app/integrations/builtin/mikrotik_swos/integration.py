"""MikroTik SwOS switch, polled over SNMP v1 or v2c: a profile on the M4d machinery.

SwOS (the switch-only OS of CSS and CRS models) has a read-only SNMP agent with v1 and
v2c, a community and an address allow-list; there is no v3. This profile reuses
``ifmib`` (MIB-II system group and IF-MIB) through ``ctx.snmp_client()``, the guarded
client with GET and WALK only: there is no SET anywhere (Hard rule 2). On top of that it
adds SwOS-style port naming and an opportunistic read of two MikroTik health objects.

What SwOS really answers is **not verified against a device** (it can't be emulated in
our lab); the fixtures are hand-written from MikroTik's public MIB. Every guess is
marked ``TODO(maintainer)`` and listed in ``docs/integrations/mikrotik_swos.md``.

Secrets: only the community (``SecretStr``). It goes into the SNMP request and nowhere
else: messages name only the host and port as configured.
"""

import asyncio
import logging
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from app.core.net_guard import (
    MAX_SNMP_RETRIES,
    MAX_SNMP_TIMEOUT,
    GuardedSnmpClient,
    NetGuardError,
    SnmpError,
    SnmpTimeout,
    SnmpV1,
    SnmpV2c,
)
from app.integrations.base import (
    CardSpec,
    Category,
    FirewallHint,
    InstanceContext,
    Integration,
    Sample,
    TestResult,
)
from app.integrations.builtin import ifmib
from app.integrations.builtin.common import checked_host, host_port
from app.integrations.builtin.snmp_device.integration import (
    MAX_FILTER_PATTERNS,
    POLL_BUDGET_SECONDS,
    FilterPattern,
    SnmpNoResponse,
    SnmpPollTooSlow,
    SnmpWalkTimedOut,
    _ascii,
    _failures,
    _no_response,
    _request_failed,
    _walk_timed_out,
)

TYPE_ID = "mikrotik_swos"
METRIC_PREFIX = f"vantage_{TYPE_ID}"

_TEST_DESCR_CHARS = 80

# MIKROTIK-MIB mtxrHealth group (1.3.6.1.4.1.14988.1.1.3), scalar instances.
# TODO(maintainer): lab checkpoint. Whether SwOS answers these (and in which units) is
# unverified. RouterOS reports tenths (dV, dC); the poll assumes the same and emits
# nothing when the object is absent.
MTXR_HL_VOLTAGE = "1.3.6.1.4.1.14988.1.1.3.8.0"
MTXR_HL_TEMPERATURE = "1.3.6.1.4.1.14988.1.1.3.10.0"
HEALTH_OIDS = (MTXR_HL_VOLTAGE, MTXR_HL_TEMPERATURE)

logger = logging.getLogger(__name__)

# Default SwOS-style port names ("Port1", "SFP1", "SFP+9", "ether1", "sfp-sfpplus1"):
# stable identifiers. Anything else is an admin's own text (a renamed port).
_PORT_NAME = re.compile(
    r"(port|ether|sfp-sfpplus|sfpplus|sfp\+|sfp|qsfpplus|qsfp\+|qsfp)\s*-?\s*(\d{1,3})",
    re.IGNORECASE,
)
_CANONICAL_KIND = {"sfp+": "sfpplus", "sfp-sfpplus": "sfpplus", "qsfp+": "qsfpplus"}


def swos_port_name(interface: ifmib.Interface) -> str:
    """A stable, unique label for a port: ``port1``, ``sfp9``, ``sfpplus1``, ...

    SwOS lets the admin rename every port, and the name comes back in ifDescr (and
    possibly ifName). Free-form text must not become the label of every value series
    (maintainer decision, 2026-10-28), and renaming must not split a port's counters. So
    a name is used only when it is a default one (kind plus number, normalised to
    lowercase without spaces) **and its number is the port's ifIndex**; any other name
    falls back to ``port<ifIndex>``. Requiring number == ifIndex keeps ``if_name``
    unique per device: a port renamed to another port's default name ("Port1" on ifIndex
    5) can't clash with the real one, and every name embeds its own ifIndex. The admin's
    own text stays on ``if_info`` as ``if_descr`` / ``if_alias``.

    TODO(maintainer): lab checkpoint. This assumes SwOS numbers ports ifIndex 1..N in
    the same order as their default names. If SFP ports are numbered separately ("SFP1"
    at ifIndex 9), they fall back to ``port9`` until the mapping is known.
    """
    for raw in (interface.name, interface.descr):
        match = _PORT_NAME.fullmatch(raw.strip())
        if match and int(match.group(2)) == interface.index:
            kind = match.group(1).lower()
            return f"{_CANONICAL_KIND.get(kind, kind)}{interface.index}"
    return f"port{interface.index}"


class MikroTikSwOSConfig(BaseModel):
    """What the admin enters. The JSON schema of this model drives the UI form."""

    host: str = Field(
        title="Host",
        description=(
            "IP address or DNS name of the switch. Must be on a private network "
            "(RFC 1918, CGNAT or IPv6 ULA). Stored in plaintext and shown to every user."
        ),
        max_length=253,
        examples=["10.0.0.41"],
    )
    port: int = Field(default=161, ge=1, le=65535, title="UDP port")
    version: Literal["1", "2c"] = Field(
        default="2c",
        title="SNMP version",
        description="SwOS supports SNMP v1 and v2c only. Both send the community in clear.",
    )
    community: SecretStr = Field(
        title="Community",
        description="Use a read-only community; set it in SwOS under SNMP.",
        min_length=1,
        max_length=255,
    )
    timeout_seconds: int = Field(
        default=2, ge=1, le=int(MAX_SNMP_TIMEOUT), title="Timeout per request (seconds)"
    )
    retries: int = Field(default=1, ge=0, le=MAX_SNMP_RETRIES, title="Retries per request")
    include_interfaces: list[FilterPattern] = Field(
        default_factory=list,
        max_length=MAX_FILTER_PATTERNS,
        title="Only these ports",
        description="Optional. Patterns such as Port1* or SFP*, matched without case. Empty: all.",
    )
    exclude_interfaces: list[FilterPattern] = Field(
        default_factory=list,
        max_length=MAX_FILTER_PATTERNS,
        title="Skip these ports",
        description="Optional. Same patterns; an excluded port is skipped even if included.",
    )
    max_interfaces: int = Field(
        default=64,
        ge=1,
        le=ifmib.MAX_INTERFACES,
        title="Ports per poll (at most)",
        description="The lowest-numbered ports that pass the filter are polled, up to this many.",
    )

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return checked_host(value)

    def auth(self) -> SnmpV1 | SnmpV2c:
        cls = SnmpV1 if self.version == "1" else SnmpV2c
        return cls(community=self.community)

    def ifmib_options(self) -> ifmib.IfMibOptions:
        return ifmib.IfMibOptions(
            max_interfaces=self.max_interfaces,
            filter=ifmib.InterfaceFilter(
                include=tuple(self.include_interfaces), exclude=tuple(self.exclude_interfaces)
            ),
            hc_counters=self.version != "1",  # SNMPv1 can't carry Counter64
            port_name=swos_port_name,
        )


async def read_health(client: GuardedSnmpClient) -> dict[str, float]:
    """Supply voltage (V) and temperature (degrees C), whichever the switch answers.

    One GET per object: on v1 one missing object fails a whole GET (``noSuchName``).
    Best effort: an object that is absent, unreadable or answered with an error is left
    out, and silence stops the reading (the system GET before it already proved the
    agent answers, so this is only a slow moment, not a failure of the poll).
    """
    out: dict[str, float] = {}
    for key, oid, scale in (
        ("voltage_volts", MTXR_HL_VOLTAGE, 0.1),
        ("temperature_celsius", MTXR_HL_TEMPERATURE, 0.1),
    ):
        try:
            rows = await client.get([oid])
        except SnmpTimeout:
            break
        except SnmpError:
            continue
        for row in rows:
            value = row.value
            if (
                row.oid.lstrip(".") == oid
                and isinstance(value, int)
                and not isinstance(value, bool)
            ):
                out[key] = value * scale
    return out


def health_samples(health: dict[str, float]) -> list[Sample]:
    return [
        Sample(name=f"{METRIC_PREFIX}_{key}", value=round(value, 3), type="gauge")
        for key, value in sorted(health.items())
    ]


class MikroTikSwOS(Integration):
    type_id = TYPE_ID
    display_name = "MikroTik SwOS switch"
    category: ClassVar[Category] = "switch"
    version = "0.1.0"
    config_schema = MikroTikSwOSConfig

    @staticmethod
    def _client(ctx: InstanceContext, cfg: MikroTikSwOSConfig) -> GuardedSnmpClient:
        return ctx.snmp_client(
            cfg.host,
            cfg.auth(),
            port=cfg.port,
            timeout=float(cfg.timeout_seconds),
            retries=cfg.retries,
        )

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        """One GET of sysDescr and sysName."""
        cfg = ctx.config_as(MikroTikSwOSConfig)
        where = host_port(cfg.host, cfg.port)
        client = self._client(ctx, cfg)
        try:
            rows = await client.get([ifmib.SYS_DESCR, ifmib.SYS_NAME])
        except NetGuardError as exc:
            return TestResult(ok=False, message=f"refused by the network guard: {exc}")
        except SnmpTimeout:
            return TestResult(ok=False, message=_no_response(where), hint=self._hint(cfg))
        except SnmpError:
            return TestResult(ok=False, message=_request_failed(where))
        finally:
            client.close()
        info = ifmib.parse_system(rows)
        if not info.readable:
            return TestResult(
                ok=False,
                message=(
                    f"{where} answered, but sysDescr and sysName are not readable (check "
                    "the community and the SNMP settings in SwOS)"
                ),
            )
        name = _ascii(info.name, 64) or "?"
        descr = _ascii(info.descr, _TEST_DESCR_CHARS)
        detail = f": {descr}" if descr else ""
        return TestResult(ok=True, message=f"SNMP v{cfg.version} answer from {name}{detail}")

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        """The system group, the IF-MIB walks, then the optional health objects.

        Failures are the ones of ``snmp_device`` (:class:`SnmpNoResponse`,
        :class:`SnmpWalkTimedOut`, ...), with the same messages.
        """
        cfg = ctx.config_as(MikroTikSwOSConfig)
        where = host_port(cfg.host, cfg.port)
        client = self._client(ctx, cfg)
        try:
            async with asyncio.timeout(POLL_BUDGET_SECONDS):
                with _failures(where, timeout=SnmpNoResponse(_no_response(where))):
                    system = await ifmib.read_system(client, v1=cfg.version == "1")
                with _failures(where, timeout=SnmpWalkTimedOut(_walk_timed_out(where))):
                    table = await ifmib.read_interfaces(client, cfg.ifmib_options())
                health = await read_health(client)
        except TimeoutError:
            raise SnmpPollTooSlow(
                f"SNMP poll of {where} took longer than {POLL_BUDGET_SECONDS:g} s (the "
                "switch may be slow or have too many ports)"
            ) from None
        finally:
            client.close()
        if table.truncated:
            logger.warning(
                "mikrotik_swos instance %s: the ifTable has more than %d rows; only the "
                "first %d were read",
                ctx.instance_id,
                ifmib.MAX_WALK_ROWS,
                ifmib.MAX_WALK_ROWS,
            )
        return (
            ifmib.system_samples(system, METRIC_PREFIX)
            + ifmib.interface_samples(table, METRIC_PREFIX, label_on_info=True)
            + health_samples(health)
        )

    def poll_interval_seconds(self) -> int:
        return 60

    def cards(self) -> list[CardSpec]:
        return [
            CardSpec(card_type="snmp-overview", title="SwOS switch", default_size=(2, 2)),
            CardSpec(card_type="snmp-ports", title="Ports", default_size=(4, 2)),
            CardSpec(card_type="switch-panel", title="Front panel", default_size=(8, 4)),
        ]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint(ctx.config_as(MikroTikSwOSConfig))]

    @staticmethod
    def _hint(cfg: MikroTikSwOSConfig) -> FirewallHint:
        return FirewallHint(
            protocol="udp",
            port=cfg.port,
            destination=cfg.host,
            reason=(
                "The Vantage backend sends SNMP requests to this port: allow them and the "
                "switch's replies back, and allow the backend's address in SwOS under "
                "SNMP (allowed addresses) and in any firewall in between."
            ),
        )

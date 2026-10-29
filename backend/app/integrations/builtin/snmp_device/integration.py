"""Generic SNMP device: MIB-II system info and IF-MIB interface counters, polled.

**Polled**: Vantage reads the device on every poll through ``ctx.snmp_client()``, the
guarded SNMP client, and exposes ``vantage_snmp_device_*`` on its own ``/metrics``.
SNMP v1, v2c and v3 (authNoPriv, authPriv).

Read-only by construction: the guarded client offers ``get`` and ``walk`` only, there is
no SET anywhere (Hard rule 2). What is read is listed in ``ifmib.py`` and in
``docs/integrations/snmp_device.md``.

Secrets: the community (v1/v2c) and the v3 authentication and privacy keys are
top-level ``SecretStr`` fields. They go into the SNMP request (the community string, or
the USM keys, which never leave the backend in clear) and nowhere else: messages name
only the host and port as configured.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.net_guard import (
    MAX_SNMP_RETRIES,
    MAX_SNMP_TIMEOUT,
    GuardedSnmpClient,
    NetGuardError,
    SnmpError,
    SnmpTimeout,
    SnmpV1,
    SnmpV2c,
    SnmpV3,
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
from app.integrations.errors import PollFailure

TYPE_ID = "snmp_device"
# Metric names are vantage_<type_id>_<metric> (Contract 1); ifmib builds them from this.
METRIC_PREFIX = f"vantage_{TYPE_ID}"

MAX_FILTER_PATTERNS = 32
_TEST_DESCR_CHARS = 80

FilterPattern = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


class SnmpNoResponse(PollFailure):
    """The agent did not answer the first request (timeout after retries)."""


class SnmpWalkTimedOut(PollFailure):
    """The agent answered, then stopped answering partway through the interface walks."""


class SnmpRequestFailed(PollFailure):
    """The agent answered with an error (v3 user or key mismatch, an error status)."""


class SnmpDeviceConfig(BaseModel):
    """What the admin enters. The JSON schema of this model drives the UI form.

    ``host`` is found by name by the SSRF check on save, resolved with ``port``. The
    community and the v3 keys are the secrets; every other field is plaintext config,
    shown to every user.
    """

    host: str = Field(
        title="Host",
        description=(
            "IP address or DNS name of the SNMP agent. Must be on a private network "
            "(RFC 1918, CGNAT or IPv6 ULA). Stored in plaintext and shown to every user."
        ),
        max_length=253,
        examples=["10.0.0.40"],
    )
    port: int = Field(default=161, ge=1, le=65535, title="UDP port")
    version: Literal["1", "2c", "3"] = Field(
        default="2c",
        title="SNMP version",
        description="v3 authenticates and can encrypt; v1 and v2c send the community in clear.",
    )
    community: SecretStr | None = Field(
        default=None,
        title="Community",
        description="v1 and v2c only. Use a read-only community.",
    )
    username: str | None = Field(
        default=None,
        title="SNMPv3 user",
        description="v3 only. Stored in plaintext.",
        min_length=1,
        max_length=32,
        pattern=r"^[!-~]+$",  # printable ASCII, no spaces
    )
    security_level: Literal["authNoPriv", "authPriv"] = Field(
        default="authPriv",
        title="SNMPv3 security level",
        description="v3 only. authPriv also encrypts the traffic.",
    )
    auth_protocol: Literal["md5", "sha", "sha224", "sha256", "sha384", "sha512"] = Field(
        default="sha",
        title="SNMPv3 authentication protocol",
        description="v3 only. Must match the device. Avoid MD5 where the device offers SHA.",
    )
    auth_key: SecretStr | None = Field(
        default=None,
        title="SNMPv3 authentication passphrase",
        description="v3 only. At least 8 characters.",
    )
    priv_protocol: Literal["des", "aes128", "aes192", "aes256"] = Field(
        default="aes128",
        title="SNMPv3 privacy protocol",
        description=(
            "v3 authPriv only. Must match the device. AES-128 is the most interoperable; avoid DES."
        ),
    )
    priv_key: SecretStr | None = Field(
        default=None,
        title="SNMPv3 privacy passphrase",
        description="v3 authPriv only. At least 8 characters.",
    )
    timeout_seconds: int = Field(
        default=2,
        ge=1,
        le=int(MAX_SNMP_TIMEOUT),
        title="Timeout per request (seconds)",
    )
    retries: int = Field(
        default=1,
        ge=0,
        le=MAX_SNMP_RETRIES,
        title="Retries per request",
    )
    include_interfaces: list[FilterPattern] = Field(
        default_factory=list,
        max_length=MAX_FILTER_PATTERNS,
        title="Only these interfaces",
        description=(
            "Optional. Patterns such as ge-0/0/* or ether*, matched against the "
            "interface name and description, ignoring case. Empty means all."
        ),
    )
    exclude_interfaces: list[FilterPattern] = Field(
        default_factory=list,
        max_length=MAX_FILTER_PATTERNS,
        title="Skip these interfaces",
        description="Optional. Same patterns; an excluded interface is skipped even if included.",
    )
    max_interfaces: int = Field(
        default=128,
        ge=1,
        le=ifmib.MAX_INTERFACES,
        title="Interfaces per poll (at most)",
        description=(
            "The lowest-numbered interfaces that pass the filter are polled, up to this "
            "many, so a large switch can't produce unbounded metrics."
        ),
    )

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return checked_host(value)

    @model_validator(mode="after")
    def _credentials_match_version(self) -> Self:
        # Messages name the field, never its value. Secrets the chosen version doesn't
        # use are ignored rather than refused, so switching version on an edit doesn't
        # require clearing the old ones first.
        if self.version in ("1", "2c"):
            if self.community is None:
                raise ValueError("a community is required for SNMP v1 and v2c")
            if len(self.community.get_secret_value()) > 255:
                raise ValueError("the community may be at most 255 characters")
            return self
        if not self.username:
            raise ValueError("an SNMPv3 user is required")
        if self.auth_key is None:
            raise ValueError("an SNMPv3 authentication passphrase is required")
        keys = [self.auth_key]
        if self.security_level == "authPriv":
            if self.priv_key is None:
                raise ValueError("authPriv needs an SNMPv3 privacy passphrase")
            keys.append(self.priv_key)
        for key in keys:
            if not 8 <= len(key.get_secret_value()) <= 256:
                raise ValueError("SNMPv3 passphrases must be 8 to 256 characters")
        return self

    def auth(self) -> SnmpV1 | SnmpV2c | SnmpV3:
        """The guarded client's credentials for the configured version."""
        if self.version in ("1", "2c"):
            if self.community is None:  # the validator already refuses this
                raise ValueError("a community is required for SNMP v1 and v2c")
            cls = SnmpV1 if self.version == "1" else SnmpV2c
            return cls(community=self.community)
        if self.username is None or self.auth_key is None:  # likewise
            raise ValueError("SNMPv3 needs a user and an authentication passphrase")
        return SnmpV3(
            username=self.username,
            auth_key=self.auth_key,
            priv_key=self.priv_key if self.security_level == "authPriv" else None,
            auth_protocol=self.auth_protocol,
            priv_protocol=self.priv_protocol,
        )

    def ifmib_options(self) -> ifmib.IfMibOptions:
        return ifmib.IfMibOptions(
            max_interfaces=self.max_interfaces,
            filter=ifmib.InterfaceFilter(
                include=tuple(self.include_interfaces), exclude=tuple(self.exclude_interfaces)
            ),
            hc_counters=self.version != "1",  # SNMPv1 can't carry Counter64
        )


def _no_response(where: str) -> str:
    # Fixed text plus the host:port as configured: safe to show (see PollFailure).
    return (
        f"no SNMP response from {where} (check the port, SNMP version, community or v3 "
        "user, the device's SNMP access list, and the firewall)"
    )


def _request_failed(where: str) -> str:
    return (
        f"SNMP request to {where} was answered with an error (check the SNMP version, "
        "and the v3 user, passphrases and protocols)"
    )


def _walk_timed_out(where: str) -> str:
    return (
        f"SNMP walk of {where} stopped answering partway (the device may be slow or "
        "have too many interfaces)"
    )


def _ascii(text: str, limit: int) -> str:
    """Device text for a test message: printable ASCII, bounded."""
    cleaned = "".join(c for c in text if c.isascii() and c.isprintable())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


class SnmpDevice(Integration):
    type_id = TYPE_ID
    display_name = "SNMP device"
    category: ClassVar[Category] = "switch"
    version = "0.1.0"
    config_schema = SnmpDeviceConfig

    @staticmethod
    def _client(ctx: InstanceContext, cfg: SnmpDeviceConfig) -> GuardedSnmpClient:
        return ctx.snmp_client(
            cfg.host,
            cfg.auth(),
            port=cfg.port,
            timeout=float(cfg.timeout_seconds),
            retries=cfg.retries,
        )

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        """One GET of sysDescr and sysName."""
        cfg = ctx.config_as(SnmpDeviceConfig)
        where = host_port(cfg.host, cfg.port)
        client = self._client(ctx, cfg)
        try:
            rows = await client.get([ifmib.SYS_DESCR, ifmib.SYS_NAME])
        except NetGuardError as exc:
            # Guard messages hold only the host as configured, an address or a fixed
            # reason (see common.connection_failure): safe to show.
            return TestResult(ok=False, message=f"refused by the network guard: {exc}")
        except SnmpTimeout:
            # Silence is what a firewall, a wrong port or a wrong community look like.
            return TestResult(ok=False, message=_no_response(where), hint=self._hint(cfg))
        except SnmpError:
            # The agent answered: a firewall rule won't help. Its text isn't shown.
            return TestResult(ok=False, message=_request_failed(where))
        finally:
            client.close()
        info = ifmib.parse_system(rows)
        if not info.readable:
            return TestResult(
                ok=False,
                message=(
                    f"{where} answered, but sysDescr and sysName are not readable (check "
                    "the SNMP view allowed for this community or user)"
                ),
            )
        name = _ascii(info.name, 64) or "?"
        descr = _ascii(info.descr, _TEST_DESCR_CHARS)
        detail = f": {descr}" if descr else ""
        return TestResult(ok=True, message=f"SNMP v{cfg.version} answer from {name}{detail}")

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        """sysDescr/sysName/sysObjectID/sysUpTime in one GET, then the IF-MIB walks.

        No answer to the first request raises :class:`SnmpNoResponse`; an error answer
        :class:`SnmpRequestFailed`; silence partway through the walks
        :class:`SnmpWalkTimedOut`. Each marks the instance ``down`` with its message.
        """
        cfg = ctx.config_as(SnmpDeviceConfig)
        where = host_port(cfg.host, cfg.port)
        client = self._client(ctx, cfg)
        try:
            with _failures(where, timeout=SnmpNoResponse(_no_response(where))):
                system = await ifmib.read_system(client)
            with _failures(where, timeout=SnmpWalkTimedOut(_walk_timed_out(where))):
                table = await ifmib.read_interfaces(client, cfg.ifmib_options())
        finally:
            client.close()
        return ifmib.system_samples(system, METRIC_PREFIX) + ifmib.interface_samples(
            table, METRIC_PREFIX
        )

    def poll_interval_seconds(self) -> int:
        # A full IF-MIB walk is a few hundred to a few thousand requests on a big switch.
        return 60

    def cards(self) -> list[CardSpec]:
        return [
            CardSpec(card_type="snmp-overview", title="SNMP device", default_size=(2, 2)),
            CardSpec(card_type="snmp-ports", title="Ports", default_size=(4, 2)),
        ]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        return [self._hint(ctx.config_as(SnmpDeviceConfig))]

    @staticmethod
    def _hint(cfg: SnmpDeviceConfig) -> FirewallHint:
        return FirewallHint(
            protocol="udp",
            port=cfg.port,
            destination=cfg.host,
            reason=(
                "The Vantage backend sends SNMP requests to this port: allow them and the "
                "agent's replies back, and allow the backend's address in the device's "
                "SNMP access list."
            ),
        )


@contextmanager
def _failures(where: str, *, timeout: PollFailure) -> Iterator[None]:
    """Turn SNMP errors into PollFailures with fixed, secret-free messages. The SNMP
    library's own error text is never shown. Guard refusals pass through (the collector
    shows the guard's own safe message)."""
    try:
        yield
    except SnmpTimeout:
        raise timeout from None
    except SnmpError:
        raise SnmpRequestFailed(_request_failed(where)) from None

"""The Integration contract (AGENTS.md Contract 1).

Every device type is a subclass of :class:`Integration`, listed in ``registry.py``.
The models and method signatures here are the contract; change them only with the
maintainer's approval.
"""

import ssl
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Literal, Self, TypeVar

import httpx
from pydantic import BaseModel, Field, SecretStr, model_validator

from app.core.net_guard import (
    DEFAULT_CONNECT_TIMEOUT,
    MAX_SNMP_RETRIES,
    MAX_SNMP_TIMEOUT,
    PING_INTERVAL,
    GuardedPingClient,
    GuardedSnmpClient,
    NetGuard,
    SnmpV1,
    SnmpV2c,
    SnmpV3,
    guarded_http_client,
)

Category = Literal[
    "firewall", "hypervisor", "storage", "switch", "tunnel", "cloud", "media", "host", "generic"
]


class TestResult(BaseModel):
    __test__: ClassVar[bool] = False  # not a pytest test class, despite the name

    ok: bool
    message: str  # human-readable, never contains secrets
    hint: "FirewallHint | None" = None


class FirewallHint(BaseModel):
    protocol: Literal["tcp", "udp", "icmp"]
    port: int | None  # required for tcp/udp; None for icmp (ICMP echo has no port)
    destination: str  # the instance host, as configured
    reason: str

    @model_validator(mode="after")
    def _port_matches_protocol(self) -> Self:
        if self.protocol == "icmp" and self.port is not None:
            raise ValueError("an icmp hint has no port")
        if self.protocol != "icmp" and self.port is None:
            raise ValueError(f"a {self.protocol} hint needs a port")
        return self


TestResult.model_rebuild()


class ScrapeTarget(BaseModel):
    address: str  # host:port
    metrics_path: str = "/metrics"
    scheme: Literal["http", "https"] = "http"


class CardSpec(BaseModel):
    card_type: str  # matches a frontend card folder name
    title: str
    default_size: tuple[int, int]  # grid width, height


class Sample(BaseModel):
    """One polled value. The collector adds the standard ``vantage_*`` labels (M2.4).

    ``name`` follows ``vantage_<type_id>_<metric>``. Integrations may add their own
    labels but never the standard ones.
    """

    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    value: float
    type: Literal["gauge", "counter"]


_Config = TypeVar("_Config", bound=BaseModel)


@dataclass(frozen=True, kw_only=True)
class InstanceContext:
    """What an integration gets for one instance: its config and guarded clients.

    Built by the backend (instance API, poll collector), never by an integration.
    Integrations open connections only through :meth:`http_client`,
    :meth:`snmp_client` and :meth:`ping_client`, which go through ``net_guard``
    (Contract 5).
    """

    instance_id: uuid.UUID
    name: str
    # The instance's config, validated against the integration's ``config_schema``,
    # with secrets decrypted into ``SecretStr`` fields. Kept out of repr regardless.
    config: BaseModel = field(repr=False)
    # The four standard vantage_* labels, filled by the poll collector (M2.4). Empty in
    # contexts built for connection tests, which don't produce metrics.
    labels: Mapping[str, str] = field(default_factory=dict)
    # Carries the SSRF policy (settings' extra CIDRs, own addresses) and the
    # integration's cloud_hosts. Built by whoever builds the context (M2.2/M2.4).
    net_guard: NetGuard = field(repr=False)

    def config_as(self, schema: type[_Config]) -> _Config:
        """The config as the integration's own schema type, for typed access."""
        if not isinstance(self.config, schema):
            raise TypeError(f"config is not a {schema.__name__}")
        return self.config

    def http_client(
        self,
        *,
        verify: bool | ssl.SSLContext = True,
        base_url: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> httpx.AsyncClient:
        """A guarded ``httpx.AsyncClient``.

        ``verify`` is ``True`` (system CAs), an ``ssl.SSLContext`` that verifies against
        a certificate the admin supplied (pinning, for self-signed devices), or ``False``
        (no verification; an explicit per-instance opt-in only). A cloud host is refused
        unless the context verifies the certificate and the hostname.
        """
        return guarded_http_client(
            self.net_guard,
            verify=verify,
            base_url=base_url,
            headers=dict(headers) if headers is not None else None,
        )

    def snmp_client(
        self,
        host: str,
        auth: SnmpV1 | SnmpV2c | SnmpV3,
        *,
        port: int = 161,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
        retries: int = 2,
    ) -> GuardedSnmpClient:
        """A guarded, read-only (GET/WALK) SNMP client.

        ``timeout`` (seconds per attempt, at most ``MAX_SNMP_TIMEOUT``) and ``retries``
        (at most ``MAX_SNMP_RETRIES``) may only be lowered; every get() or walk() still
        stops after the client's 15 s total limit.
        """
        if not 0 < timeout <= MAX_SNMP_TIMEOUT or not 0 <= retries <= MAX_SNMP_RETRIES:
            raise ValueError(
                f"SNMP timeout must be in (0, {MAX_SNMP_TIMEOUT:g}] s and retries in "
                f"[0, {MAX_SNMP_RETRIES}]"
            )
        return GuardedSnmpClient(
            self.net_guard, host, auth, port=port, timeout=timeout, retries=retries
        )

    def ping_client(
        self, host: str, *, count: int = 3, reply_timeout: float = 1.0
    ) -> GuardedPingClient:
        """A guarded ICMP echo client: private ranges only, never a cloud host, at most
        ``MAX_PING_COUNT`` packets, ``PING_INTERVAL`` seconds apart, and done within 15 s
        (DNS included, capped at ``PING_RESOLVE_TIMEOUT``)."""
        return GuardedPingClient(
            self.net_guard, host, count=count, interval=PING_INTERVAL, reply_timeout=reply_timeout
        )


class Integration(ABC):
    type_id: ClassVar[str]  # stable, e.g. "pfsense"
    display_name: ClassVar[str]
    category: ClassVar[Category]
    version: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]  # secrets typed as SecretStr
    cloud_hosts: ClassVar[tuple[str, ...]] = ()  # fixed public API hosts, if any

    @abstractmethod
    async def test_connection(self, ctx: InstanceContext) -> TestResult: ...

    def scrape_targets(self, ctx: InstanceContext) -> list[ScrapeTarget]:
        return []

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        return []

    def poll_interval_seconds(self) -> int:
        return 60

    @abstractmethod
    def cards(self) -> list[CardSpec]: ...

    @abstractmethod
    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]: ...


__all__ = [
    "CardSpec",
    "Category",
    "FirewallHint",
    "InstanceContext",
    "Integration",
    "Sample",
    "ScrapeTarget",
    "SecretStr",
    "TestResult",
]

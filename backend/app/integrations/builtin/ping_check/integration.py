"""Ping check: does a host answer ICMP echo, with how much loss, and how fast?

Reference integration (M2.7) and the template for integrations that use the guarded
ICMP ping client. Like ``http_check`` it is **polled**: Vantage pings on every poll and
exposes the result on its own ``/metrics``.

What it shows:

- a config schema with a ``host`` field (found by name by the SSRF check on save, and
  validated with the guard's own host parser, the same one used at connect time) and
  no secrets: nothing in this config is secret, and it is all stored in plaintext;
- ``poll()`` that awaits all its I/O and uses only ``ctx.ping_client()``. Integrations
  never open sockets: the client resolves the host once, checks every address with the
  SSRF guard, and pings only the checked address, over an unprivileged ICMP datagram
  socket (no raw sockets);
- ``vantage_ping_check_*`` samples without the standard labels (the collector adds them).

ICMP echo changes nothing on the target: the check is read-only by construction.
"""

from typing import ClassVar, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.net_guard import (
    MAX_PING_COUNT,
    MAX_PING_REPLY_TIMEOUT,
    MAX_PING_TOTAL_TIMEOUT,
    NetGuardError,
    PingError,
    PingResult,
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
from app.integrations.builtin.common import checked_host

TYPE_ID = "ping_check"
# Metric names: vantage_<type_id>_<metric> (Contract 1).
METRIC_LOSS = f"vantage_{TYPE_ID}_packet_loss_ratio"
METRIC_SENT = f"vantage_{TYPE_ID}_packets_sent"
METRIC_RECEIVED = f"vantage_{TYPE_ID}_packets_received"
METRIC_RTT_MIN = f"vantage_{TYPE_ID}_rtt_min_seconds"
METRIC_RTT_AVG = f"vantage_{TYPE_ID}_rtt_avg_seconds"
METRIC_RTT_MAX = f"vantage_{TYPE_ID}_rtt_max_seconds"

# The ping client sends one packet per second; this is its spacing in the budget below.
_INTERVAL_SECONDS = 1


class NoReply(Exception):
    """Not one echo request was answered. Raised by ``poll()`` so the collector marks
    the instance ``down``; the collector shows only this type name."""


class PingCheckConfig(BaseModel):
    """What the admin enters. The JSON schema of this model drives the UI form.

    Naming matters: ``host`` is found by name by the SSRF check on save (fields named
    ``host``, ``*_host``, ``url`` or ``*_url``). There are no secrets.
    """

    host: str = Field(
        title="Host",
        description=(
            "IP address or DNS name to ping. Must be on a private network (RFC 1918, "
            "CGNAT or IPv6 ULA). Stored in plaintext and shown to every user."
        ),
        max_length=253,
        examples=["10.0.0.30"],
    )
    count: int = Field(
        default=3,
        ge=1,
        le=MAX_PING_COUNT,
        title="Packets per check",
        description="Echo requests sent per check, one per second.",
    )
    timeout_seconds: int = Field(
        default=1,
        ge=1,
        le=int(MAX_PING_REPLY_TIMEOUT),
        title="Reply timeout (seconds)",
        description="How long to wait for each reply before counting it as lost.",
    )

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        # The guard's own parser (via checked_host), as at connect time: one parsing
        # path, so a host accepted here is one the guard can check.
        return checked_host(value)

    @model_validator(mode="after")
    def _fits_time_limit(self) -> Self:
        # The ping client stops after 15 s in total; refuse settings that can't finish.
        if self.count * max(_INTERVAL_SECONDS, self.timeout_seconds) > MAX_PING_TOTAL_TIMEOUT:
            raise ValueError(
                f"packets x reply timeout must be at most {MAX_PING_TOTAL_TIMEOUT:.0f} seconds"
            )
        return self


def _ms(seconds: float | None) -> str:
    return "?" if seconds is None else f"{seconds * 1000:.1f} ms"


class PingCheck(Integration):
    type_id = TYPE_ID
    display_name = "Ping check"
    category: ClassVar[Category] = "generic"
    version = "0.1.0"
    config_schema = PingCheckConfig

    @staticmethod
    async def _ping(ctx: InstanceContext) -> PingResult:
        cfg = ctx.config_as(PingCheckConfig)
        client = ctx.ping_client(cfg.host, count=cfg.count, reply_timeout=cfg.timeout_seconds)
        return await client.ping()

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        host = ctx.config_as(PingCheckConfig).host
        try:
            result = await self._ping(ctx)
        except NetGuardError as exc:
            # Guard messages hold only the host as configured, an address or a fixed
            # reason (see common.connection_failure): safe to show.
            return TestResult(ok=False, message=f"refused by the network guard: {exc}")
        except PingError as exc:
            # Fixed text plus, at most, the checked address.
            return TestResult(ok=False, message=f"ping failed: {exc}")
        # Anything else propagates: the instance API reports only its type.
        if result.received == 0:
            # TODO(maintainer): FirewallHint.protocol is "tcp" | "udp" (Contract 1), so no
            # ICMP hint can be given here. Add "icmp" (with port unused) to the contract?
            return TestResult(
                ok=False,
                message=f"no reply from {host} ({result.sent} packets sent, ICMP may be blocked)",
            )
        return TestResult(
            ok=True,
            message=(
                f"{result.received}/{result.sent} replies from {host}, "
                f"avg {_ms(result.rtt_avg)}, {result.loss_ratio:.0%} loss"
            ),
        )

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        """One ping run per poll.

        No reply at all raises :class:`NoReply` (and a guard refusal or missing ICMP
        permission raises too): the collector then marks the instance ``down`` and sets
        ``vantage_instance_up`` to 0, as ``http_check`` does when nothing answers. Partial
        loss is a result: the loss ratio and the round-trip times of the replies.
        """
        result = await self._ping(ctx)
        rtts = result.rtts
        if not rtts:
            raise NoReply
        return [
            Sample(name=METRIC_LOSS, value=result.loss_ratio, type="gauge"),
            Sample(name=METRIC_SENT, value=float(result.sent), type="gauge"),
            Sample(name=METRIC_RECEIVED, value=float(result.received), type="gauge"),
            Sample(name=METRIC_RTT_MIN, value=min(rtts), type="gauge"),
            Sample(name=METRIC_RTT_AVG, value=sum(rtts) / len(rtts), type="gauge"),
            Sample(name=METRIC_RTT_MAX, value=max(rtts), type="gauge"),
        ]

    def poll_interval_seconds(self) -> int:
        return 60

    def cards(self) -> list[CardSpec]:
        return [CardSpec(card_type="ping-check", title="Ping check", default_size=(2, 2))]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        # ICMP has no port and FirewallHint can only say tcp or udp (see the TODO in
        # test_connection), so there is nothing to hint yet.
        return []

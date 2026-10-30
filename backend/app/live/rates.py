"""Live traffic rates for the dashboard's throughput card (maintainer-approved 2026-10-30).

While at least one browser is connected to ``/ws``, every :data:`TICK_SECONDS` this reads
the traffic counters of the ports chosen on the dashboard's throughput card, and
publishes their summed rate as a ``live_rate`` message. Nothing is stored: history stays
in Prometheus at its normal resolution.

Reads go through each instance's guarded clients (``InstanceContext``), like a poll:

* SNMP (``snmp_device``, ``mikrotik_swos``): one GET of the port's two octet counters.
  The port's ifIndex comes from the instance's last poll in the metrics store.
* Scraped exporters (``pfsense``, ``linux_host``): one GET of the integration's own
  scrape target, the counters picked from it. Telegraf updates its page on its own
  interval, so between updates the last rate is kept.

Ports come only from the saved layout (admin-written), at most :data:`MAX_PORTS`; the
interface name is only compared with label values, never sent anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.hub import EventHub
from app.collectors.scheduler import PollSpec, _PollError, instance_context
from app.collectors.store import MetricsStore
from app.core.crypto import SecretBox
from app.integrations import instance_config as ic
from app.integrations.base import InstanceContext
from app.integrations.registry import Registry
from app.models import Dashboard, Instance

logger = logging.getLogger(__name__)

TICK_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 1.5
MAX_PORTS = 8
MAX_BODY_BYTES = 8 * 1024 * 1024
DASHBOARD_ID = 1
THROUGHPUT_CARD = "throughput"
USER_AGENT = "Vantage live rates"

# Scraped sources: the counter family names (the parser reports counter samples as
# ``<family>_total``, so match on the family) and the label naming the interface.
SCRAPED = {
    "pfsense": ("net_bytes_recv", "net_bytes_sent", "interface"),
    "linux_host": (
        "node_network_receive_bytes",
        "node_network_transmit_bytes",
        "device",
    ),
}
SNMP_TYPES = {"snmp_device": "vantage_snmp_device", "mikrotik_swos": "vantage_mikrotik_swos"}
# IF-MIB octet counters: 64-bit ifXTable, or 32-bit ifTable for SNMPv1.
HC_IN, HC_OUT = "1.3.6.1.2.1.31.1.1.1.6", "1.3.6.1.2.1.31.1.1.1.10"
IN_32, OUT_32 = "1.3.6.1.2.1.2.2.1.10", "1.3.6.1.2.1.2.2.1.16"
SOURCE_TYPES = {"pfsense": {"pfsense"}, "node": {"linux_host"}, "snmp": set(SNMP_TYPES)}


@dataclass(frozen=True)
class Port:
    source: str
    instance_id: uuid.UUID
    iface: str


def ports_from_layout(layout: Any) -> list[Port]:
    """The throughput card's ports (``port1``..``port8`` = ``source:instance:iface``)."""
    cards = layout.get("cards", []) if isinstance(layout, dict) else []
    for card in cards:
        if not isinstance(card, dict) or card.get("card_type") != THROUGHPUT_CARD:
            continue
        if card.get("instance_id") is not None:
            continue
        settings = card.get("settings") or {}
        ports: list[Port] = []
        for i in range(1, MAX_PORTS + 1):
            value = settings.get(f"port{i}") if isinstance(settings, dict) else None
            if not isinstance(value, str):
                continue
            source, _, rest = value.partition(":")
            instance, _, iface = rest.partition(":")
            if source not in SOURCE_TYPES or not iface:
                continue
            try:
                ports.append(Port(source, uuid.UUID(instance), iface))
            except ValueError:
                continue
        return ports
    return []


@dataclass
class _Last:
    in_octets: int
    out_octets: int
    at: float
    in_bps: float | None = None
    out_bps: float | None = None


class LiveRates:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        registry: Registry,
        secret_box: SecretBox,
        net_guard_factory: ic.NetGuardFactory,
        store: MetricsStore,
        hub: EventHub,
        tick_seconds: float = TICK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._box = secret_box
        self._guards = net_guard_factory
        self._store = store
        self._hub = hub
        self._tick = tick_seconds
        self._clock = clock
        self._last: dict[Port, _Last] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="vantage-live-rates")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            if self._hub.subscriber_count == 0:
                self._last.clear()  # rates restart cleanly when someone connects again
                continue
            try:
                await self.tick_once()
            except Exception as exc:  # never let one bad tick stop the loop
                logger.warning("live rates tick failed (%s)", type(exc).__name__)

    async def tick_once(self) -> None:
        ports, specs = self._load()
        if not ports:
            self._last.clear()
            return
        by_instance: dict[uuid.UUID, list[Port]] = {}
        for port in ports:
            by_instance.setdefault(port.instance_id, []).append(port)
        results = await asyncio.gather(
            *(self._read(specs[iid], group) for iid, group in by_instance.items() if iid in specs)
        )
        now = self._clock()
        total_in = total_out = 0.0
        counted = 0
        for counters in results:
            for port, (in_octets, out_octets) in counters.items():
                rate = self._rate(port, in_octets, out_octets, now)
                if rate is not None:
                    total_in += rate[0]
                    total_out += rate[1]
                    counted += 1
        self._last = {p: v for p, v in self._last.items() if p in set(ports)}
        if counted:
            self._hub.publish(
                {
                    "type": "live_rate",
                    "in_bps": round(total_in, 1),
                    "out_bps": round(total_out, 1),
                    "ports": counted,
                    "at": datetime.now(UTC).isoformat(),
                }
            )

    def _rate(
        self, port: Port, in_octets: int, out_octets: int, now: float
    ) -> tuple[float, float] | None:
        last = self._last.get(port)
        if last is None or in_octets < last.in_octets or out_octets < last.out_octets:
            self._last[port] = _Last(in_octets, out_octets, now)  # first read, or a reset
            return None
        if in_octets == last.in_octets and out_octets == last.out_octets:
            # Not updated since (an exporter's own interval): keep the last rate, if any.
            if last.in_bps is None or last.out_bps is None:
                return None
            return last.in_bps, last.out_bps
        elapsed = now - last.at
        if elapsed <= 0:
            return None
        in_bps = (in_octets - last.in_octets) * 8 / elapsed
        out_bps = (out_octets - last.out_octets) * 8 / elapsed
        self._last[port] = _Last(in_octets, out_octets, now, in_bps, out_bps)
        return in_bps, out_bps

    def _load(self) -> tuple[list[Port], dict[uuid.UUID, PollSpec]]:
        with self._session_factory() as db:
            dashboard = db.get(Dashboard, DASHBOARD_ID)
            ports = ports_from_layout(dashboard.layout if dashboard else None)
            if not ports:
                return [], {}
            rows = db.scalars(
                select(Instance).where(
                    Instance.id.in_({p.instance_id for p in ports}), Instance.enabled.is_(True)
                )
            ).all()
            specs = {
                row.id: PollSpec(
                    instance_id=row.id,
                    type_id=row.type_id,
                    name=row.name,
                    site=row.site,
                    updated_at=row.updated_at,
                    config=dict(row.config),
                    secrets=row.secrets,
                )
                for row in rows
            }
        # A port only counts when its source matches the instance's integration.
        ports = [
            p
            for p in ports
            if p.instance_id in specs and specs[p.instance_id].type_id in SOURCE_TYPES[p.source]
        ]
        return ports, specs

    async def _read(self, spec: PollSpec, ports: list[Port]) -> dict[Port, tuple[int, int]]:
        """Counters per port, or nothing for ports that couldn't be read this tick."""
        cls = self._registry.get(spec.type_id)
        if cls is None:
            return {}
        try:
            _secrets, ctx = instance_context(cls, spec, self._box, self._guards)
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                if spec.type_id in SNMP_TYPES:
                    return await self._read_snmp(spec, ctx, ports)
                return await self._read_scraped(cls(), spec, ctx, ports)
        except _PollError:
            return {}
        except Exception as exc:  # unreachable device, guard refusal, timeout: skip quietly
            logger.debug("live rates: %s read failed (%s)", spec.type_id, type(exc).__name__)
            return {}

    async def _read_snmp(
        self, spec: PollSpec, ctx: InstanceContext, ports: list[Port]
    ) -> dict[Port, tuple[int, int]]:
        prefix = SNMP_TYPES[spec.type_id]
        index_of: dict[str, int] = {}
        for sample in self._store.samples(spec.instance_id):
            if sample.name == f"{prefix}_if_info":
                name, index = sample.labels.get("if_name"), sample.labels.get("if_index", "")
                if name and index.isdigit():
                    index_of[name] = int(index)
        cfg: Any = ctx.config
        v1 = getattr(cfg, "version", None) == "1"
        in_oid, out_oid = (IN_32, OUT_32) if v1 else (HC_IN, HC_OUT)
        wanted = [(p, index_of[p.iface]) for p in ports if p.iface in index_of]
        if not wanted:
            return {}
        client = ctx.snmp_client(cfg.host, cfg.auth(), port=cfg.port, timeout=1.0, retries=0)
        try:
            oids = [f"{oid}.{index}" for _, index in wanted for oid in (in_oid, out_oid)]
            values = [vb.value for vb in await client.get(oids)]
        finally:
            client.close()
        counters: dict[Port, tuple[int, int]] = {}
        for i, (port, _index) in enumerate(wanted):
            in_v, out_v = values[2 * i], values[2 * i + 1]
            if isinstance(in_v, int) and isinstance(out_v, int):
                counters[port] = (in_v, out_v)
        return counters

    async def _read_scraped(
        self, integration: Any, spec: PollSpec, ctx: InstanceContext, ports: list[Port]
    ) -> dict[Port, tuple[int, int]]:
        in_name, out_name, label = SCRAPED[spec.type_id]
        targets = integration.scrape_targets(ctx)
        if not targets:
            return {}
        target = targets[0]
        url = f"{target.scheme}://{target.address}{target.metrics_path}"
        body = bytearray()
        async with (
            ctx.http_client(headers={"User-Agent": USER_AGENT}) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                return {}
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    return {}
        wanted = {p.iface: p for p in ports}
        found: dict[str, dict[str, int]] = {}
        for family in text_string_to_metric_families(body.decode("utf-8", errors="replace")):
            if family.name not in (in_name, out_name):
                continue
            for sample in family.samples:
                iface = sample.labels.get(label)
                if iface in wanted:
                    found.setdefault(iface, {})[family.name] = int(sample.value)
        return {
            wanted[iface]: (vals[in_name], vals[out_name])
            for iface, vals in found.items()
            if in_name in vals and out_name in vals
        }

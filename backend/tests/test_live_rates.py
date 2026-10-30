"""Live rates for the throughput card: which ports, the rate math, and one real read of a
pfSense Telegraf page through the guard (fake sockets, real SSRF guard)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.collectors.hub import EventHub
from app.collectors.store import MetricsStore
from app.core.crypto import SecretBox
from app.core.db import make_session_factory
from app.integrations.registry import builtin_registry
from app.live.rates import LiveRates, Port, ports_from_layout
from app.models import Dashboard, Instance
from tests.integrations.netfake import FakeNetwork, fake_guards, http_response
from tests.integrations.netfake import fake_net as fake_net
from tests.integrations.snmpfake import FakeSnmp
from tests.integrations.snmpfake import fake_snmp as fake_snmp

PF_ID = uuid.UUID("00000000-0000-4000-8000-0000000000f1")
HOST_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")


def telegraf(recv: int, sent: int) -> bytes:
    return (
        "# TYPE net_bytes_recv counter\n"
        f'net_bytes_recv{{host="fw",interface="igc0"}} {recv}\n'
        'net_bytes_recv{host="fw",interface="igc1"} 999999\n'
        "# TYPE net_bytes_sent counter\n"
        f'net_bytes_sent{{host="fw",interface="igc0"}} {sent}\n'
        'net_bytes_sent{host="fw",interface="igc1"} 999999\n'
    ).encode()


def test_ports_come_only_from_the_throughput_card() -> None:
    layout = {
        "cards": [
            {"instance_id": None, "card_type": "devices-health", "settings": {"port1": "x"}},
            {
                "instance_id": None,
                "card_type": "throughput",
                "settings": {
                    "port1": f"pfsense:{PF_ID}:igc0",
                    "port2": "bogus:abc:eth0",
                    "port3": "node:not-a-uuid:eth0",
                    "port4": f"snmp:{HOST_ID}",
                    "port9": f"node:{HOST_ID}:eth0",  # beyond the 8-port limit
                },
            },
        ]
    }
    assert ports_from_layout(layout) == [Port("pfsense", PF_ID, "igc0")]
    assert ports_from_layout(None) == []
    assert ports_from_layout({"cards": "nope"}) == []


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup(
    engine: Engine, secret_box: SecretBox
) -> Iterator[tuple[LiveRates, EventHub, Clock, Session]]:
    factory = make_session_factory(engine)
    hub = EventHub()
    clock = Clock()
    rates = LiveRates(
        session_factory=factory,
        registry=builtin_registry(),
        secret_box=secret_box,
        net_guard_factory=fake_guards,
        store=MetricsStore(),
        hub=hub,
        clock=clock,
    )
    with factory() as db:
        db.add(
            Instance(
                id=PF_ID,
                type_id="pfsense",
                name="fw",
                tags=[],
                enabled=True,
                config={"host": "10.0.0.1", "port": 9273, "scheme": "http"},
                secrets=None,
            )
        )
        db.add(
            Dashboard(
                id=1,
                name="Default",
                layout={
                    "version": 2,
                    "cards": [
                        {
                            "instance_id": None,
                            "card_type": "throughput",
                            "x": 0,
                            "y": 0,
                            "w": 3,
                            "h": 2,
                            "settings": {"port1": f"pfsense:{PF_ID}:igc0"},
                        }
                    ],
                },
            )
        )
        db.commit()
        yield rates, hub, clock, db


async def test_reads_the_chosen_port_and_publishes_its_rate(
    setup: tuple[LiveRates, EventHub, Clock, Session], fake_net: FakeNetwork
) -> None:
    rates, hub, clock, _db = setup
    sub = hub.subscribe()
    assert sub is not None
    pages = iter([telegraf(1000, 500), telegraf(1000, 500), telegraf(3500, 1500)])
    fake_net.serve("10.0.0.1", 9273, lambda _request: http_response(body=next(pages)))

    await rates.tick_once()  # first read: nothing to compare with yet
    assert sub.queue.empty()

    clock.now += 2
    await rates.tick_once()  # Telegraf hasn't updated: no rate known yet
    assert sub.queue.empty()

    clock.now += 2  # 4 s after the first read
    await rates.tick_once()
    message = sub.queue.get_nowait()
    assert message["type"] == "live_rate"
    assert (message["in_bps"], message["out_bps"], message["ports"]) == (5000.0, 2000.0, 1)
    assert set(message) == {"type", "in_bps", "out_bps", "ports", "at"}  # numbers only

    # Read only: GET of the scrape target, nothing else.
    assert {(r.method, r.target) for r in fake_net.requests} == {("GET", "/metrics")}


async def test_a_refused_or_down_device_publishes_nothing(
    setup: tuple[LiveRates, EventHub, Clock, Session], fake_net: FakeNetwork
) -> None:
    rates, hub, clock, db = setup
    sub = hub.subscribe()
    assert sub is not None
    # Nothing serves 10.0.0.1: connection refused.
    await rates.tick_once()
    clock.now += 2
    await rates.tick_once()
    assert sub.queue.empty()

    # A loopback host is refused by the guard before any connection.
    row = db.get(Instance, PF_ID)
    assert row is not None
    row.config = {"host": "127.0.0.1", "port": 9273, "scheme": "http"}
    db.commit()
    await rates.tick_once()
    assert sub.queue.empty()
    assert all(r.address != "127.0.0.1" for r in fake_net.requests)


# --- SNMP: ifIndex from the last poll, GET only, idle = 0 --------------------------------

SNMP_ID = uuid.UUID("00000000-0000-4000-8000-0000000000b1")
HC_IN = "1.3.6.1.2.1.31.1.1.1.6.3"
HC_OUT = "1.3.6.1.2.1.31.1.1.1.10.3"


async def test_snmp_port_read_and_idle_counters_mean_zero(
    engine: Engine, secret_box: SecretBox, fake_snmp: FakeSnmp
) -> None:
    from pysnmp.proto.rfc1902 import Counter64

    from app.integrations import instance_config as ic
    from app.integrations.base import Sample

    factory = make_session_factory(engine)
    store = MetricsStore()
    hub = EventHub()
    clock = Clock()
    rates = LiveRates(
        session_factory=factory,
        registry=builtin_registry(),
        secret_box=secret_box,
        net_guard_factory=fake_guards,
        store=store,
        hub=hub,
        clock=clock,
    )
    with factory() as db:
        db.add(
            Instance(
                id=SNMP_ID,
                type_id="snmp_device",
                name="sw",
                tags=[],
                enabled=True,
                config={"host": "10.0.0.50", "version": "2c"},
                secrets=ic.encrypt(secret_box, SNMP_ID, {"community": "example-community"}),
            )
        )
        port = f"snmp:{SNMP_ID}:eth2"
        db.add(
            Dashboard(
                id=1,
                name="Default",
                layout={
                    "cards": [
                        {
                            "instance_id": None,
                            "card_type": "throughput",
                            "settings": {"port1": port},
                        }
                    ]
                },
            )
        )
        db.commit()
    # The last poll told us eth2 is ifIndex 3.
    store.track(SNMP_ID, {})
    store.record_success(
        SNMP_ID,
        "snmp_device",
        [
            Sample(
                name="vantage_snmp_device_if_info",
                labels={"if_index": "3", "if_name": "eth2", "if_descr": "", "if_alias": ""},
                value=1.0,
                type="gauge",
            )
        ],
        at=datetime.now(UTC),
        duration_seconds=0.1,
    )
    agent = fake_snmp.agent(
        "10.0.0.50",
        {HC_IN: Counter64(1000), HC_OUT: Counter64(2000)},
        community="example-community",
    )
    sub = hub.subscribe()
    assert sub is not None

    await rates.tick_once()
    clock.now += 2
    agent.mib.update({HC_IN: Counter64(1500), HC_OUT: Counter64(2250)})
    await rates.tick_once()
    message = sub.queue.get_nowait()
    assert (message["in_bps"], message["out_bps"]) == (2000.0, 1000.0)

    clock.now += 2  # no traffic since: the counters didn't move
    await rates.tick_once()
    message = sub.queue.get_nowait()
    assert (message["in_bps"], message["out_bps"]) == (0.0, 0.0)

    assert {c.op for c in fake_snmp.calls} == {"get"}
    assert all(set(c.oids) == {HC_IN, HC_OUT} for c in fake_snmp.calls)


async def test_node_exporter_counters_with_total_suffix(
    setup: tuple[LiveRates, EventHub, Clock, Session], fake_net: FakeNetwork
) -> None:
    rates, hub, clock, db = setup
    db.add(
        Instance(
            id=HOST_ID,
            type_id="linux_host",
            name="host",
            tags=[],
            enabled=True,
            config={"host": "10.0.0.20", "port": 9100},
            secrets=None,
        )
    )
    dashboard = db.get(Dashboard, 1)
    assert dashboard is not None
    dashboard.layout = {
        "cards": [
            {
                "instance_id": None,
                "card_type": "throughput",
                "settings": {"port1": f"node:{HOST_ID}:eth0"},
            }
        ]
    }
    db.commit()

    def page(rx: int, tx: int) -> bytes:
        return (
            "# TYPE node_network_receive_bytes_total counter\n"
            f'node_network_receive_bytes_total{{device="eth0"}} {rx}\n'
            "# TYPE node_network_transmit_bytes_total counter\n"
            f'node_network_transmit_bytes_total{{device="eth0"}} {tx}\n'
            "# TYPE node_cpu_seconds_total counter\n"
            'node_cpu_seconds_total{cpu="0",mode="idle"} 1\n'
        ).encode()

    pages = iter([page(0, 0), page(250, 500)])
    fake_net.serve("10.0.0.20", 9100, lambda _request: http_response(body=next(pages)))
    sub = hub.subscribe()
    assert sub is not None
    await rates.tick_once()
    clock.now += 2
    await rates.tick_once()
    message = sub.queue.get_nowait()
    assert (message["in_bps"], message["out_bps"]) == (1000.0, 2000.0)


def test_an_exporter_rate_goes_stale(
    setup: tuple[LiveRates, EventHub, Clock, Session],
) -> None:
    rates, _hub, _clock, _db = setup
    port = Port("pfsense", PF_ID, "igc0")
    assert rates._rate(port, 0, 0, 0.0, live=False) is None
    assert rates._rate(port, 1000, 1000, 10.0, live=False) == (800.0, 800.0)
    assert rates._rate(port, 1000, 1000, 20.0, live=False) == (800.0, 800.0)  # kept
    assert rates._rate(port, 1000, 1000, 41.0, live=False) is None  # stale after 30 s
    assert rates._rate(port, 10, 10, 43.0, live=False) is None  # counter reset

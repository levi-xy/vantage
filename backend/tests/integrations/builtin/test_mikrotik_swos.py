"""MikroTik SwOS profile (M4e): schema, port naming, connection test, poll parsing, health,
the collector and API end to end, and the security assertions: only GET and WALK, only
documented OIDs, only to the checked private address, the community never in a result,
message, log line, status, metric or API response.

SwOS can't be emulated in our lab, so ``fixtures/snmp_swos_*.snmpwalk`` are hand-written
from MikroTik's public MIB (see the TODO(maintainer) notes in them and in the docs).
"""

import inspect
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from pydantic import ValidationError
from sqlalchemy import Engine

from app.collectors.scheduler import PollScheduler, PollSpec
from app.collectors.store import MetricsStore, make_registry
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.core.net_guard import NetGuardError, SnmpV1, SnmpV2c
from app.integrations import instance_config as ic
from app.integrations.base import Sample, TestResult
from app.integrations.builtin import ifmib
from app.integrations.builtin.mikrotik_swos import MikroTikSwOS, MikroTikSwOSConfig
from app.integrations.builtin.mikrotik_swos import integration as swos_module
from app.integrations.builtin.snmp_device import (
    SnmpNoResponse,
    SnmpRequestFailed,
    SnmpWalkTimedOut,
)
from app.integrations.registry import Registry
from app.integrations.schema import config_json_schema
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import fake_guards, make_ctx
from tests.integrations.snmpfake import FakeSnmp, load_walk
from tests.test_auth import do_setup, make_client

HOST = "snmp.example.internal"
ADDRESS = "10.0.0.40"
COMMUNITY = "example-community"
PREFIX = "vantage_mikrotik_swos"
FULL = "snmp_swos_full"
IFTABLE_ONLY = "snmp_swos_iftable_only"

UDP_HINT = {
    "protocol": "udp",
    "port": 161,
    "destination": HOST,
    "reason": (
        "The Vantage backend sends SNMP requests to this port: allow them and the switch's "
        "replies back, and allow the backend's address in SwOS under SNMP (allowed "
        "addresses) and in any firewall in between."
    ),
}

DOCUMENTED_OIDS = (
    set(ifmib.SYSTEM_OIDS)
    | set(ifmib.IF_TABLE.values())
    | set(ifmib.IF_X_TABLE.values())
    | set(swos_module.HEALTH_OIDS)
)


def cfg(**overrides: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"host": HOST, "community": COMMUNITY}
    out.update(overrides)
    return out


def serve(fake: FakeSnmp, walk: str = FULL, **creds: Any) -> None:
    fake.agent(ADDRESS, load_walk(walk), **(creds or {"community": COMMUNITY}))


async def run_test(config: dict[str, Any]) -> TestResult:
    return await MikroTikSwOS().test_connection(make_ctx(MikroTikSwOS, config))


async def run_poll(config: dict[str, Any]) -> list[Sample]:
    return await MikroTikSwOS().poll(make_ctx(MikroTikSwOS, config))


def assert_read_only(fake: FakeSnmp) -> None:
    assert fake.calls
    assert fake.set_attempts == []
    for call in fake.calls:
        assert call.op in {"get", "walk"}
        assert call.address == ADDRESS
        assert set(call.oids) <= DOCUMENTED_OIDS, call.oids


def assert_no_secret(*texts: object) -> None:
    for text in texts:
        assert COMMUNITY not in str(text)


def by_name(samples: list[Sample]) -> dict[tuple[str, str], Sample]:
    return {(s.name.removeprefix(PREFIX + "_"), s.labels.get("if_name", "")): s for s in samples}


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults_and_secrets() -> None:
    c = MikroTikSwOSConfig.model_validate(cfg())
    assert (c.port, c.version, c.timeout_seconds, c.retries, c.max_interfaces) == (
        161,
        "2c",
        2,
        1,
        64,
    )
    assert ic.secret_field_keys(MikroTikSwOSConfig) == {"community"}
    stored, secrets = ic.split(c)
    assert secrets == {"community": COMMUNITY}
    assert_no_secret(stored, repr(c), c.model_dump_json(), repr(c.auth()))


def test_served_schema_flags_the_community_write_only_and_has_no_v3() -> None:
    schema = config_json_schema(MikroTikSwOSConfig)
    assert schema["properties"]["community"]["writeOnly"] is True
    assert "writeOnly" not in schema["properties"]["host"]
    assert schema["properties"]["version"]["enum"] == ["1", "2c"]
    assert not {"username", "auth_key", "priv_key"} & schema["properties"].keys()
    assert_no_secret(schema)


@pytest.mark.parametrize(
    "config",
    [cfg(version="1"), cfg(host="10.0.0.41"), cfg(include_interfaces=["Port1*", "SFP*"])],
)
def test_schema_accepts(config: dict[str, Any]) -> None:
    MikroTikSwOSConfig.model_validate(config)


@pytest.mark.parametrize(
    "config",
    [
        {"host": HOST},  # no community
        cfg(community=""),
        cfg(community="c" * 256),
        cfg(version="3"),
        cfg(version="2"),
        cfg(port=0),
        cfg(timeout_seconds=6),
        cfg(retries=3),
        cfg(max_interfaces=0),
        cfg(host="http://10.0.0.40/"),
        cfg(exclude_interfaces=[""]),
        cfg(include_interfaces=[f"p{i}" for i in range(33)]),
    ],
)
def test_schema_rejects(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as info:
        MikroTikSwOSConfig.model_validate(config)
    assert_no_secret(*(e["msg"] for e in info.value.errors()))
    assert_no_secret(*(e.get("input") for e in info.value.errors() if e["loc"] != ("community",)))


def test_auth_per_version() -> None:
    assert isinstance(MikroTikSwOSConfig.model_validate(cfg(version="1")).auth(), SnmpV1)
    assert isinstance(MikroTikSwOSConfig.model_validate(cfg()).auth(), SnmpV2c)


# --- port naming -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "descr", "expected"),
    [
        ("", "Port1", "port1"),
        ("", "Port 12", "port12"),
        ("", "SFP+9", "sfpplus9"),
        ("", "sfp-sfpplus1", "sfpplus1"),
        ("", "SFP2", "sfp2"),
        ("ether3", "something else", "ether3"),  # ifName wins
        ("", "QSFP+1", "qsfpplus1"),
        ("", "Uplink to core (10G)", "port7"),  # a renamed port: never free text
        ("", "Port1; drop table", "port7"),
        ("", "", "port7"),
        ("Port9999", "", "port7"),  # not a plausible port number
    ],
)
def test_port_name(name: str, descr: str, expected: str) -> None:
    port = ifmib.Interface(index=7, name=name, descr=descr)
    assert swos_module.swos_port_name(port) == expected


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    result = await run_test(cfg())
    assert result == TestResult(
        ok=True, message="SNMP v2c answer from example-swos: CSS610-EXAMPLE"
    )
    [call] = fake_snmp.calls
    assert call.op == "get" and call.oids == (ifmib.SYS_DESCR, ifmib.SYS_NAME)
    assert (call.timeout, call.retries, call.mp_model) == (2.0, 1, 1)
    assert_read_only(fake_snmp)


async def test_connection_ok_v1(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    result = await run_test(cfg(version="1"))
    assert result.ok and result.message.startswith("SNMP v1 answer from example-swos")
    assert fake_snmp.calls[0].mp_model == 0


@pytest.mark.parametrize(
    ("config", "port", "agent"),
    [
        (cfg(), 161, False),
        (cfg(community="example-wrong-community"), 161, True),
        (cfg(port=1161), 1161, True),
    ],
)
async def test_connection_timeout_gives_udp_hint(
    fake_snmp: FakeSnmp, config: dict[str, Any], port: int, agent: bool
) -> None:
    if agent:
        serve(fake_snmp)
    result = await run_test(config)
    assert result.ok is False
    assert result.message.startswith(f"no SNMP response from {HOST}:{port} ")
    assert result.hint is not None
    assert result.hint.model_dump() == {**UDP_HINT, "port": port}
    assert_no_secret(result)
    assert "example-wrong-community" not in str(result)


async def test_connection_error_answer_has_no_hint(fake_snmp: FakeSnmp) -> None:
    mib = load_walk(FULL)
    del mib[ifmib.SYS_NAME]  # v1: one missing object fails the whole GET (noSuchName)
    fake_snmp.agent(ADDRESS, mib, community=COMMUNITY)
    result = await run_test(cfg(version="1"))
    assert result.ok is False and result.hint is None
    assert result.message.startswith(f"SNMP request to {HOST}:161 was answered with an error")


async def test_connection_unreadable_system_group(fake_snmp: FakeSnmp) -> None:
    mib = load_walk(FULL)
    del mib[ifmib.SYS_DESCR], mib[ifmib.SYS_NAME]
    fake_snmp.agent(ADDRESS, mib, community=COMMUNITY)
    result = await run_test(cfg())
    assert result.ok is False and "not readable" in result.message and result.hint is None


async def test_connection_refused_by_guard(fake_snmp: FakeSnmp) -> None:
    result = await run_test(cfg(host="metadata.example.internal"))
    assert result.ok is False and result.message.startswith("refused by the network guard")
    assert fake_snmp.calls == []


# --- poll parsing ----------------------------------------------------------------------


async def test_poll_full(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    samples = await run_poll(cfg())
    got = by_name(samples)
    assert got[("interfaces", "")].value == 6.0
    assert got[("interfaces_polled", "")].value == 6.0
    assert got[("uptime_seconds", "")].labels == {"source": "sysUpTime"}
    assert got[("uptime_seconds", "")].value == 86400.0
    info = got[("info", "")]
    assert info.labels == {
        "sys_name": "example-swos",
        "sys_descr": "CSS610-EXAMPLE",
        "sys_object_id": "1.3.6.1.4.1.14988.2",
    }
    # Value series carry the stable SwOS-style names; the renamed port is port3.
    names = {k[1] for k in got if k[0] == "if_in_bytes"}
    assert names == {"port1", "port2", "port3", "port4", "sfpplus9", "sfpplus10"}
    assert got[("if_in_bytes", "port1")].value == 5000001000.0
    assert got[("if_in_bytes", "port1")].type == "counter"
    assert got[("if_counters_64bit", "port1")].value == 1.0
    assert got[("if_oper_status", "port2")].value == 2.0
    assert got[("if_speed_bits_per_second", "port1")].value == 1e9
    assert got[("if_speed_bits_per_second", "sfpplus9")].value == 10e9
    # The admin's own text is on if_info only.
    renamed = [s for s in samples if s.name == f"{PREFIX}_if_info" and s.labels["if_index"] == "3"]
    assert renamed[0].labels == {
        "if_index": "3",
        "if_name": "port3",
        "if_descr": "Uplink to core (10G)",
        "if_alias": "to core",
    }
    assert_read_only(fake_snmp)


async def test_poll_health_scaled(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    got = by_name(await run_poll(cfg()))
    assert got[("voltage_volts", "")].value == pytest.approx(24.1)
    assert got[("temperature_celsius", "")].value == pytest.approx(41.5)
    assert got[("temperature_celsius", "")].type == "gauge"


async def test_poll_without_health_objects_emits_none(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp, IFTABLE_ONLY)
    samples = await run_poll(cfg())
    assert not [s for s in samples if s.name.endswith(("voltage_volts", "temperature_celsius"))]
    assert {c.oids for c in fake_snmp.calls if c.op == "get"} >= {
        (swos_module.MTXR_HL_VOLTAGE,),
        (swos_module.MTXR_HL_TEMPERATURE,),
    }


async def test_poll_v1_health_absent_is_not_a_failure(fake_snmp: FakeSnmp) -> None:
    # On v1 a missing object is a noSuchName error answer; the poll goes on.
    serve(fake_snmp, IFTABLE_ONLY)
    got = by_name(await run_poll(cfg(version="1")))
    assert ("voltage_volts", "") not in got and got[("interfaces", "")].value == 6.0


async def test_poll_v1_reads_health_and_uses_32bit_counters(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    got = by_name(await run_poll(cfg(version="1")))
    assert got[("temperature_celsius", "")].value == pytest.approx(41.5)
    assert got[("if_counters_64bit", "port1")].value == 0.0
    assert got[("if_in_bytes", "port1")].value == 1000.0


async def test_poll_iftable_only_names_by_descr(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp, IFTABLE_ONLY)
    got = by_name(await run_poll(cfg()))
    names = sorted(k[1] for k in got if k[0] == "if_in_bytes")
    assert names == ["port1", "port2", "port3", "port4", "sfpplus10", "sfpplus9"]
    assert got[("if_speed_bits_per_second", "port1")].value == 1e9


async def test_poll_filters_and_cap(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    got = by_name(await run_poll(cfg(include_interfaces=["SFP*"])))
    assert got[("interfaces_polled", "")].value == 2.0
    got = by_name(await run_poll(cfg(exclude_interfaces=["SFP*"], max_interfaces=2)))
    assert {k[1] for k in got if k[0] == "if_oper_status"} == {"port1", "port2"}


async def test_poll_free_text_only_on_info_series(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    free = {"if_alias", "if_descr", "sys_name", "sys_descr"}
    for sample in await run_poll(cfg()):
        if sample.name.endswith("_info"):
            assert sample.value == 1.0 and sample.type == "gauge"
        else:
            assert not free & sample.labels.keys(), sample.name


@pytest.mark.parametrize("name", ["snmp_swos_full", "snmp_swos_iftable_only"])
async def test_poll_sample_names_use_the_type_prefix(fake_snmp: FakeSnmp, name: str) -> None:
    serve(fake_snmp, name)
    for sample in await run_poll(cfg()):
        assert sample.name.startswith(PREFIX + "_"), sample.name


# --- poll failures ---------------------------------------------------------------------


async def test_poll_no_response(fake_snmp: FakeSnmp) -> None:
    with pytest.raises(SnmpNoResponse) as info:
        await run_poll(cfg())
    assert str(info.value).startswith(f"no SNMP response from {HOST}:161")
    assert_no_secret(info.value, repr(info.value))


async def test_poll_wrong_community(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    with pytest.raises(SnmpNoResponse):
        await run_poll(cfg(community="example-wrong-community"))


async def test_poll_v1_error_answer(fake_snmp: FakeSnmp) -> None:
    mib = load_walk(FULL)
    del mib[ifmib.SYS_OBJECT_ID]
    fake_snmp.agent(ADDRESS, mib, community=COMMUNITY)
    with pytest.raises(SnmpRequestFailed) as info:
        await run_poll(cfg(version="1"))
    assert_no_secret(info.value)


async def test_poll_silence_partway(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    fake_snmp.agents[ADDRESS].answer_limit = 8
    with pytest.raises(SnmpWalkTimedOut):
        await run_poll(cfg())


async def test_poll_silence_during_health_keeps_the_poll(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    agent = fake_snmp.agents[ADDRESS]
    await run_poll(cfg())
    # The two health GETs are the last two requests; go silent right before them.
    agent.answer_limit, agent.answered = agent.answered - 2, 0
    got = by_name(await run_poll(cfg()))
    assert got[("interfaces_polled", "")].value == 6.0
    assert ("temperature_celsius", "") not in got and ("voltage_volts", "") not in got


async def test_poll_refused_by_guard(fake_snmp: FakeSnmp) -> None:
    with pytest.raises(NetGuardError):
        await run_poll(cfg(host="metadata.example.internal"))
    assert fake_snmp.calls == []


# --- through the poll collector --------------------------------------------------------


async def collect(engine: Engine, config: dict[str, Any]) -> tuple[MetricsStore, PollSpec]:
    box = SecretBox(TEST_MASTER_KEY)
    instance_id = uuid.uuid4()
    stored, secrets = ic.split(MikroTikSwOSConfig.model_validate(config))
    spec = PollSpec(
        instance_id=instance_id,
        type_id="mikrotik_swos",
        name="sw-1",
        site="lab",
        updated_at=utcnow(),
        config=stored,
        secrets=ic.encrypt(box, instance_id, secrets),
    )
    store = MetricsStore()
    scheduler = PollScheduler(
        session_factory=make_session_factory(engine),
        registry=Registry([MikroTikSwOS]),
        secret_box=box,
        net_guard_factory=fake_guards,
        store=store,
    )
    store.track(spec.instance_id, spec.labels)
    await scheduler.poll_once(spec, MikroTikSwOS())
    return store, spec


async def test_collector_exposes_metrics(
    fake_snmp: FakeSnmp, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_snmp)
    store, spec = await collect(engine, cfg())
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "up", status
    text = generate_latest(make_registry(store)).decode()
    std = (
        f'vantage_instance="sw-1",vantage_instance_id="{spec.instance_id}",'
        'vantage_integration="mikrotik_swos",vantage_site="lab"'
    )
    assert f'{PREFIX}_if_in_bytes_total{{if_index="9",if_name="sfpplus9",{std}}}' in text
    assert f"{PREFIX}_temperature_celsius{{{std}}} 41.5" in text
    assert f"vantage_instance_up{{{std}}} 1.0" in text
    assert_no_secret(text, caplog.text)


async def test_collector_marks_silent_switch_down(
    fake_snmp: FakeSnmp, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store, spec = await collect(engine, cfg())
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error is not None and status.error.startswith("no SNMP response from")
    assert_no_secret(status.error, caplog.text, generate_latest(make_registry(store)).decode())


# --- API -------------------------------------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_snmp: FakeSnmp) -> Iterator[TestClient]:
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def test_api_create_test_read(admin: TestClient, fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    bodies = []
    r = admin.post("/api/v1/instances/test", json={"type_id": "mikrotik_swos", "config": cfg()})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    bodies.append(r.text)
    created = admin.post(
        "/api/v1/instances", json={"type_id": "mikrotik_swos", "name": "sw-1", "config": cfg()}
    )
    assert created.status_code == 201, created.text
    bodies.append(created.text)
    body = created.json()
    assert "community" not in body["config"] and body["secrets_set"] == {"community": True}
    for path in ("/api/v1/instances", f"/api/v1/instances/{body['id']}", "/api/v1/integrations"):
        r = admin.get(path)
        assert r.status_code == 200
        bodies.append(r.text)
    tested = admin.post(f"/api/v1/instances/{body['id']}/test")
    assert tested.status_code == 200 and tested.json()["ok"] is True
    bodies.append(tested.text)
    cards = admin.get(f"/api/v1/instances/{body['id']}/cards").json()
    assert [c["card_type"] for c in cards] == ["snmp-overview", "snmp-ports"]
    assert_no_secret(*bodies)
    assert {c.address for c in fake_snmp.calls} == {ADDRESS}


@pytest.mark.parametrize("host", ["metadata.example.internal", "127.0.0.1", "192.0.2.40"])
def test_api_refuses_denied_host_on_save(admin: TestClient, fake_snmp: FakeSnmp, host: str) -> None:
    r = admin.post(
        "/api/v1/instances",
        json={"type_id": "mikrotik_swos", "name": "sw-1", "config": cfg(host=host)},
    )
    assert r.status_code == 422, r.text
    assert fake_snmp.calls == []
    assert_no_secret(r.text)


def test_api_serves_udp_hint_on_timeout(admin: TestClient, fake_snmp: FakeSnmp) -> None:
    r = admin.post("/api/v1/instances/test", json={"type_id": "mikrotik_swos", "config": cfg()})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and r.json()["hint"] == UDP_HINT
    assert_no_secret(r.text)


# --- static parts and read-only --------------------------------------------------------


def test_firewall_hints_cards_and_interval() -> None:
    ctx = make_ctx(MikroTikSwOS, cfg())
    assert [h.model_dump() for h in MikroTikSwOS().firewall_hints(ctx)] == [UDP_HINT]
    assert MikroTikSwOS().poll_interval_seconds() == 60
    assert [c.card_type for c in MikroTikSwOS().cards()] == ["snmp-overview", "snmp-ports"]
    assert (MikroTikSwOS.type_id, MikroTikSwOS.category) == ("mikrotik_swos", "switch")


async def test_no_write_endpoints_called(fake_snmp: FakeSnmp) -> None:
    """Every request of a test and a poll is a GET or a WALK to the checked address, for
    documented OIDs only, and nothing reaches pysnmp's SET paths."""
    serve(fake_snmp)
    await run_test(cfg())
    await run_poll(cfg())
    serve(fake_snmp, IFTABLE_ONLY)
    await run_poll(cfg(version="1"))
    assert_read_only(fake_snmp)
    assert {c.op for c in fake_snmp.calls} == {"get", "walk"}


def test_no_write_operation_in_snmp_code() -> None:
    source = inspect.getsource(swos_module)
    for word in ("set_cmd", "setCmd", "SetRequest", "pysnmp", "socket", "_auth_data"):
        assert word not in source, word

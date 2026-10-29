"""SNMP device (M4d): schema, connection test, IF-MIB poll parsing, collector and API end
to end, and the security assertions: only GET and WALK, only the OIDs documented, only
to the checked private address, and the community and v3 keys never in a result,
message, log line, status, metric or API response.

No real network: ``fake_snmp`` keeps the real SSRF guard and the real guarded SNMP
client and replaces only pysnmp's request functions with a fake agent loaded from
hand-written ``fixtures/*.snmpwalk`` files.
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
from pysnmp.proto.rfc1902 import Counter32, OctetString
from sqlalchemy import Engine

from app.collectors.scheduler import PollScheduler, PollSpec
from app.collectors.store import MetricsStore, make_registry
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.core.net_guard import SnmpV1, SnmpV2c, SnmpV3
from app.integrations import instance_config as ic
from app.integrations.base import Sample, TestResult
from app.integrations.builtin import ifmib
from app.integrations.builtin.snmp_device import (
    SnmpDevice,
    SnmpDeviceConfig,
    SnmpNoResponse,
    SnmpRequestFailed,
    SnmpWalkTimedOut,
)
from app.integrations.builtin.snmp_device import integration as snmp_module
from app.integrations.registry import Registry
from app.integrations.schema import config_json_schema
from tests.conftest import TEST_MASTER_KEY
from tests.integrations.netfake import fake_guards, make_ctx
from tests.integrations.snmpfake import FakeSnmp, load_walk
from tests.test_auth import do_setup, make_client

HOST = "snmp.example.internal"
ADDRESS = "10.0.0.40"
COMMUNITY = "example-community"
USER = "example-monitor"
AUTH_KEY = "example-auth-key"
PRIV_KEY = "example-priv-key"
SECRETS = (COMMUNITY, AUTH_KEY, PRIV_KEY)
PREFIX = "vantage_snmp_device"

UDP_HINT = {
    "protocol": "udp",
    "port": 161,
    "destination": HOST,
    "reason": (
        "The Vantage backend sends SNMP requests to this port: allow them and the agent's "
        "replies back, and allow the backend's address in the device's SNMP access list."
    ),
}

# Every OID the integration may request: the system group GET and the IF-MIB columns.
DOCUMENTED_OIDS = (
    set(ifmib.SYSTEM_OIDS) | set(ifmib.IF_TABLE.values()) | set(ifmib.IF_X_TABLE.values())
)


def v2c(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"host": HOST, "community": COMMUNITY}
    cfg.update(overrides)
    return cfg


def v3(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "host": HOST,
        "version": "3",
        "username": USER,
        "auth_key": AUTH_KEY,
        "priv_key": PRIV_KEY,
    }
    cfg.update(overrides)
    return cfg


def serve(fake: FakeSnmp, walk: str = "snmp_netsnmp_linux", **creds: Any) -> None:
    creds = creds or {"community": COMMUNITY}
    fake.agent(ADDRESS, load_walk(walk), **creds)


def serve_v3(fake: FakeSnmp, walk: str = "snmp_netsnmp_linux", **overrides: Any) -> None:
    creds = {"v3_user": USER, "v3_auth_key": AUTH_KEY, "v3_priv_key": PRIV_KEY, **overrides}
    fake.agent(ADDRESS, load_walk(walk), **creds)


async def run_test(cfg: dict[str, Any]) -> TestResult:
    return await SnmpDevice().test_connection(make_ctx(SnmpDevice, cfg))


async def run_poll(cfg: dict[str, Any]) -> list[Sample]:
    return await SnmpDevice().poll(make_ctx(SnmpDevice, cfg))


def assert_read_only(fake: FakeSnmp) -> None:
    """Only GET and WALK (the guarded client has nothing else), only documented OIDs,
    only to the checked address."""
    assert fake.calls
    for call in fake.calls:
        assert call.op in {"get", "walk"}
        assert call.address == ADDRESS
        assert set(call.oids) <= DOCUMENTED_OIDS, call.oids


def assert_no_secret(*texts: object) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in str(text)


def by_name(samples: list[Sample]) -> dict[tuple[str, str], Sample]:
    """``{(metric suffix, if_name or ""): sample}`` for compact assertions."""
    return {(s.name.removeprefix(PREFIX + "_"), s.labels.get("if_name", "")): s for s in samples}


# --- schema ----------------------------------------------------------------------------


def test_schema_defaults() -> None:
    cfg = SnmpDeviceConfig.model_validate(v2c())
    assert (cfg.port, cfg.version, cfg.timeout_seconds, cfg.retries) == (161, "2c", 2, 1)
    assert (cfg.max_interfaces, cfg.include_interfaces, cfg.exclude_interfaces) == (128, [], [])
    assert (cfg.security_level, cfg.auth_protocol, cfg.priv_protocol) == (
        "authPriv",
        "sha",
        "aes128",
    )


def test_secrets_are_the_community_and_the_v3_keys() -> None:
    assert ic.secret_field_keys(SnmpDeviceConfig) == {"community", "auth_key", "priv_key"}
    config, secrets = ic.split(SnmpDeviceConfig.model_validate(v3(community=COMMUNITY)))
    assert secrets == {"community": COMMUNITY, "auth_key": AUTH_KEY, "priv_key": PRIV_KEY}
    assert config["username"] == USER  # the v3 user is plaintext config, as documented
    assert_no_secret(config)


def test_served_schema_flags_secrets_write_only() -> None:
    schema = config_json_schema(SnmpDeviceConfig)
    for key in ("community", "auth_key", "priv_key"):
        assert schema["properties"][key]["writeOnly"] is True
    for key in ("host", "username", "version", "include_interfaces"):
        assert "writeOnly" not in schema["properties"][key]
    assert schema["properties"]["version"]["enum"] == ["1", "2c", "3"]
    assert_no_secret(schema)


@pytest.mark.parametrize(
    "cfg",
    [
        v2c(version="1"),
        v2c(port=1161, timeout_seconds=5, retries=2, max_interfaces=512),
        v3(security_level="authNoPriv", priv_key=None),
        v3(auth_protocol="sha512", priv_protocol="aes256"),
        v3(auth_protocol="md5", priv_protocol="des"),
        v2c(include_interfaces=["eth*", "Gi1/0/*"], exclude_interfaces=["lo"]),
        v2c(host="10.0.0.40"),
        v2c(host="[fd00::40]"),
    ],
)
def test_schema_accepts(cfg: dict[str, Any]) -> None:
    SnmpDeviceConfig.model_validate(cfg)


@pytest.mark.parametrize(
    "cfg",
    [
        v2c(community=None),
        v2c(version="1", community=None),
        v2c(version="2"),
        v2c(port=0),
        v2c(port=65536),
        v2c(timeout_seconds=0),
        v2c(timeout_seconds=6),
        v2c(retries=3),
        v2c(max_interfaces=0),
        v2c(max_interfaces=513),
        v2c(include_interfaces=["x" * 65]),
        v2c(exclude_interfaces=[""]),
        v2c(include_interfaces=[f"eth{i}" for i in range(33)]),
        v2c(host="http://10.0.0.40/"),
        v2c(host="10.0.0.40:161"),
        v2c(community="c" * 256),
        v3(username=None),
        v3(username="has space"),
        v3(username="u" * 33),
        v3(auth_key=None),
        v3(priv_key=None),  # authPriv is the default
        v3(auth_key="short"),
        v3(priv_key="short"),
        v3(auth_protocol="sha1"),
        v3(priv_protocol="3des"),
        v3(security_level="noAuthNoPriv"),
    ],
)
def test_schema_rejects(cfg: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as info:
        SnmpDeviceConfig.model_validate(cfg)
    # Validation errors are shown to the admin: they never repeat a secret.
    assert_no_secret(*(e["msg"] for e in info.value.errors()))


def test_auth_for_each_version() -> None:
    v1_auth = SnmpDeviceConfig.model_validate(v2c(version="1")).auth()
    assert isinstance(v1_auth, SnmpV1)
    assert v1_auth.community.get_secret_value() == COMMUNITY
    assert isinstance(SnmpDeviceConfig.model_validate(v2c()).auth(), SnmpV2c)
    auth = SnmpDeviceConfig.model_validate(v3(auth_protocol="sha256")).auth()
    assert isinstance(auth, SnmpV3)
    assert (auth.username, auth.auth_protocol, auth.priv_protocol) == (USER, "sha256", "aes128")
    assert auth.priv_key is not None and auth.priv_key.get_secret_value() == PRIV_KEY
    # authNoPriv ignores a stored privacy key (left over from an earlier authPriv config).
    no_priv = SnmpDeviceConfig.model_validate(v3(security_level="authNoPriv")).auth()
    assert isinstance(no_priv, SnmpV3) and no_priv.priv_key is None


def test_unused_secrets_are_ignored_not_refused() -> None:
    # Switching version on an edit keeps the other version's stored secrets (PATCH
    # omits them): they must not make the config invalid.
    SnmpDeviceConfig.model_validate(v2c(username=USER, auth_key=AUTH_KEY, priv_key=PRIV_KEY))
    SnmpDeviceConfig.model_validate(v3(community=COMMUNITY))


def test_repr_holds_no_secret() -> None:
    cfg = SnmpDeviceConfig.model_validate(v3(community=COMMUNITY))
    assert_no_secret(repr(cfg), str(cfg), repr(cfg.auth()), cfg.model_dump_json())


# --- test_connection -------------------------------------------------------------------


async def test_connection_ok_v2c(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    result = await run_test(v2c())
    assert result == TestResult(
        ok=True,
        message=(
            "SNMP v2c answer from example-switch: Linux example-switch 6.6.0-example #1 SMP x86_64"
        ),
    )
    [call] = fake_snmp.calls
    assert call.op == "get"
    assert call.oids == (ifmib.SYS_DESCR, ifmib.SYS_NAME)
    assert (call.address, call.port, call.mp_model) == (ADDRESS, 161, 1)
    assert (call.timeout, call.retries) == (2.0, 1)  # the configured values, not defaults
    assert_read_only(fake_snmp)


async def test_connection_ok_v1(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    result = await run_test(v2c(version="1"))
    assert result.ok and result.message.startswith("SNMP v1 answer from example-switch")
    assert fake_snmp.calls[0].mp_model == 0


@pytest.mark.parametrize("level", ["authPriv", "authNoPriv"])
async def test_connection_ok_v3(fake_snmp: FakeSnmp, level: str) -> None:
    serve_v3(fake_snmp, v3_priv_key=PRIV_KEY if level == "authPriv" else None)
    result = await run_test(v3(security_level=level))
    assert result.ok, result.message
    assert fake_snmp.calls[0].mp_model == 3
    assert_no_secret(result)


async def test_connection_long_sysdescr_is_cut_and_one_line(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp, "snmp_cisco_ios")
    result = await run_test(v2c())
    assert result.ok
    assert result.message.startswith(
        "SNMP v2c answer from example-switch.example.internal: Cisco IOS Software"
    )
    assert result.message.endswith("...")
    assert "\r" not in result.message and "\n" not in result.message


@pytest.mark.parametrize(
    ("cfg", "port", "agent"),
    [
        (v2c(), 161, False),  # nothing answers at all
        (v2c(community="example-wrong-community"), 161, True),  # agents ignore it
        (v2c(port=1161), 1161, True),  # the agent listens on 161
    ],
)
async def test_connection_timeout_gives_udp_hint(
    fake_snmp: FakeSnmp, cfg: dict[str, Any], port: int, agent: bool
) -> None:
    if agent:
        serve(fake_snmp)
    result = await run_test(cfg)
    assert result.ok is False
    where = f"{HOST}:{port}"
    assert result.message == (
        f"no SNMP response from {where} (check the port, SNMP version, community or v3 "
        "user, the device's SNMP access list, and the firewall)"
    )
    assert result.hint is not None
    assert result.hint.model_dump() == {**UDP_HINT, "port": port}
    assert_no_secret(result)
    assert "example-wrong-community" not in str(result)


@pytest.mark.parametrize(
    "creds",
    [
        {"v3_user": "example-other-user"},  # unknownUserName
        {"v3_auth_key": "example-other-auth-key"},  # wrongDigest
        {"v3_priv_key": "example-other-priv-key"},  # decryptionError
    ],
)
async def test_connection_v3_error_report_has_no_hint(
    fake_snmp: FakeSnmp, creds: dict[str, str]
) -> None:
    serve_v3(fake_snmp, **creds)
    result = await run_test(v3())
    assert result.ok is False
    assert result.message == (
        f"SNMP request to {HOST}:161 was answered with an error (check the SNMP version, "
        "and the v3 user, passphrases and protocols)"
    )
    assert result.hint is None  # the agent answered: a firewall rule won't help
    assert_no_secret(result)


async def test_connection_restricted_view(fake_snmp: FakeSnmp) -> None:
    mib = load_walk("snmp_netsnmp_linux")
    del mib[ifmib.SYS_DESCR], mib[ifmib.SYS_NAME]
    fake_snmp.agent(ADDRESS, mib, community=COMMUNITY)
    result = await run_test(v2c())
    assert result.ok is False
    assert "sysDescr and sysName are not readable" in result.message
    assert result.hint is None


@pytest.mark.parametrize(
    "host", ["metadata.example.internal", "127.0.0.1", "169.254.169.254", "192.0.2.40", "::1"]
)
async def test_connection_refused_by_guard(fake_snmp: FakeSnmp, host: str) -> None:
    serve(fake_snmp)
    result = await run_test(v2c(host=host))
    assert result.ok is False
    assert result.message.startswith("refused by the network guard")
    assert result.hint is None
    assert fake_snmp.calls == []


async def test_connection_unknown_host(fake_snmp: FakeSnmp) -> None:
    result = await run_test(v2c(host="missing.example.internal"))
    assert result.ok is False and result.message.startswith("refused by the network guard")
    assert fake_snmp.calls == []


async def test_connection_logs_no_secret(
    fake_snmp: FakeSnmp, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve_v3(fake_snmp, v3_auth_key="example-other-auth-key")
    await run_test(v3())
    await run_test(v2c())  # no v2c agent: timeout
    assert_no_secret(caplog.text)


# --- poll parsing ----------------------------------------------------------------------


async def test_poll_netsnmp_linux(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    samples = await run_poll(v2c())
    got = by_name(samples)
    info = got[("info", "")]
    assert info.labels == {
        "sys_name": "example-switch",
        "sys_descr": "Linux example-switch 6.6.0-example #1 SMP x86_64",
        "sys_object_id": "1.3.6.1.4.1.8072.3.2.10",
    }
    assert got[("uptime_seconds", "")].value == 86400.0
    assert got[("interfaces", "")].value == 4.0
    assert got[("interfaces_polled", "")].value == 4.0
    eth0 = {"if_index": "2", "if_name": "eth0"}
    expected = {
        "if_in_bytes": (5_000_000_000.0, "counter"),  # 64-bit counter, beyond 2^32
        "if_out_bytes": (10_000_000_000.0, "counter"),
        "if_counters_64bit": (1.0, "gauge"),
        "if_oper_status": (1.0, "gauge"),
        "if_admin_status": (1.0, "gauge"),
        "if_in_errors": (3.0, "counter"),
        "if_out_errors": (0.0, "counter"),
        "if_in_discards": (12.0, "counter"),
        "if_out_discards": (1.0, "counter"),
        "if_speed_bits_per_second": (1e9, "gauge"),
    }
    for suffix, (value, kind) in expected.items():
        sample = got[(suffix, "eth0")]
        assert (sample.value, sample.type, sample.labels) == (value, kind, eth0), suffix
    assert got[("if_info", "eth0")].labels == {
        **eth0,
        "if_descr": "eth0",
        "if_alias": "uplink to example-core",
    }
    assert got[("if_oper_status", "eth1")].value == 7.0  # lowerLayerDown
    assert got[("if_admin_status", "eth2")].value == 2.0
    assert got[("if_speed_bits_per_second", "eth2")].value == 0.0
    # No sample carries a standard label: the collector adds those.
    for sample in samples:
        assert not any(key.startswith("vantage_") for key in sample.labels)
        assert sample.name.startswith(PREFIX + "_")
    # All counters were 64-bit: the 32-bit octet columns were never walked.
    assert ifmib.IF_TABLE["in_octets"] not in fake_snmp.walked()
    assert ifmib.IF_TABLE["out_octets"] not in fake_snmp.walked()
    assert_read_only(fake_snmp)


async def test_poll_reads_exactly_the_documented_oids(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp, "snmp_cisco_ios")  # one interface without HC: every column is read
    await run_poll(v2c())
    [get] = [c for c in fake_snmp.calls if c.op == "get"]
    assert set(get.oids) == set(ifmib.SYSTEM_OIDS)
    assert set(fake_snmp.walked()) == set(ifmib.IF_TABLE.values()) | set(ifmib.IF_X_TABLE.values())
    # Every walk is bounded: the first by the hard cap, the rest by the rows it found.
    walks = [c for c in fake_snmp.calls if c.op == "walk"]
    assert walks[0].max_rows == ifmib.MAX_WALK_ROWS
    assert {c.max_rows for c in walks[1:]} == {5}


async def test_poll_cisco_hc_fallback_per_interface(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp, "snmp_cisco_ios")
    got = by_name(await run_poll(v2c()))
    # Null0 has no ifHC*Octets: its counters fall back to the 32-bit columns...
    assert got[("if_in_bytes", "Nu0")].value == 0.0
    assert got[("if_out_bytes", "Nu0")].value == 42.0
    assert got[("if_counters_64bit", "Nu0")].value == 0.0
    # ...while the others keep their 64-bit values.
    assert got[("if_in_bytes", "Gi1/0/1")].value == 7_294_967_296.0
    assert got[("if_counters_64bit", "Gi1/0/1")].value == 1.0
    assert got[("if_in_bytes", "Gi1/0/1")].labels == {"if_index": "10001", "if_name": "Gi1/0/1"}
    assert got[("if_info", "Te1/1/1")].labels["if_descr"] == "TenGigabitEthernet1/1/1"
    # ifHighSpeed beats the saturated ifSpeed; ifSpeed is used where ifHighSpeed is 0.
    assert got[("if_speed_bits_per_second", "Te1/1/1")].value == 10e9
    assert got[("if_speed_bits_per_second", "Gi1/0/2")].value == 10e6
    assert got[("if_speed_bits_per_second", "Nu0")].value == 4_294_967_295.0
    # The multi-line sysDescr is one line in the label, and bounded.
    descr = got[("info", "")].labels["sys_descr"]
    assert "\r" not in descr and "\n" not in descr
    assert descr.startswith("Cisco IOS Software, Example Software")
    assert len(descr) <= ifmib.MAX_SYS_DESCR
    assert got[("uptime_seconds", "")].value == pytest.approx(1_234_567.89)


async def test_poll_routeros(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp, "snmp_routeros")
    got = by_name(await run_poll(v2c()))
    assert got[("if_info", "ether1")].labels["if_alias"] == "example WAN"
    assert got[("if_in_bytes", "sfp-sfpplus1")].value == 12000.0
    assert got[("if_speed_bits_per_second", "sfp-sfpplus1")].value == 10e9
    assert got[("if_oper_status", "ether2")].value == 2.0
    assert got[("if_in_bytes", "bridge")].labels["if_index"] == "7"


async def test_poll_without_ifxtable(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp, "snmp_no_ifxtable")
    got = by_name(await run_poll(v2c()))
    # No ifName: the port is named by ifDescr (NUL padding dropped). 32-bit counters.
    assert got[("if_in_bytes", "port 1")].value == 4_294_967_000.0
    assert got[("if_counters_64bit", "port 1")].value == 0.0
    assert got[("if_info", "port 2")].labels == {
        "if_index": "2",
        "if_name": "port 2",
        "if_descr": "port 2",
        "if_alias": "",
    }
    # ifSpeed, including a link under 1 Mbit/s.
    assert got[("if_speed_bits_per_second", "port 1")].value == 100e6
    assert got[("if_speed_bits_per_second", "port 2")].value == 64000.0
    # ifName came back empty, so no other ifXTable column was walked.
    walked = fake_snmp.walked()
    assert ifmib.IF_X_TABLE["name"] in walked
    for key in ("alias", "high_speed", "hc_in_octets", "hc_out_octets"):
        assert ifmib.IF_X_TABLE[key] not in walked


async def test_poll_v1_uses_32bit_counters(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    got = by_name(await run_poll(v2c(version="1")))
    assert all(call.mp_model == 0 for call in fake_snmp.calls)
    # SNMPv1 can't carry Counter64: the HC columns are not even asked for.
    assert ifmib.IF_X_TABLE["hc_in_octets"] not in fake_snmp.walked()
    assert got[("if_in_bytes", "eth0")].value == 705_032_704.0
    assert got[("if_counters_64bit", "eth0")].value == 0.0
    assert got[("if_speed_bits_per_second", "eth0")].value == 1e9  # ifHighSpeed via v1


async def test_poll_v3(fake_snmp: FakeSnmp) -> None:
    serve_v3(fake_snmp)
    got = by_name(await run_poll(v3()))
    assert got[("if_in_bytes", "eth0")].value == 5_000_000_000.0
    assert {call.mp_model for call in fake_snmp.calls} == {3}


async def test_poll_interface_cap(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp, "snmp_cisco_ios")
    samples = await run_poll(v2c(max_interfaces=2))
    got = by_name(samples)
    assert got[("interfaces", "")].value == 5.0
    assert got[("interfaces_polled", "")].value == 2.0
    # The lowest ifIndex values are kept.
    assert {s.labels["if_index"] for s in samples if "if_index" in s.labels} == {"1", "10001"}


async def test_poll_walk_cap_bounds_a_huge_table(fake_snmp: FakeSnmp) -> None:
    mib = {
        f"{ifmib.IF_TABLE['descr']}.{i}": OctetString(f"port{i}".encode()) for i in range(1, 1500)
    }
    mib.update({f"{ifmib.IF_TABLE['in_octets']}.{i}": Counter32(i) for i in range(1, 1500)})
    mib[ifmib.SYS_NAME] = OctetString(b"example-switch")
    fake_snmp.agent(ADDRESS, mib, community=COMMUNITY)
    samples = await run_poll(v2c(max_interfaces=ifmib.MAX_INTERFACES))
    got = by_name(samples)
    assert got[("interfaces", "")].value == ifmib.MAX_WALK_ROWS
    assert got[("interfaces_polled", "")].value == ifmib.MAX_INTERFACES
    assert all(
        c.max_rows and c.max_rows <= ifmib.MAX_WALK_ROWS for c in fake_snmp.calls if c.op == "walk"
    )
    assert len(samples) < 10_000  # the collector's per-poll limit


@pytest.mark.parametrize(
    ("include", "exclude", "expected"),
    [
        ([], [], {"Vl1", "Gi1/0/1", "Gi1/0/2", "Te1/1/1", "Nu0"}),
        (["gi*"], [], {"Gi1/0/1", "Gi1/0/2"}),  # ifName, case-insensitive
        (["GigabitEthernet*"], [], {"Gi1/0/1", "Gi1/0/2"}),  # ifDescr
        (["*Ethernet*"], ["Gi1/0/2"], {"Gi1/0/1", "Te1/1/1"}),  # exclude wins
        ([], ["Null*", "vlan*"], {"Gi1/0/1", "Gi1/0/2", "Te1/1/1"}),
        (["Gi1/0/[12]"], [], {"Gi1/0/1", "Gi1/0/2"}),
        (["nothing-matches"], [], set()),
    ],
)
async def test_poll_filters(
    fake_snmp: FakeSnmp, include: list[str], exclude: list[str], expected: set[str]
) -> None:
    serve(fake_snmp, "snmp_cisco_ios")
    samples = await run_poll(v2c(include_interfaces=include, exclude_interfaces=exclude))
    assert {s.labels["if_name"] for s in samples if "if_name" in s.labels} == expected
    assert by_name(samples)[("interfaces", "")].value == 5.0
    if not expected:  # nothing selected: no per-interface column is walked
        assert fake_snmp.walked() == [
            ifmib.IF_TABLE["descr"],
            ifmib.IF_X_TABLE["name"],
            ifmib.IF_X_TABLE["alias"],
        ]


async def test_poll_filter_then_cap(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp, "snmp_cisco_ios")
    samples = await run_poll(v2c(exclude_interfaces=["vl*"], max_interfaces=1))
    assert {s.labels["if_name"] for s in samples if "if_name" in s.labels} == {"Gi1/0/1"}
    got = by_name(samples)
    assert (got[("interfaces", "")].value, got[("interfaces_polled", "")].value) == (5.0, 1.0)


async def test_poll_device_text_is_cleaned_and_bounded(fake_snmp: FakeSnmp) -> None:
    mib = load_walk("snmp_netsnmp_linux")
    mib[f"{ifmib.IF_X_TABLE['alias']}.2"] = OctetString(
        b"line one\r\nline\ttwo \xe2\x80\xae\x01" + b"x" * 300
    )
    mib[f"{ifmib.IF_X_TABLE['name']}.3"] = OctetString(b"\xff\xfeeth1")  # not UTF-8
    mib[ifmib.SYS_NAME] = OctetString(b"")
    fake_snmp.agent(ADDRESS, mib, community=COMMUNITY)
    got = by_name(await run_poll(v2c()))
    alias = got[("if_info", "eth0")].labels["if_alias"]
    assert alias.startswith("line one line two x")
    assert len(alias) == ifmib.MAX_LABEL_TEXT
    assert all(c.isprintable() for c in alias)
    assert ("if_in_bytes", "��eth1") in got  # replacement characters, no crash
    assert got[("info", "")].labels["sys_name"] == ""


async def test_poll_ignores_malformed_rows(fake_snmp: FakeSnmp) -> None:
    mib = load_walk("snmp_no_ifxtable")
    mib[f"{ifmib.IF_TABLE['descr']}.3.1"] = OctetString(b"two-part index")  # not an ifIndex
    mib[f"{ifmib.IF_TABLE['in_octets']}.2"] = OctetString(b"not a number")
    fake_snmp.agent(ADDRESS, mib, community=COMMUNITY)
    got = by_name(await run_poll(v2c()))
    assert got[("interfaces", "")].value == 2.0
    assert ("if_in_bytes", "port 2") not in got  # a non-numeric counter is skipped
    assert ("if_out_bytes", "port 2") in got


async def test_poll_system_only_agent(fake_snmp: FakeSnmp) -> None:
    # A view that hides IF-MIB: the system samples are still reported, nothing else walked.
    mib = {k: v for k, v in load_walk("snmp_routeros").items() if k.startswith("1.3.6.1.2.1.1.")}
    fake_snmp.agent(ADDRESS, mib, community=COMMUNITY)
    got = by_name(await run_poll(v2c()))
    assert got[("info", "")].labels["sys_name"] == "example-switch"
    assert (got[("interfaces", "")].value, got[("interfaces_polled", "")].value) == (0.0, 0.0)
    assert fake_snmp.walked() == [ifmib.IF_TABLE["descr"]]


async def test_poll_no_response_raises(fake_snmp: FakeSnmp) -> None:
    with pytest.raises(SnmpNoResponse) as info:
        await run_poll(v2c())
    assert str(info.value).startswith(f"no SNMP response from {HOST}:161")
    assert_no_secret(info.value, repr(info.value))
    assert info.value.__cause__ is None and info.value.__suppress_context__


async def test_poll_wrong_community_is_no_response(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    with pytest.raises(SnmpNoResponse):
        await run_poll(v2c(community="example-wrong-community"))


async def test_poll_v3_error_report_raises(fake_snmp: FakeSnmp) -> None:
    serve_v3(fake_snmp, v3_auth_key="example-other-auth-key")
    with pytest.raises(SnmpRequestFailed) as info:
        await run_poll(v3())
    assert_no_secret(info.value)


async def test_poll_v1_nosuchname_on_get_raises(fake_snmp: FakeSnmp) -> None:
    mib = load_walk("snmp_no_ifxtable")
    del mib[ifmib.SYS_OBJECT_ID]  # v1: one missing object fails the whole GET
    fake_snmp.agent(ADDRESS, mib, community=COMMUNITY)
    with pytest.raises(SnmpRequestFailed):
        await run_poll(v2c(version="1"))


async def test_poll_silence_partway_raises(fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    fake_snmp.agents[ADDRESS].answer_limit = 8  # the GET and a few walk rows, then silence
    with pytest.raises(SnmpWalkTimedOut) as info:
        await run_poll(v2c())
    assert str(info.value).startswith(f"SNMP walk of {HOST}:161 stopped answering partway")


async def test_poll_refused_by_guard(fake_snmp: FakeSnmp) -> None:
    from app.core.net_guard import NetGuardError

    with pytest.raises(NetGuardError):
        await run_poll(v2c(host="metadata.example.internal"))
    assert fake_snmp.calls == []


# --- the reusable IF-MIB helper (for device profiles, M4e) -----------------------------


async def test_ifmib_profile_extension(fake_snmp: FakeSnmp) -> None:
    """A profile can read extra ifIndex-indexed columns and name ports its own way."""
    serve(fake_snmp)
    ifmib_mtu = "1.3.6.1.2.1.2.2.1.4"
    options = ifmib.IfMibOptions(
        max_interfaces=8,
        extra_columns={"mtu": ifmib_mtu},
        port_name=lambda i: f"Port {i.index}",
    )
    ctx = make_ctx(SnmpDevice, v2c())
    client = ctx.snmp_client(HOST, ctx.config_as(SnmpDeviceConfig).auth())
    try:
        table = await ifmib.read_interfaces(client, options)
    finally:
        client.close()
    assert [i.port_name for i in table.interfaces] == ["Port 1", "Port 2", "Port 3", "Port 4"]
    assert [i.extra["mtu"] for i in table.interfaces] == [65536, 1500, 1500, 1500]
    samples = ifmib.interface_samples(table, "vantage_example_profile")
    assert {s.name for s in samples} >= {
        "vantage_example_profile_if_in_bytes",
        "vantage_example_profile_if_info",
    }
    assert ifmib_mtu in fake_snmp.walked()


def test_ifmib_options_cap() -> None:
    with pytest.raises(ValueError):
        ifmib.IfMibOptions(max_interfaces=ifmib.MAX_INTERFACES + 1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"eth0", "eth0"),
        (b"  a\r\n\tb  ", "a b"),
        (b"pad\x00\x00", "pad"),
        ("‮evil", "evil"),
        (12, ""),
        (None, ""),
    ],
)
def test_clean_text(value: object, expected: str) -> None:
    assert ifmib.clean_text(value) == expected


# --- through the poll collector --------------------------------------------------------


async def collect(
    engine: Engine, cfg: dict[str, Any], *, site: str | None
) -> tuple[MetricsStore, PollSpec]:
    box = SecretBox(TEST_MASTER_KEY)
    instance_id = uuid.uuid4()
    stored, secrets = ic.split(SnmpDeviceConfig.model_validate(cfg))
    spec = PollSpec(
        instance_id=instance_id,
        type_id="snmp_device",
        name="sw-1",
        site=site,
        updated_at=utcnow(),
        config=stored,
        secrets=ic.encrypt(box, instance_id, secrets),
    )
    store = MetricsStore()
    scheduler = PollScheduler(
        session_factory=make_session_factory(engine),
        registry=Registry([SnmpDevice]),
        secret_box=box,
        net_guard_factory=fake_guards,
        store=store,
    )
    store.track(spec.instance_id, spec.labels)
    await scheduler.poll_once(spec, SnmpDevice())
    return store, spec


@pytest.mark.parametrize(("site", "label"), [("lab", "lab"), (None, "")])
async def test_collector_exposes_samples_with_standard_labels(
    fake_snmp: FakeSnmp,
    engine: Engine,
    caplog: pytest.LogCaptureFixture,
    site: str | None,
    label: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    serve(fake_snmp)
    store, spec = await collect(engine, v2c(), site=site)
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "up", status
    text = generate_latest(make_registry(store)).decode()
    std = (
        f'vantage_instance="sw-1",vantage_instance_id="{spec.instance_id}",'
        f'vantage_integration="snmp_device",vantage_site="{label}"'
    )
    # Counters are exposed with _total; the four standard labels are added.
    assert f'{PREFIX}_if_in_bytes_total{{if_index="2",if_name="eth0",{std}}} 5e+09' in text
    assert f"# TYPE {PREFIX}_if_in_errors_total counter" in text
    assert f"# TYPE {PREFIX}_if_oper_status gauge" in text
    assert f"{PREFIX}_uptime_seconds{{{std}}} 86400.0" in text
    assert f"vantage_instance_up{{{std}}} 1.0" in text
    assert f"vantage_poll_dropped_samples{{{std}}} 0.0" in text
    assert_no_secret(text, caplog.text)


async def test_collector_marks_silent_device_down(
    fake_snmp: FakeSnmp, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store, spec = await collect(engine, v2c(), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error == (await run_test(v2c())).message  # reads as test_connection does
    text = generate_latest(make_registry(store)).decode()
    assert f"{PREFIX}_if_in_bytes" not in text
    assert_no_secret(status, text, caplog.text)


async def test_collector_v3_error_status(
    fake_snmp: FakeSnmp, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    serve_v3(fake_snmp, v3_user="example-other-user")
    store, spec = await collect(engine, v3(community=COMMUNITY), site=None)
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error is not None and "answered with an error" in status.error
    assert_no_secret(status, caplog.text)


async def test_collector_reports_guard_refusal(fake_snmp: FakeSnmp, engine: Engine) -> None:
    store, spec = await collect(engine, v2c(host="metadata.example.internal"), site="lab")
    status = store.status(spec.instance_id)
    assert status is not None and status.state == "down"
    assert status.error is not None and status.error.startswith("refused by the network guard")
    assert fake_snmp.calls == []


# --- through the instance API ----------------------------------------------------------


@pytest.fixture
def admin(engine: Engine, db_url: str, fake_snmp: FakeSnmp) -> Iterator[TestClient]:
    # The built-in registry, as in production; only DNS and the SNMP requests are fake.
    with make_client(db_url, net_guard_factory=fake_guards) as client:
        me = do_setup(client)
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        yield client


def test_api_create_test_read_and_update(admin: TestClient, fake_snmp: FakeSnmp) -> None:
    serve(fake_snmp)
    bodies = []
    r = admin.post("/api/v1/instances/test", json={"type_id": "snmp_device", "config": v2c()})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    bodies.append(r.text)
    created = admin.post(
        "/api/v1/instances", json={"type_id": "snmp_device", "name": "sw-1", "config": v2c()}
    )
    assert created.status_code == 201, created.text
    bodies.append(created.text)
    body = created.json()
    assert "community" not in body["config"]
    assert body["secrets_set"]["community"] is True
    assert body["secrets_set"]["auth_key"] is False
    instance_id = body["id"]
    for path in ("/api/v1/instances", f"/api/v1/instances/{instance_id}"):
        r = admin.get(path)
        assert r.status_code == 200
        bodies.append(r.text)
    tested = admin.post(f"/api/v1/instances/{instance_id}/test")
    assert tested.status_code == 200 and tested.json()["ok"] is True
    bodies.append(tested.text)
    cards = admin.get(f"/api/v1/instances/{instance_id}/cards")
    assert cards.json() == [
        {"card_type": "snmp-overview", "title": "SNMP device", "default_size": [2, 2]},
        {"card_type": "snmp-ports", "title": "Ports", "default_size": [4, 2]},
    ]
    # Switch to v3 on an edit: the stored community stays (unused), the keys are added.
    patched = admin.patch(
        f"/api/v1/instances/{instance_id}",
        json={"config": v3()},
    )
    assert patched.status_code == 200, patched.text
    bodies.append(patched.text)
    assert patched.json()["secrets_set"] == {
        "community": True,
        "auth_key": True,
        "priv_key": True,
    }
    schema = admin.get("/api/v1/integrations")
    bodies.append(schema.text)
    assert_no_secret(*bodies)
    assert {c.address for c in fake_snmp.calls} == {ADDRESS}


@pytest.mark.parametrize("host", ["metadata.example.internal", "127.0.0.1", "192.0.2.40"])
def test_api_refuses_denied_host_on_save(admin: TestClient, fake_snmp: FakeSnmp, host: str) -> None:
    r = admin.post(
        "/api/v1/instances",
        json={"type_id": "snmp_device", "name": "sw-1", "config": v2c(host=host)},
    )
    assert r.status_code == 422, r.text
    assert fake_snmp.calls == []
    assert_no_secret(r.text)


def test_api_validation_error_holds_no_secret(admin: TestClient) -> None:
    r = admin.post(
        "/api/v1/instances",
        json={"type_id": "snmp_device", "name": "sw-1", "config": v3(priv_key="short")},
    )
    assert r.status_code == 422
    assert_no_secret(r.text)
    assert "short" not in r.text


def test_api_serves_udp_hint_on_timeout(admin: TestClient, fake_snmp: FakeSnmp) -> None:
    r = admin.post("/api/v1/instances/test", json={"type_id": "snmp_device", "config": v2c()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["hint"] == UDP_HINT
    assert_no_secret(r.text)


# --- static parts ----------------------------------------------------------------------


def test_firewall_hints_cards_and_interval() -> None:
    ctx = make_ctx(SnmpDevice, v2c())
    assert [h.model_dump() for h in SnmpDevice().firewall_hints(ctx)] == [UDP_HINT]
    [hint] = SnmpDevice().firewall_hints(make_ctx(SnmpDevice, v2c(host="[fd00::40]", port=1161)))
    assert (hint.protocol, hint.port, hint.destination) == ("udp", 1161, "fd00::40")
    assert SnmpDevice().poll_interval_seconds() == 60
    assert [c.card_type for c in SnmpDevice().cards()] == ["snmp-overview", "snmp-ports"]
    assert (SnmpDevice.type_id, SnmpDevice.category) == ("snmp_device", "switch")


def test_no_write_operation_in_snmp_code() -> None:
    """Hard rule 2: nothing in this integration or the IF-MIB helper could send a SET.
    (The guarded client itself has only get/walk: see test_snmp_is_read_only.)"""
    for module in (snmp_module, ifmib):
        source = inspect.getsource(module)
        for word in ("set_cmd", "setCmd", "SetRequest", "pysnmp", "socket", "_auth_data"):
            assert word not in source, (module.__name__, word)

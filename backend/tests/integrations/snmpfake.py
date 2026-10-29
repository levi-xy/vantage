"""A fake SNMP agent for integration tests.

Replaces only pysnmp's ``get_cmd`` and ``walk_cmd`` inside ``net_guard``, so the real
guard (DNS through :data:`tests.integrations.netfake.DNS`, address checks) and the real
:class:`GuardedSnmpClient` run unchanged. Agents are keyed by the **checked** address
the client connects to, like ``fake_net``'s routes.

An agent's MIB is loaded from ``snmpwalk -On`` style text (see ``fixtures/*.snmpwalk``):

    .1.3.6.1.2.1.1.5.0 = STRING: "example-switch"
    .1.3.6.1.2.1.2.2.1.8.2 = INTEGER: up(1)

It behaves like a real agent where the tests need it to:

- v1/v2c with a wrong community: silence (the client times out);
- v3 with an unknown user or a wrong key: an error report;
- v1 cannot carry Counter64: such objects are invisible to v1 (skipped by GETNEXT,
  ``noSuchName`` on GET, which fails the whole PDU as on a real v1 agent);
- a missing object: ``noSuchObject`` on v2c/v3 GET; a walk stops at its subtree's end.

Faults: ``answer_limit`` (silence after N requests), ``delay`` (seconds per answer) and
``broken_walk`` (``"repeat"``: the first row again, forever; ``"backwards"``: rows in
decreasing order), the last ignoring ``maxRows`` as a buggy agent would.

Every request is recorded in :attr:`FakeSnmp.calls`. The fixture also traps every way
pysnmp could send a SET (``set_cmd`` and the SET command generator): reaching one fails
the test, at the call and again at teardown, even if the caller swallowed the error
(Hard rule 2).
"""

import asyncio
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pysnmp.hlapi.v3arch.asyncio as hlapi_asyncio
import pytest
from pyasn1.type.base import Asn1Item
from pysnmp.entity.rfc3413 import cmdgen as rfc3413_cmdgen
from pysnmp.hlapi.v3arch.asyncio import cmdgen as hlapi_cmdgen
from pysnmp.proto import errind
from pysnmp.proto.rfc1902 import (
    Counter32,
    Counter64,
    Gauge32,
    Integer32,
    IpAddress,
    ObjectName,
    OctetString,
    TimeTicks,
)
from pysnmp.proto.rfc1905 import NoSuchObject

from app.core import net_guard

FIXTURES = Path(__file__).parent / "builtin" / "fixtures"


def _key(oid: str) -> tuple[int, ...]:
    return tuple(int(part) for part in oid.strip(".").split("."))


_LINE = re.compile(r"^(\.?[0-9.]+) = ([A-Za-z0-9-]+): ?(.*)$")
_PAREN_NUMBER = re.compile(r"\((\d+)\)")


def _value(kind: str, text: str) -> Asn1Item:
    text = text.strip()
    if kind == "STRING":
        if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        # ASCII with backslash escapes (\r\n, \x00); anything else as Hex-STRING.
        return OctetString(text.encode("ascii").decode("unicode_escape").encode("latin-1"))
    if kind == "Hex-STRING":
        return OctetString(bytes.fromhex(text))
    if kind == "OID":
        return ObjectName(text.lstrip("."))
    if kind == "IpAddress":
        return IpAddress(text)
    paren = _PAREN_NUMBER.search(text)
    number = int(paren.group(1)) if paren else int(text.split()[0])
    types: dict[str, Any] = {
        "INTEGER": Integer32,
        "Counter32": Counter32,
        "Counter64": Counter64,
        "Gauge32": Gauge32,
        "Timeticks": TimeTicks,
    }
    return types[kind](number)  # type: ignore[no-any-return]


def load_walk(name: str) -> dict[str, Asn1Item]:
    """Parse ``fixtures/<name>.snmpwalk``. ``#`` lines and blank lines are skipped."""
    mib: dict[str, Asn1Item] = {}
    for line in (FIXTURES / f"{name}.snmpwalk").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if match is None:
            raise ValueError(f"bad fixture line: {line!r}")
        oid, kind, text = match.groups()
        mib[oid.lstrip(".")] = _value(kind, text)
    return mib


@dataclass
class FakeAgent:
    mib: dict[str, Asn1Item]
    port: int = 161  # a request to another port gets silence
    community: str | None = None
    v3_user: str | None = None
    v3_auth_key: str | None = None
    v3_priv_key: str | None = None
    # After this many answered requests, the agent goes silent (a timeout mid-poll).
    answer_limit: int | None = None
    answered: int = 0
    delay: float = 0.0
    broken_walk: Literal["repeat", "backwards"] | None = None


@dataclass(frozen=True)
class SnmpCall:
    op: Literal["get", "walk"]
    oids: tuple[str, ...]
    address: str
    port: int
    timeout: float
    retries: int
    mp_model: int  # 0 = v1, 1 = v2c, 3 = v3
    max_rows: int | None = None


class _ErrorStatus:
    """What pysnmp hands back as a PDU error status (truthy, with ``prettyPrint``)."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __bool__(self) -> bool:
        return True

    def prettyPrint(self) -> str:
        return self._name


def _requested(var_bind: Any) -> str:
    # ObjectType(ObjectIdentity("1.3...")) as net_guard builds it, unresolved
    # (lookupMib=False): read the numeric OID back out.
    identity = var_bind._ObjectType__args[0]
    return str(identity._ObjectIdentity__args[0]).lstrip(".")


@dataclass
class FakeSnmp:
    agents: dict[str, FakeAgent] = field(default_factory=dict)
    calls: list[SnmpCall] = field(default_factory=list)
    rows_sent: int = 0  # walk rows handed to the client
    set_attempts: list[str] = field(default_factory=list)

    def agent(self, address: str, mib: Mapping[str, Asn1Item], **creds: Any) -> FakeAgent:
        agent = FakeAgent(mib=dict(mib), **creds)
        self.agents[address] = agent
        return agent

    # --- what an agent answers -----------------------------------------------------

    def _record(
        self,
        op: Literal["get", "walk"],
        oids: list[str],
        auth: Any,
        target: Any,
        max_rows: int | None = None,
    ) -> None:
        address, port = target.transport_address[:2]
        self.calls.append(
            SnmpCall(
                op=op,
                oids=tuple(oids),
                address=address,
                port=port,
                timeout=target.timeout,
                retries=target.retries,
                mp_model=auth.message_processing_model,
                max_rows=max_rows,
            )
        )

    def _check(self, auth: Any, target: Any) -> tuple[FakeAgent | None, Any]:
        """``(agent, error_indication)``: no agent means silence."""
        address, port = target.transport_address[:2]
        agent = self.agents.get(address)
        if agent is None or agent.port != port:
            return None, errind.RequestTimedOut()
        if agent.answer_limit is not None and agent.answered >= agent.answer_limit:
            return None, errind.RequestTimedOut()
        if auth.message_processing_model in (0, 1):
            if agent.community is None or auth.communityName != agent.community:
                return None, errind.RequestTimedOut()  # agents ignore a wrong community
        else:
            if agent.v3_user is None or auth.userName != agent.v3_user:
                return None, errind.UnknownUserName()
            if auth.authentication_key != agent.v3_auth_key:
                return None, errind.WrongDigest()
            if agent.v3_priv_key is not None and auth.privacy_key != agent.v3_priv_key:
                return None, errind.DecryptionError()
        agent.answered += 1
        return agent, None

    @staticmethod
    def _visible(value: Asn1Item, v1: bool) -> bool:
        return not (v1 and isinstance(value, Counter64))

    async def get_cmd(
        self, engine: Any, auth: Any, target: Any, context: Any, *var_binds: Any, **kw: Any
    ) -> Any:
        oids = [_requested(vb) for vb in var_binds]
        self._record("get", oids, auth, target)
        agent, error = self._check(auth, target)
        if agent is None:
            return error, 0, 0, []
        await asyncio.sleep(agent.delay)
        v1 = auth.message_processing_model == 0
        result = []
        for oid in oids:
            value = agent.mib.get(oid)
            if value is None or not self._visible(value, v1):
                if v1:
                    return None, _ErrorStatus("noSuchName"), 1, []
                value = NoSuchObject()
            result.append((ObjectName(oid), value))
        return None, 0, 0, result

    async def walk_cmd(
        self, engine: Any, auth: Any, target: Any, context: Any, var_bind: Any, **kw: Any
    ) -> AsyncIterator[Any]:
        start = _requested(var_bind)
        max_rows = kw.get("maxRows", 0)
        self._record("walk", [start], auth, target, max_rows=max_rows)
        assert kw.get("lexicographicMode") is False  # the client never walks past the subtree
        v1 = auth.message_processing_model == 0
        prefix = _key(start)
        agent = self.agents.get(target.transport_address[0])
        mib = agent.mib if agent is not None else {}
        oids = [
            oid
            for oid in sorted(mib, key=_key)
            if _key(oid)[: len(prefix)] == prefix and self._visible(mib[oid], v1)
        ]
        if agent is not None and agent.broken_walk is not None and oids:
            # A buggy agent: maxRows doesn't stop it, only the client can.
            order = oids[::-1] if agent.broken_walk == "backwards" else oids[:1]
            i = 0
            while True:
                oid = order[i % len(order)] if agent.broken_walk == "repeat" else order[i]
                self.rows_sent += 1
                yield None, 0, 0, ((ObjectName(oid), mib[oid]),)
                await asyncio.sleep(0)
                i = i + 1 if agent.broken_walk == "repeat" else min(i + 1, len(order) - 1)
        # One GETNEXT per row, plus the one that runs off the subtree's end; the agent
        # may stop answering at any of them.
        for i in range(len(oids) + 1):
            answering, error = self._check(auth, target)
            if answering is None:
                yield error, 0, 0, ()
                return
            await asyncio.sleep(answering.delay)
            if i == len(oids):
                return
            self.rows_sent += 1
            yield None, 0, 0, ((ObjectName(oids[i]), answering.mib[oids[i]]),)
            if max_rows and i + 1 >= max_rows:
                return

    # --- views on the record ---------------------------------------------------------

    def walked(self) -> list[str]:
        return [c.oids[0] for c in self.calls if c.op == "walk"]

    def requested_oids(self) -> Iterator[str]:
        for call in self.calls:
            yield from call.oids


def trap_snmp_set(monkeypatch: pytest.MonkeyPatch, attempts: list[str]) -> None:
    """Make every SET path in pysnmp fail loudly and record the attempt."""

    def trapped(where: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            attempts.append(where)
            raise AssertionError(f"SNMP SET reached ({where}): Hard rule 2")

        return fail

    monkeypatch.setattr(hlapi_asyncio, "set_cmd", trapped("hlapi set_cmd"))
    monkeypatch.setattr(hlapi_cmdgen, "set_cmd", trapped("hlapi cmdgen.set_cmd"))
    monkeypatch.setattr(
        rfc3413_cmdgen.SetCommandGenerator, "send_varbinds", trapped("SetCommandGenerator")
    )


@pytest.fixture
def fake_snmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSnmp]:
    fake = FakeSnmp()
    monkeypatch.setattr(net_guard, "get_cmd", fake.get_cmd)
    monkeypatch.setattr(net_guard, "walk_cmd", fake.walk_cmd)
    trap_snmp_set(monkeypatch, fake.set_attempts)
    yield fake
    assert fake.set_attempts == [], "an SNMP SET was attempted"

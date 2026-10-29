"""Reading MIB-II system info and IF-MIB interface tables over SNMP, read-only.

Shared by the SNMP integrations: ``snmp_device`` (M4d) uses it as is, and a device
profile (MikroTik SwOS, M4e) can reuse it with its own metric prefix, extra
ifIndex-indexed columns (:attr:`IfMibOptions.extra_columns`) and its own port naming
(:attr:`IfMibOptions.port_name`).

Everything here is ``get`` and ``walk`` on a :class:`GuardedSnmpClient`, which exposes
nothing else: no SET, ever (Hard rule 2). Every OID read is listed in
:data:`SYSTEM_OIDS`, :data:`IF_TABLE` and :data:`IF_X_TABLE`, and in
``docs/integrations/snmp_device.md``.

Uptime: sysUpTime is the SNMP agent's uptime, not the device's (VyOS, for one, restarts
snmpd on every config commit). hrSystemUptime (HOST-RESOURCES-MIB) is the host's, so it
is read in the same GET (a second GET on v1) and preferred where the agent has it;
switches often don't.

How a poll reads the interfaces, and why:

1. Walk ``ifDescr`` (every ``ifTable`` row has one) to learn the interfaces, at most
   :data:`MAX_WALK_ROWS` rows (one more is read to tell whether there are more:
   :attr:`InterfaceTable.truncated`). Then ``ifName`` and ``ifAlias`` from ``ifXTable``; if
   ``ifName`` comes back empty the agent has no ``ifXTable`` (old or v1-only agents), and
   no other ``ifXTable`` column is walked.
2. Apply the include/exclude filter and the interface cap, so a large switch cannot
   produce unbounded series. The walks themselves still cover every row (up to the
   walk cap): a walk cannot skip rows.
3. Walk one column per value. 64-bit ``ifHCInOctets``/``ifHCOutOctets`` are used where
   the agent has them; the 32-bit ``ifInOctets``/``ifOutOctets`` are walked only when a
   selected interface lacks one (and always on SNMPv1, which has no 64-bit counters).
   Counter wrap and agent restarts are Prometheus' business (``rate()`` handles resets).

Values and text come from the device: numbers are type-checked, text is cleaned and
length-capped (:func:`clean_text`) before it is used as a label.
"""

import fnmatch
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from app.core.net_guard import GuardedSnmpClient, SnmpError, SnmpTimeout, SnmpVarBind
from app.integrations.base import Sample

# --- OIDs (numeric; the guarded client does no MIB lookups) ----------------------------

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"  # TimeTicks, hundredths of a second
SYS_NAME = "1.3.6.1.2.1.1.5.0"
MIB2_SYSTEM_OIDS = (SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME, SYS_NAME)
# HOST-RESOURCES-MIB hrSystemUptime.0: TimeTicks since the host booted.
HR_SYSTEM_UPTIME = "1.3.6.1.2.1.25.1.1.0"
# Everything the system GET asks for.
SYSTEM_OIDS = (*MIB2_SYSTEM_OIDS, HR_SYSTEM_UPTIME)

# IF-MIB ifTable (1.3.6.1.2.1.2.2.1.<column>.<ifIndex>): the columns read.
IF_TABLE = {
    "descr": "1.3.6.1.2.1.2.2.1.2",
    "speed": "1.3.6.1.2.1.2.2.1.5",  # bit/s, saturates at 2^32-1
    "admin_status": "1.3.6.1.2.1.2.2.1.7",
    "oper_status": "1.3.6.1.2.1.2.2.1.8",
    "in_octets": "1.3.6.1.2.1.2.2.1.10",
    "in_discards": "1.3.6.1.2.1.2.2.1.13",
    "in_errors": "1.3.6.1.2.1.2.2.1.14",
    "out_octets": "1.3.6.1.2.1.2.2.1.16",
    "out_discards": "1.3.6.1.2.1.2.2.1.19",
    "out_errors": "1.3.6.1.2.1.2.2.1.20",
}
# IF-MIB ifXTable (1.3.6.1.2.1.31.1.1.1.<column>.<ifIndex>).
IF_X_TABLE = {
    "name": "1.3.6.1.2.1.31.1.1.1.1",
    "hc_in_octets": "1.3.6.1.2.1.31.1.1.1.6",
    "hc_out_octets": "1.3.6.1.2.1.31.1.1.1.10",
    "high_speed": "1.3.6.1.2.1.31.1.1.1.15",  # Mbit/s
    "alias": "1.3.6.1.2.1.31.1.1.1.18",
}

# Columns read as numbers, apart from the octet counters (which have the HC fallback).
_NUMERIC_IF_TABLE = (
    "admin_status",
    "oper_status",
    "in_discards",
    "in_errors",
    "out_discards",
    "out_errors",
    "speed",
)

# Hard caps, whatever the config says.
MAX_WALK_ROWS = 1024  # rows read per column walk
MAX_INTERFACES = 512  # interfaces one poll may return
MAX_LABEL_TEXT = 128  # characters of device text in an interface label
MAX_SYS_DESCR = 256  # characters of sysDescr in the info label


def clean_text(value: object, limit: int = MAX_LABEL_TEXT) -> str:
    """Device-supplied text made safe for a label or a message.

    Octet strings are decoded as UTF-8 (invalid bytes replaced); NULs (padding some
    agents add) are dropped; anything non-printable (newlines, tabs, control and bidi
    characters) becomes a space; runs of spaces collapse; the result is cut to ``limit``.
    Anything that is not text (a number, ``None``) becomes ``""``.
    """
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        return ""
    text = "".join(c if c.isprintable() else " " for c in text.replace("\x00", ""))
    return " ".join(text.split())[:limit]


def _number(value: object) -> int | None:
    # bool is an int subclass; nothing SNMP returns is a bool, but be strict anyway.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


SnmpValue = int | str | bytes | None


def _column(rows: Iterable[SnmpVarBind], column: str) -> dict[int, SnmpValue]:
    """``{ifIndex: value}`` from a column walk. Rows whose OID is not exactly
    ``<column>.<integer>`` are ignored (a walk stays in the subtree, but be strict)."""
    prefix = column + "."
    out: dict[int, SnmpValue] = {}
    for row in rows:
        oid = row.oid.lstrip(".")
        if not oid.startswith(prefix):
            continue
        suffix = oid[len(prefix) :]
        if suffix.isdigit() and suffix.isascii():
            out[int(suffix)] = row.value
    return out


# --- system group ------------------------------------------------------------------


@dataclass(frozen=True)
class SystemInfo:
    descr: str = ""
    name: str = ""
    object_id: str = ""
    # sysUpTime: how long the SNMP agent has been running.
    agent_uptime_seconds: float | None = None
    # hrSystemUptime: how long the host has been up, where the agent has it.
    host_uptime_seconds: float | None = None

    @property
    def uptime_seconds(self) -> float | None:
        """The best available device uptime: hrSystemUptime, else sysUpTime."""
        if self.host_uptime_seconds is not None:
            return self.host_uptime_seconds
        return self.agent_uptime_seconds

    @property
    def uptime_source(self) -> Literal["hrSystemUptime", "sysUpTime"] | None:
        """The object :attr:`uptime_seconds` came from."""
        if self.host_uptime_seconds is not None:
            return "hrSystemUptime"
        return "sysUpTime" if self.agent_uptime_seconds is not None else None

    @property
    def readable(self) -> bool:
        """Whether the agent let us read sysDescr or sysName (a restricted view may not)."""
        return bool(self.descr or self.name)


def parse_system(rows: Iterable[SnmpVarBind]) -> SystemInfo:
    values = {row.oid.lstrip("."): row.value for row in rows}
    ticks = _number(values.get(SYS_UPTIME))
    # Absent (noSuchObject/noSuchInstance come back as None), not a number, or 0 (an
    # agent that doesn't really track it): fall back to sysUpTime.
    host_ticks = _number(values.get(HR_SYSTEM_UPTIME)) or None
    object_id = values.get(SYS_OBJECT_ID)
    return SystemInfo(
        descr=clean_text(values.get(SYS_DESCR), MAX_SYS_DESCR),
        name=clean_text(values.get(SYS_NAME)),
        # An OID comes back as a dotted string; anything else is ignored.
        object_id=object_id
        if isinstance(object_id, str) and object_id.replace(".", "").isdigit()
        else "",
        agent_uptime_seconds=ticks / 100 if ticks is not None else None,
        host_uptime_seconds=host_ticks / 100 if host_ticks is not None else None,
    )


async def read_system(client: GuardedSnmpClient, *, v1: bool = False) -> SystemInfo:
    """One GET of sysDescr, sysObjectID, sysUpTime, sysName and hrSystemUptime.

    On SNMPv1 one missing object fails the whole GET (``noSuchName``), and many agents
    have no HOST-RESOURCES-MIB. So with ``v1`` hrSystemUptime is read in a second GET of
    its own, and an error answer to that one only means it's absent. A timeout raises.
    """
    if not v1:
        return parse_system(await client.get(list(SYSTEM_OIDS)))
    rows = await client.get(list(MIB2_SYSTEM_OIDS))
    try:
        rows += await client.get([HR_SYSTEM_UPTIME])
    except SnmpTimeout:
        raise
    except SnmpError:
        # The first GET already proved the agent answers with these credentials, so an
        # error answer here is the v1 noSuchName for hrSystemUptime: treat it as absent.
        pass
    return parse_system(rows)


# --- interfaces --------------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceFilter:
    """Shell-style patterns (``fnmatch``: ``*``, ``?``, ``[...]``), case-insensitive,
    matched against an interface's name and its description (``ifName``, ``ifDescr``).

    An interface is kept when no include pattern is given or one matches, and no
    exclude pattern matches (exclude wins).
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def keeps(self, *names: str) -> bool:
        candidates = [n.casefold() for n in names if n]

        def hit(patterns: tuple[str, ...]) -> bool:
            return any(
                fnmatch.fnmatchcase(name, pattern.casefold())
                for pattern in patterns
                for name in candidates
            )

        if self.include and not hit(self.include):
            return False
        return not hit(self.exclude)


@dataclass
class Interface:
    index: int
    descr: str = ""
    name: str = ""  # ifName as the agent reports it ("" without ifXTable)
    alias: str = ""
    port_name: str = ""  # the label used for this port; see IfMibOptions.port_name
    in_octets: int | None = None
    out_octets: int | None = None
    counters_64bit: bool = False  # both octet counters came from ifHC*Octets
    values: dict[str, int | None] = field(default_factory=dict)  # see _NUMERIC_IF_TABLE
    speed_bps: int | None = None
    extra: dict[str, SnmpValue] = field(default_factory=dict)


def default_port_name(interface: Interface) -> str:
    """ifName (cleaned and capped), else ``if<ifIndex>``.

    Never ifDescr: the port name is a label on every value series, and free-form device
    text may only be a label on ``*_info`` series (maintainer decision, 2026-10-28).
    ifDescr is on ``if_info`` as ``if_descr``.
    """
    return interface.name or f"if{interface.index}"


@dataclass(frozen=True)
class IfMibOptions:
    max_interfaces: int = 128
    filter: InterfaceFilter = InterfaceFilter()
    # False for SNMPv1, which cannot carry Counter64: the ifHC* columns are not walked.
    hc_counters: bool = True
    # Extra columns of ifIndex-indexed tables a profile wants, as {key: column OID}.
    # Walked like the others and stored raw in Interface.extra; turning them into
    # samples is the profile's job.
    extra_columns: Mapping[str, str] = field(default_factory=dict)
    # The label value for a port; a profile can name ports its own way.
    port_name: Callable[[Interface], str] = default_port_name

    def __post_init__(self) -> None:
        if not 1 <= self.max_interfaces <= MAX_INTERFACES:
            raise ValueError(f"max_interfaces must be between 1 and {MAX_INTERFACES}")


@dataclass(frozen=True)
class InterfaceTable:
    seen: int  # ifTable rows read (at most MAX_WALK_ROWS)
    matched: int  # of those, how many the filter kept (before the cap)
    interfaces: list[Interface]  # what the poll reports: filtered, capped, by ifIndex
    # The agent has more than MAX_WALK_ROWS ifTable rows; only the first were read.
    truncated: bool = False


async def _walk(client: GuardedSnmpClient, column: str, rows: int) -> dict[int, SnmpValue]:
    return _column(await client.walk(column, max_rows=rows), column)


async def read_interfaces(client: GuardedSnmpClient, options: IfMibOptions) -> InterfaceTable:
    """Walk the IF-MIB columns and return the filtered, capped interfaces."""
    # One row past the cap tells "exactly at the cap" from "more than the cap".
    descrs = await _walk(client, IF_TABLE["descr"], MAX_WALK_ROWS + 1)
    truncated = len(descrs) > MAX_WALK_ROWS
    if truncated:
        descrs = dict(sorted(descrs.items())[:MAX_WALK_ROWS])
    if not descrs:  # no ifTable visible (or a view hides it): nothing more to walk
        return InterfaceTable(seen=0, matched=0, interfaces=[])
    # Every later walk covers at most as many rows as ifTable has: the columns are in
    # the same ifIndex order, so this bounds each walk without losing selected rows.
    rows = len(descrs)
    names = await _walk(client, IF_X_TABLE["name"], rows)
    has_x_table = bool(names)
    aliases = await _walk(client, IF_X_TABLE["alias"], rows) if has_x_table else {}

    candidates: list[Interface] = []
    for index in sorted(descrs):
        interface = Interface(
            index=index,
            descr=clean_text(descrs[index]),
            name=clean_text(names.get(index)),
            alias=clean_text(aliases.get(index)),
        )
        interface.port_name = clean_text(options.port_name(interface)) or f"if{index}"
        if options.filter.keeps(interface.name, interface.descr):
            candidates.append(interface)
    selected = candidates[: options.max_interfaces]
    table = InterfaceTable(
        seen=len(descrs), matched=len(candidates), interfaces=selected, truncated=truncated
    )
    if not selected:
        return table

    for key in _NUMERIC_IF_TABLE:
        column = await _walk(client, IF_TABLE[key], rows)
        for interface in selected:
            interface.values[key] = _number(column.get(interface.index))

    hc_in: dict[int, SnmpValue] = {}
    hc_out: dict[int, SnmpValue] = {}
    high_speed: dict[int, SnmpValue] = {}
    if has_x_table:
        high_speed = await _walk(client, IF_X_TABLE["high_speed"], rows)
        if options.hc_counters:
            hc_in = await _walk(client, IF_X_TABLE["hc_in_octets"], rows)
            hc_out = await _walk(client, IF_X_TABLE["hc_out_octets"], rows)
    for interface in selected:
        interface.in_octets = _number(hc_in.get(interface.index))
        interface.out_octets = _number(hc_out.get(interface.index))
        interface.counters_64bit = (
            interface.in_octets is not None and interface.out_octets is not None
        )

    # The 32-bit counters, only for what the 64-bit ones didn't cover.
    if any(i.in_octets is None for i in selected):
        column = await _walk(client, IF_TABLE["in_octets"], rows)
        for interface in selected:
            if interface.in_octets is None:
                interface.in_octets = _number(column.get(interface.index))
    if any(i.out_octets is None for i in selected):
        column = await _walk(client, IF_TABLE["out_octets"], rows)
        for interface in selected:
            if interface.out_octets is None:
                interface.out_octets = _number(column.get(interface.index))

    for interface in selected:
        mbps = _number(high_speed.get(interface.index))
        # ifHighSpeed (Mbit/s) when the agent reports one; else ifSpeed (bit/s), which
        # also covers links under 1 Mbit/s (ifHighSpeed 0). ifSpeed saturates at 2^32-1,
        # so a faster link on an agent without ifHighSpeed reads as ~4.29 Gbit/s.
        if mbps:
            interface.speed_bps = mbps * 1_000_000
        else:
            interface.speed_bps = interface.values.get("speed")

    for key, oid in options.extra_columns.items():
        column = await _walk(client, oid, rows)
        for interface in selected:
            interface.extra[key] = column.get(interface.index)
    return table


# --- samples -----------------------------------------------------------------------


def system_samples(info: SystemInfo, prefix: str) -> list[Sample]:
    """``<prefix>_info`` (1, with sys_* labels), ``<prefix>_uptime_seconds`` (the best
    available device uptime, labelled ``source`` with the object it came from) and
    ``<prefix>_agent_uptime_seconds`` (sysUpTime, whenever the agent reports it)."""
    samples = [
        Sample(
            name=f"{prefix}_info",
            labels={
                "sys_name": info.name,
                "sys_descr": info.descr,
                "sys_object_id": info.object_id,
            },
            value=1.0,
            type="gauge",
        )
    ]
    if info.uptime_seconds is not None and info.uptime_source is not None:
        samples.append(
            Sample(
                name=f"{prefix}_uptime_seconds",
                labels={"source": info.uptime_source},
                value=info.uptime_seconds,
                type="gauge",
            )
        )
    if info.agent_uptime_seconds is not None:
        samples.append(
            Sample(
                name=f"{prefix}_agent_uptime_seconds",
                value=info.agent_uptime_seconds,
                type="gauge",
            )
        )
    return samples


# (sample suffix, Interface.values key, type) for the plain numeric columns.
_VALUE_SAMPLES: tuple[tuple[str, str, Literal["gauge", "counter"]], ...] = (
    ("if_oper_status", "oper_status", "gauge"),
    ("if_admin_status", "admin_status", "gauge"),
    ("if_in_errors", "in_errors", "counter"),
    ("if_out_errors", "out_errors", "counter"),
    ("if_in_discards", "in_discards", "counter"),
    ("if_out_discards", "out_discards", "counter"),
)


def interface_samples(table: InterfaceTable, prefix: str) -> list[Sample]:
    """Per-interface samples, labelled ``if_index`` and ``if_name`` (the port name), plus
    ``<prefix>_if_info`` carrying ``if_descr`` and ``if_alias``, and the interface counts.

    The free-text ``ifDescr``/``ifAlias`` sit only on the info series, so renaming a port
    on the device doesn't split its counter series.
    """
    samples = [
        Sample(name=f"{prefix}_interfaces", value=float(table.seen), type="gauge"),
        Sample(
            name=f"{prefix}_interfaces_polled", value=float(len(table.interfaces)), type="gauge"
        ),
        Sample(
            name=f"{prefix}_interfaces_truncated",
            value=1.0 if table.truncated else 0.0,
            type="gauge",
        ),
    ]
    for interface in table.interfaces:
        labels = {"if_index": str(interface.index), "if_name": interface.port_name}
        samples.append(
            Sample(
                name=f"{prefix}_if_info",
                labels={**labels, "if_descr": interface.descr, "if_alias": interface.alias},
                value=1.0,
                type="gauge",
            )
        )
        if interface.in_octets is not None:
            samples.append(
                Sample(
                    name=f"{prefix}_if_in_bytes",
                    labels=labels,
                    value=float(interface.in_octets),
                    type="counter",
                )
            )
        if interface.out_octets is not None:
            samples.append(
                Sample(
                    name=f"{prefix}_if_out_bytes",
                    labels=labels,
                    value=float(interface.out_octets),
                    type="counter",
                )
            )
        samples.append(
            Sample(
                name=f"{prefix}_if_counters_64bit",
                labels=labels,
                value=1.0 if interface.counters_64bit else 0.0,
                type="gauge",
            )
        )
        for suffix, key, kind in _VALUE_SAMPLES:
            value = interface.values.get(key)
            if value is not None:
                samples.append(
                    Sample(
                        name=f"{prefix}_{suffix}",
                        labels=labels,
                        value=float(value),
                        type=kind,
                    )
                )
        if interface.speed_bps is not None:
            samples.append(
                Sample(
                    name=f"{prefix}_if_speed_bits_per_second",
                    labels=labels,
                    value=float(interface.speed_bps),
                    type="gauge",
                )
            )
    return samples

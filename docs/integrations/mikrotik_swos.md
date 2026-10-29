# MikroTik SwOS switch (`mikrotik_swos`)

**Style:** polled (SNMP), a profile on the generic [`snmp_device`](snmp_device.md). **Category:** `switch`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/mikrotik_swos/integration.py`, reusing `backend/app/integrations/builtin/ifmib.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**Not verified against a device.** SwOS can't be emulated in our lab. The fixtures (`snmp_swos_full.snmpwalk`, `snmp_swos_iftable_only.snmpwalk`) are hand-written from MikroTik's public MIB and documentation, and the profile is built to degrade: whatever the switch doesn't answer is left out, and the poll only fails when the system group or the interface table can't be read. The open questions are listed at the end as `TODO(maintainer)` for a lab checkpoint.

**When to use it:** for a CSS or CRS switch running SwOS (not RouterOS, which has a full SNMP agent: use `snmp_device` for that). It differs from `snmp_device` in four ways: SwOS-aware port names, the two health gauges below, SNMP v1/v2c only (SwOS has no v3, so the form has no v3 fields), and a smaller default port cap (64).

**Setup on the switch:** in SwOS, open the SNMP tab, enable SNMP, set a read-only community and, under allowed addresses, add the address of the Vantage backend. SwOS's agent is read-only.

**What it reads:** SNMP GET and GETNEXT (walk) only, through `ctx.snmp_client()`; no SET exists on the guarded client (Hard rule 2). Sent only to the address the SSRF guard checked. The IF-MIB and system-group OIDs are exactly those of [`snmp_device`](snmp_device.md); plus:

| What | OID | How | Notes |
|---|---|---|---|
| mtxrHlVoltage.0 (MIKROTIK-MIB) | `1.3.6.1.4.1.14988.1.1.3.8.0` | GET, alone | after the interface walks; best effort |
| mtxrHlTemperature.0 (MIKROTIK-MIB) | `1.3.6.1.4.1.14988.1.1.3.10.0` | GET, alone | likewise |

Each health object is a GET of its own, because on v1 one missing object fails a whole GET. An object that is missing or answered with an error is left out; silence stops the health reads but doesn't fail the poll. A poll is therefore one GET (two on v1), up to 15 walks and two GETs, inside the same 50 s budget as `snmp_device`.

**Config:** see `MikroTikSwOSConfig`. `host` (private address only), `port` (161), `version` (`1` or `2c`, default `2c`), `community` (secret), `timeout_seconds`, `retries`, `include_interfaces` / `exclude_interfaces` (shell-style patterns against ifName and ifDescr) and `max_interfaces` (64).

**Secrets:** `community` only, a write-only `SecretStr`. It goes into the SNMP request and nowhere else; messages, statuses and metrics name only the host and port. v1 and v2c send it unencrypted on the wire: keep the switch's management network private and the community read-only.

**Port names.** `if_name` is a label on every value series, so it must be a stable identifier, not free text. SwOS lets the admin rename each port, and (as far as the public documentation says) the name is what the switch reports in `ifDescr`. So the profile takes `ifName`, else `ifDescr`, and uses it only when it is a default-style name: `Port1`, `ether3`, `SFP2`, `SFP+9`, `sfp-sfpplus1`, `QSFP+1`, normalised to lowercase without spaces (`port1`, `ether3`, `sfp2`, `sfpplus9`, `sfpplus1`, `qsfpplus1`). Any other name (a renamed port) becomes `port<ifIndex>`. The admin's own text is still available on `if_info` as `if_descr` and `if_alias`; join on `if_index` to show it. Renaming a port therefore never splits its counter series.

**Metrics:** prefix `vantage_mikrotik_swos_`. The same `info`, `uptime_seconds`, `agent_uptime_seconds`, `interfaces*` and `if_*` series as [`snmp_device`](snmp_device.md), with the port naming above, plus:

| Metric | Type | Meaning |
|---|---|---|
| `voltage_volts` | gauge | mtxrHlVoltage x 0.1, when the switch answers it |
| `temperature_celsius` | gauge | mtxrHlTemperature x 0.1, when the switch answers it |

Free-form text (`sys_name`, `sys_descr`, `if_descr`, `if_alias`) is on the `*_info` series only, as for `snmp_device`.

**Status and failures:** the same as `snmp_device` (`no SNMP response`, answered with an error, walk stopped partway, guard refusal, non-increasing walk, poll too slow). Silence is also what a wrong community, SNMP disabled in SwOS or a missing allowed address look like.

**Firewall:** `udp` to the configured port (161), from the Vantage backend, plus the replies back; and the backend's address in SwOS's SNMP allowed addresses.

**Cards:** `snmp-overview` and `snmp-ports`, shared with `snmp_device` (frontend work in M5.5, with the switch faceplate).

**Not read (on purpose):** PoE (`mtxrPOE`), SFP/DDM optics and per-port VLAN or bridge tables. Whether SwOS answers them is unknown, and none is needed for status and traffic.

**TODO(maintainer): lab checkpoint** (everything below is a guess until a real SwOS switch is walked):

1. Does SwOS answer `ifXTable` (ifName, ifAlias, ifHighSpeed, 64-bit counters)? Both shapes are handled and tested.
2. What does it return as `ifDescr`/`ifName` for default and renamed ports, and are there non-port interfaces (CPU, bridge)? The naming rule above assumes default-style names.
3. Does it answer `mtxrHlVoltage.0` / `mtxrHlTemperature.0`, and in which units (RouterOS uses tenths)? Model-dependent, if at all.
4. `sysDescr` and `sysObjectID` (the fixtures assume the model name and `1.3.6.1.4.1.14988.2`).
5. Is PoE (`mtxrPOE`) exposed on PoE models? If so, a follow-up could add it.
6. Replace the hand-written fixtures with a scrubbed walk.

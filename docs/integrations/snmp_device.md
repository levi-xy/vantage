# SNMP device (`snmp_device`)

**Style:** polled (SNMP). **Category:** `switch`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/snmp_device/integration.py`, with the IF-MIB reading in `backend/app/integrations/builtin/ifmib.py` (shared with device profiles such as MikroTik SwOS). Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** SNMP GET and GETNEXT (walk) only, through `ctx.snmp_client()`. The guarded client has no SET, so nothing on the device can change (Hard rule 2). It resolves the host once, checks the address with the SSRF guard, and sends only to that checked address. All OIDs are numeric. No MIB files are loaded.

| What | OID | How | Notes |
|---|---|---|---|
| sysDescr.0 | `1.3.6.1.2.1.1.1.0` | GET | connection test and poll |
| sysObjectID.0 | `1.3.6.1.2.1.1.2.0` | GET | poll |
| sysUpTime.0 | `1.3.6.1.2.1.1.3.0` | GET | poll |
| sysName.0 | `1.3.6.1.2.1.1.5.0` | GET | connection test and poll |
| hrSystemUptime.0 (HOST-RESOURCES-MIB) | `1.3.6.1.2.1.25.1.1.0` | GET | poll, in the same GET; on v1 in a second GET of its own, because one missing object fails a whole v1 GET (`noSuchName`) |
| ifDescr | `1.3.6.1.2.1.2.2.1.2` | walk | always: it lists the interfaces |
| ifName | `1.3.6.1.2.1.31.1.1.1.1` | walk | always; empty means the agent has no ifXTable |
| ifAlias | `1.3.6.1.2.1.31.1.1.1.18` | walk | if ifXTable exists |
| ifAdminStatus, ifOperStatus | `1.3.6.1.2.1.2.2.1.7`, `.8` | walk | if any interface is selected |
| ifInDiscards, ifInErrors | `1.3.6.1.2.1.2.2.1.13`, `.14` | walk | likewise |
| ifOutDiscards, ifOutErrors | `1.3.6.1.2.1.2.2.1.19`, `.20` | walk | likewise |
| ifSpeed | `1.3.6.1.2.1.2.2.1.5` | walk | likewise |
| ifHighSpeed | `1.3.6.1.2.1.31.1.1.1.15` | walk | if ifXTable exists |
| ifHCInOctets, ifHCOutOctets | `1.3.6.1.2.1.31.1.1.1.6`, `.10` | walk | if ifXTable exists; never on v1 |
| ifInOctets, ifOutOctets | `1.3.6.1.2.1.2.2.1.10`, `.16` | walk | only if a selected interface has no 64-bit counter |

A poll is one GET (two on v1) and up to 15 walks, one per column. `ifDescr` reads at most `MAX_WALK_ROWS` (1024) rows, plus one to tell whether there are more; every other walk reads at most as many rows as `ifDescr` kept. The guarded client stops a walk at the first OID that doesn't increase. The connection test is a single GET of sysDescr and sysName.

**Config:** see `SnmpDeviceConfig`.

- `host` is found by name by the save-time check, resolved with `port`, and must be on a private network. It is stored in plaintext.
- `version` is `1`, `2c` or `3`. v3 supports `authNoPriv` and `authPriv` (`security_level`), with the `auth_protocol` and `priv_protocol` choices the guarded client offers: MD5, SHA-1 and SHA-2, and DES or AES. `noAuthNoPriv` is not offered.
- `username` (v3) is plaintext config.
- `timeout_seconds` and `retries` apply per request and are capped by the guarded client (5 s, 2 retries). Every GET or walk also stops after 15 s in total.
- `include_interfaces` and `exclude_interfaces` are shell-style patterns (`*`, `?`, `[...]`), matched without case against `ifName` and `ifDescr`. Exclude wins.
- `max_interfaces` caps how many interfaces one poll reports: the lowest `ifIndex` values that pass the filter. The hard limit is `ifmib.MAX_INTERFACES`.
- A secret that the chosen version doesn't use is ignored, not refused, so you can switch version on an edit without clearing the old secrets first.

**Secrets:** `community` (v1/v2c), `auth_key` and `priv_key` (v3 passphrases, 8 to 256 characters). All three are write-only `SecretStr` fields. They go only into the SNMP request: the community string, or the USM keys, which pysnmp uses to authenticate and encrypt and never sends in clear. Messages, statuses and metrics name only the host and port. v1 and v2c send the community unencrypted on the wire, so prefer v3 where the device supports it.

**Metrics:** prefix `vantage_snmp_device_` (`METRIC_PREFIX`; names are built in `ifmib.system_samples` and `ifmib.interface_samples`). Counters are exposed with `_total`.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `info` | gauge, 1 | `sys_name`, `sys_descr`, `sys_object_id` | system identity; `sys_descr` is one line, at most 256 characters |
| `uptime_seconds` | gauge | `source` | the best available device uptime: hrSystemUptime (`source="hrSystemUptime"`) when the agent has it and it isn't 0, otherwise sysUpTime (`source="sysUpTime"`), for example on switches without HOST-RESOURCES-MIB |
| `agent_uptime_seconds` | gauge | none | sysUpTime: the SNMP agent's uptime. Not the device's: VyOS restarts snmpd on every config commit, so this can be seconds while the host has been up for hours |
| `interfaces` | gauge | none | ifTable rows seen (at most the walk cap) |
| `interfaces_polled` | gauge | none | rows reported after the filter and the cap |
| `interfaces_truncated` | gauge | none | 1 if the agent has more than `MAX_WALK_ROWS` ifTable rows and only the first were read (also logged as a warning with the instance id) |
| `if_info` | gauge, 1 | `if_index`, `if_name`, `if_descr`, `if_alias` | one per polled interface |
| `if_in_bytes`, `if_out_bytes` | counter | `if_index`, `if_name` | ifHC*Octets, or ifIn/OutOctets where the agent has no 64-bit counter |
| `if_counters_64bit` | gauge | `if_index`, `if_name` | 1 if both octet counters are 64-bit |
| `if_in_errors`, `if_out_errors`, `if_in_discards`, `if_out_discards` | counter | `if_index`, `if_name` | packets |
| `if_oper_status`, `if_admin_status` | gauge | `if_index`, `if_name` | the raw IF-MIB value: 1 up, 2 down, 3 testing, 4 unknown, 5 dormant, 6 notPresent, 7 lowerLayerDown |
| `if_speed_bits_per_second` | gauge | `if_index`, `if_name` | ifHighSpeed × 10⁶ when it isn't 0; otherwise ifSpeed |

Because of its `source` label, `uptime_seconds` starts a new series if an agent's hrSystemUptime comes and goes. A reboot alert should therefore use `agent_uptime_seconds` or `sum without(source) (vantage_snmp_device_uptime_seconds)`, so a flapping hrSystemUptime doesn't split the series and fire a false alert.

`if_name` is the device's `ifName`, cleaned and capped; without one (no ifXTable, or an empty ifName) it is `if<ifIndex>`, never ifDescr. Free-form device text (`if_descr`, `if_alias`, `sys_name`, `sys_descr`) appears only on the `info` and `if_info` series (value 1), under the exception the maintainer approved on 2026-10-28 (INTEGRATIONS.md section 9). It never appears on a value series, so renaming a port on the device doesn't split its counter series. Join on `if_index` to show them. All device text is cleaned before it becomes a label: decoded as UTF-8, NULs dropped, control and other non-printable characters replaced, whitespace collapsed, at most 128 characters. Counter wrap and agent restarts are left to Prometheus (`rate()` handles resets). An interface switching between 32-bit and 64-bit counters looks like a reset.

**Status and failures:** the instance goes `down` with:

- `no SNMP response from <host>:<port> (...)`: no answer to the first request. A firewall, a wrong port, SNMP disabled, a wrong community (agents answer that with silence), or an access list without the backend's address.
- `SNMP request to <host>:<port> was answered with an error (...)`: for example an unknown v3 user, a wrong passphrase or protocol, or a v1 `noSuchName` on the system GET. pysnmp's own text is never shown.
- `SNMP walk of <host>:<port> stopped answering partway (...)`: the agent answered, then went silent, or a walk ran past the 15 s limit. Usually a slow agent or a very large interface table.
- `refused by the network guard: ...`: the host resolves to an address the guard denies.
- `SNMP agent at <host>:<port> sent walk answers whose OIDs do not increase (...)`: a buggy agent that repeats or goes back; the walk is stopped at that row.
- `SNMP poll of <host>:<port> took longer than 50 s (...)`: the poll's own budget (`POLL_BUDGET_SECONDS`). One GET and up to 15 walks at 15 s each could take 240 s, far past the collector's 60 s cut-off, so the integration ends the poll itself at 50 s with this message.

An agent that hides sysDescr and sysName fails the connection test with a "not readable" message. A poll still reports what it can read.

**Firewall:** `udp` to the configured port (161 by default), from the Vantage backend for both polls and connection tests, plus the replies back. The device's SNMP access list (community ACL, `snmpd.conf` `rocommunity ... <source>`, RouterOS `addresses`) must allow the backend's address.

**Cards:** `snmp-overview`, "SNMP device", 2 by 2: `info`, `uptime_seconds`, `interfaces`/`interfaces_polled`, `vantage_instance_up`. `snmp-ports`, "Ports", 4 by 2: per-interface `if_oper_status`, `if_admin_status`, `rate(if_in_bytes_total)`/`rate(if_out_bytes_total)`, errors and discards, speed, and `if_info` for names. The card components are frontend work (M5.5, with the switch faceplate).

**Limits and caveats:**

- Poll time: at most 50 s (`POLL_BUDGET_SECONDS`), inside the collector's `min(interval, 60)` s. Each single request is bounded by `timeout_seconds` x (`retries` + 1), at most 15 s.
- Walks use GETNEXT, one request per row and column. A switch with hundreds of interfaces needs thousands of requests per poll, and filtering doesn't reduce the walking, only what's reported. GETBULK isn't used yet.
- `aes192`/`aes256`: vendors disagree on key localisation for these (Blumenthal vs. Reeder). pysnmp uses the Blumenthal draft, so Cisco's variant may fail with a decryption error. `aes128` is interoperable.
- `sysUpTime` and `hrSystemUptime` are 32-bit TimeTicks and wrap after about 497 days.
- Written for the output of net-snmp on Linux (VyOS, Debian), Cisco IOS and RouterOS.

**Tested with:** VyOS 2025.06 rolling (live, in an isolated lab, 2026-10-28), SNMP v2c and v3 authPriv (SHA / AES-128). One poll gave 71 samples over 6 interfaces, all with 64-bit counters.

**Troubleshooting:**

- **"no SNMP response"** usually means the agent's listen address, or the community's client/network access list, doesn't match the Vantage backend's address. Check which address the agent listens on and which source addresses the community (or v3 user) accepts.
- **v3:** an unknown user or a wrong passphrase gets an error answer ("answered with an error"). A v3 request that gets no answer at all usually means v3 isn't active on the agent.
- **VyOS, v3 engine ID:** the stored v3 keys are derived from the engine ID. Setting or changing `service snmp v3 engineid` after the user was created invalidates them, and requests fail with a wrong-digest error ("answered with an error"). Set the user's plaintext passwords again after changing the engine ID.
- **Virtual interfaces** such as VyOS' `pim6reg` (or `lo`) are polled like any other. Leave them out with `exclude_interfaces`, for example `pim6reg`.

**Fixtures:** `backend/tests/integrations/builtin/fixtures/snmp_*.snmpwalk`, in `snmpwalk -On` format with example values only. `snmp_vyos_live` is a live capture from a lab VyOS 2025.06 rolling, scrubbed (sysContact and sysLocation set to `example`, MACs replaced with locally administered example values). The others are hand-written: `snmp_netsnmp_linux` (net-snmp on Linux, VyOS-like), `snmp_cisco_ios` (multi-line sysDescr, large ifIndex values, one interface without 64-bit counters), `snmp_routeros`, and `snmp_no_ifxtable` (an old ifTable-only agent). `tests/integrations/snmpfake.py` serves them as a fake agent behind the real guard and client. It emulates v1 (no Counter64, `noSuchName`), a wrong community (silence), v3 report errors, and an agent that goes silent partway.

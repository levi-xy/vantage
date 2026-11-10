# OPNsense (`opnsense`)

**Style:** polled (HTTPS API). **Category:** `firewall`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/opnsense/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

> **Status:** written against OPNsense's documented endpoints and hand-written fixtures. It has **not yet been checked against a real OPNsense** (lab check pending); some response shapes are marked below as unverified.

**What it reads:** the OPNsense REST API, `GET` only, through `ctx.http_client()` (the SSRF-guarded client). Every request goes through `_get_json`, which refuses any path not on the allowlist (`ENDPOINTS` / `ALLOWED_PATHS`, exact paths, no query) before anything is sent. One request per endpoint per poll (8 in all); default poll interval 60 s.

| Path | Page privilege it needs | Gives | Response shape |
|---|---|---|---|
| `GET /api/diagnostics/system/system_information` | Lobby: Dashboard | firmware version (`versions[0]`) | documented keys |
| `GET /api/diagnostics/system/system_time` | Lobby: Dashboard | uptime, load average | documented keys; format of `uptime`/`loadavg` strings unverified |
| `GET /api/diagnostics/system/system_resources` | Lobby: Dashboard | memory used / total | `memory.total`/`used` (bytes) documented, with `*_frmt` (MiB) as a fallback; unit unverified |
| `GET /api/diagnostics/firewall/pf_statistics/info` | Diagnostics: Firewall statistics | pf state-table entries | **unverified**: searched for `state-table.current-entries` anywhere in the answer |
| `GET /api/diagnostics/interface/get_interface_statistics` | Diagnostics: Netstat | per-interface byte counters, flags (up/running) | entry keys `name`, `flags`, `received-bytes`, `sent-bytes` **unverified**; link state is read from the `flags` (IFF_UP and IFF_RUNNING) |
| `GET /api/diagnostics/interface/get_interface_names` | not reported (optional) | interface descriptions (WAN, LAN) | device to description map |
| `GET /api/routes/gateway/status` | System: Gateways | gateway status, delay, stddev, loss | `{"items": [...]}` documented; per-gateway fields (`name`, `status`, `delay` "1.2 ms", `loss` "0.0 %") **unverified** |
| `GET /api/core/service/search` | Status: Services | service names and running flag | `rows[]` with `id`/`name`, `running` **unverified** |

Nothing else is called: no `POST`/`PUT`/`DELETE`, no `start`, `stop`, `restart`, `reconfigure`, `set`, `add`, `del`, `flush` or `kill` endpoint, no streaming endpoint, and no firmware endpoint. **Update-available is not shown**: it needs a firmware check, which makes the firewall do work (and `firmware/status` only returns the result of a check someone ran earlier). Redirects are not followed.

**Config:** see `OpnsenseConfig`.

- `host` and `port` (443 by default): found by name by the save-time SSRF check; must be on a private network. If the web GUI uses another port, use that.
- `api_key` and `api_secret`: both write-only secrets (`SecretStr`). Sent only as the HTTP Basic `Authorization` header, to the configured host. Never in a message, log line, status, metric or API response.
- `tls_ca_certificate`, `tls_insecure_skip_verify`, `timeout_seconds`: see TLS.

### Creating a read-only API user

1. **System > Access > Users > Add**: a dedicated user, e.g. `vantage`, with a long random password you don't keep (the API key is what is used). Leave "Login shell" empty.
2. On the user, under **Effective Privileges**, add only these pages (all are read-only views):
   - `Lobby: Dashboard`
   - `Status: Services`
   - `System: Gateways`
   - `Diagnostics: Netstat`
   - `Diagnostics: Firewall statistics`

   Do **not** grant "All pages", `System: Firmware`, or any page that can change settings or control services. (The privilege names are taken from OPNsense's own access-control lists. A page-level privilege grants every endpoint of that page, so the Services page also allows start/stop calls to a client that wanted to make them; Vantage never does. Keep the key to this one purpose.)
3. Save, then use the **API keys** section of the user (the key icon) to create a key. OPNsense downloads a file with `key=` and `secret=` lines: paste them into **API key** and **API secret**. The secret is shown only in that file.

The connection test tells you which of these pages the user can't read (for example "The API user lacks these privileges: System: Gateways"); the rest still work. If all pages are refused, it says so.

### TLS

OPNsense ships with a self-signed certificate. Verification is **on** by default, so the first test fails with "certificate not trusted". Fix it, in order of preference:

1. Give the firewall a certificate from a CA the Vantage host trusts (an internal CA or ACME), and connect by a name it covers.
2. Paste the firewall's certificate (or its CA, as PEM) into **Trusted certificate (PEM)**: from **System > Trust > Certificates** (the web GUI certificate, export the certificate, not the key). Only that certificate is then trusted, TLS 1.2 or newer, and the host must still match it (use the name or address in the certificate). The same field as the Proxmox integration: parsed line by line, at most 16 KiB, certificates only, never a private key.
3. **Last resort:** **INSECURE: don't verify the TLS certificate**. The API key and secret then go to whatever answers on that address. Only on a network you control. Can't be combined with a pasted certificate.

### Metrics

Prefix `vantage_opnsense_`; counters are exposed with `_total`. Caps: 64 interfaces, 32 gateways, 128 services; names must match a simple pattern or the entry is skipped; the interface description is a cleaned, length-capped label on `interface_info` only.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `info` | gauge (1) | `version` | firmware, e.g. `OPNsense 24.7.5-amd64` |
| `uptime_seconds`, `load1`, `load5`, `load15` | gauge | none | |
| `memory_used_bytes`, `memory_total_bytes` | gauge | none | |
| `pf_states` | gauge | none | state-table entries |
| `interface_up` | gauge | `interface` | 1 when the interface is up and running (from flags) |
| `interface_receive_bytes`, `interface_transmit_bytes` | counter | `interface` | |
| `interface_info` | gauge (1) | `interface`, `interface_name` | description (WAN, LAN) |
| `gateway_up` | gauge | `gateway` | 0 for `down`/`force_down`; `loss`/`delay` states count as up |
| `gateway_delay_seconds`, `gateway_stddev_seconds`, `gateway_loss_percent` | gauge | `gateway` | missing while the gateway reports no value |
| `service_running` | gauge | `service` | |
| `interfaces`, `gateways`, `services` | gauge | none | totals seen; `items_omitted{kind}` counts those beyond the caps |
| `endpoint_ok` | gauge | `endpoint` | 1 when that endpoint answered and was readable; 0 shows a missing privilege |

### Failure behaviour

A 401, an unreachable host, a TLS failure, a timeout or an answer that is not the API fails the whole poll with a fixed message (the instance goes `down` after the usual number of failed cycles). If only some endpoints are refused (403, or an odd answer), the others are still reported, the missing ones show as `endpoint_ok 0` and in the backend log (endpoint key and status code only). If no endpoint is readable the poll fails with "the API user can't read any page". The instance's status `error` shows only whole-poll failures; partial gaps are in `endpoint_ok` and the connection test.

### Cards

`opnsense-overview` (load, memory, states, firmware, uptime), `opnsense-interfaces` (link state, in/out rate), `opnsense-gateways` (status, latency, loss), `opnsense-services` (running or stopped, stopped first). All read through `/query` only.

### Firewall hint

The Vantage backend connects to the OPNsense API (TCP, `port`, default 443). A timeout or refused connection in the connection test carries this hint.

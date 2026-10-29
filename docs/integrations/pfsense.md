# pfSense (`pfsense`)

**Style:** scrape-only. **Category:** `firewall`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/pfsense/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** Prometheus scrapes the `/metrics` page of Telegraf's `prometheus_client` output, running on pfSense through the Telegraf package. Vantage does not poll and never talks to pfSense's web UI or API, so nothing can change firewall state. `test_connection` makes one `GET /metrics` through the guarded client (TLS verified, body capped at 8 MiB) and checks that the page carries at least one of the Telegraf metrics the cards read.

**Config**

| Field | Type | Default | Notes |
|---|---|---|---|
| `host` | string | none | IP or DNS name of pfSense; checked by the SSRF guard on save |
| `port` | integer | Telegraf's default | the `prometheus_client` listen port |
| `scheme` | `http` / `https` | `http` | `https` only if Prometheus trusts Telegraf's certificate |

See `PfSenseConfig` for bounds and defaults.

**Secrets:** none. Prometheus' config is fixed (Contract 4), so it scrapes with no credentials: the Telegraf listener must not require authentication. Restrict it with a pfSense firewall rule instead.

**Metrics:** Telegraf's native names, with the four standard `vantage_*` labels added by `/sd/targets`. The Telegraf config on pfSense must enable these inputs: `cpu`, `mem`, `net`, `pf` and `ping` (one `ping` target per gateway address). Used: `cpu_usage_*`, `mem_total` / `mem_used_percent`, `net_bytes_recv` / `net_bytes_sent` (label `interface`), `pf_entries` (state table size), `ping_average_response_ms`, `ping_percent_packet_loss`, `ping_packets_transmitted` and `ping_result_code` (label `url`, the gateway address). Telegraf emits `ping_average_response_ms` (and the other RTT series and `ping_ttl`) only when at least one reply came back: a down gateway has `ping_percent_packet_loss` 100 and no RTT series at all, only loss, `ping_packets_transmitted` / `ping_packets_received` and `ping_result_code`. Cards and alerts must detect a down gateway from `ping_percent_packet_loss` (for example `ping_percent_packet_loss >= 100`), never from a missing or stale RTT series. No `vantage_pfsense_*` metric and no `vantage_instance_up` are emitted.

**Status and failures:** always `unknown` (the collector does not poll it). Whether the scrape works is Prometheus' `up`. The connection test reports: refused by the guard (no hint), timeout or refused (with a firewall hint), non-200, a page that is not Telegraf firewall metrics, or a page that is too large. Gateways are counted by `url` from any `ping_*` series that is always present, so a gateway with 100% loss still counts. Messages quote only the configured host and port and counts of interfaces and gateways, never device text.

**Firewall:** TCP to the Telegraf port on pfSense, from Prometheus (scrapes) and from the backend (connection test). On pfSense this is a rule on the interface facing Vantage.

**Cards:**
- `pfsense-overview`, "pfSense", 4 by 3: `cpu_usage_idle`, `mem_used_percent`, `pf_entries`, `net_bytes_recv` / `net_bytes_sent` per interface.
- `pfsense-gateways`, "Gateways", 4 by 2: one row per gateway, keyed by `url` from `ping_percent_packet_loss` (present for every gateway); latency from `ping_average_response_ms` where it exists. A gateway with loss 100 has no latency series: show it as down, not as missing.

**Limits and caveats:** gateway latency and loss come from Telegraf's `ping` input, not from pfSense's own gateway monitoring (dpinger), so gateways appear only if you configure a ping target for each. Hostnames are resolved again by Prometheus when it scrapes, so only an IP-literal host is fully protected against DNS rebinding. The metric names are Telegraf's defaults; a custom Telegraf measurement prefix or name override will hide them.

**Fixtures:** `backend/tests/integrations/builtin/fixtures/telegraf_pfsense.prom`, hand-written and trimmed to the metrics above; documentation addresses only. `telegraf_pfsense_live.prom`: live capture, scrubbed (2026-10-28, pfSense with the Telegraf package): one interface and three ping targets, one of them down (loss 100, no RTT series); host `pfsense.example`, documentation addresses and loopback only.

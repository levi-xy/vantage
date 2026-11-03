# pfSense (`pfsense`)

**Style:** scrape-only. **Category:** `firewall`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/pfsense/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** Prometheus scrapes the `/metrics` page of Telegraf's `prometheus_client` output, running on pfSense through the Telegraf package. Vantage does not poll and never talks to pfSense's web UI or API, so nothing can change firewall state. `test_connection` makes one `GET /metrics` through the guarded client (TLS verified, body capped at 8 MiB) and checks that the page carries at least one of the Telegraf metrics the cards read.

**Config**

| Field | Type | Default | Notes |
|---|---|---|---|
| `host` | string | none | IP or DNS name of pfSense; checked by the SSRF guard on save |
| `port` | integer | Telegraf's default | the `prometheus_client` listen port |
| `scheme` | `http` / `https` | `http` | `https` only if Prometheus trusts Telegraf's certificate |
| `interface_names` | list of `interface=name` | empty | display names for the cards, e.g. `igc0=WAN` (name up to 32 characters). Wins over the description set on pfSense (see **Interface descriptions**) |
| `hidden_interfaces` | list of interface names | empty | left off the Interfaces card; still scraped |
| `gateway_names` | list of `address=name` | empty | **Deprecated, unused.** Kept so saved instances still validate; nothing reads it since the Gateways card shows picked interfaces instead of ping targets |

See `PfSenseConfig` for bounds and defaults.

**Secrets:** none. Prometheus' config is fixed (Contract 4), so it scrapes with no credentials: the Telegraf listener must not require authentication. Restrict it with a pfSense firewall rule instead.

**Metrics:** Telegraf's native names, with the four standard `vantage_*` labels added by `/sd/targets`. The Telegraf config on pfSense must enable these inputs: `cpu`, `mem`, `net` and `pf`. The `ping` input (one target per gateway address) is optional: the Gateways card no longer uses it, but the WAN status tile and the ping-based alert templates do. Used: `cpu_usage_*`, `mem_total` / `mem_used_percent`, `net_bytes_recv` / `net_bytes_sent` (label `interface`), `pf_entries` (state table size), `ping_average_response_ms`, `ping_percent_packet_loss`, `ping_packets_transmitted` and `ping_result_code` (label `url`, the gateway address). Telegraf emits `ping_average_response_ms` (and the other RTT series and `ping_ttl`) only when at least one reply came back: a down gateway has `ping_percent_packet_loss` 100 and no RTT series at all, only loss, `ping_packets_transmitted` / `ping_packets_received` and `ping_result_code`. Cards and alerts must detect a down gateway from `ping_percent_packet_loss` (for example `ping_percent_packet_loss >= 100`), never from a missing or stale RTT series. No `vantage_pfsense_*` metric and no `vantage_instance_up` are emitted.

**Interface descriptions (optional):** Telegraf's `net` input only knows the OS names (`igc0`). To show the descriptions set on pfSense (WAN, LAN, IoT), install `deploy/pfsense/vantage-ifdescr.php`. It reads `/cf/conf/config.xml`, writes nothing, and prints only each assigned interface's OS name and description (letters, digits, space, `_ . -`, up to 32 characters; the interface key in capitals, e.g. `OPT1`, when no description is set):

1. Copy the script to pfSense as `/usr/local/bin/vantage-ifdescr.php` (Diagnostics > Command Prompt > Upload File puts it in `/tmp`; move it from there).
2. In Services > Telegraf, add to "Additional configuration for Telegraf":

   ```toml
   [[inputs.exec]]
     commands = ["/usr/local/bin/php -f /usr/local/bin/vantage-ifdescr.php"]
     data_format = "influx"
     interval = "5m"
   ```

Telegraf then serves `pfsense_interface_info{interface="igc0",descr="WAN"} 1`. The Interfaces card and the throughput port picker name each interface by the `interface_names` setting first, then this description, then the OS name, which stays visible beside it. Without the script nothing changes.

**Status and failures:** always `unknown` (the collector does not poll it). Whether the scrape works is Prometheus' `up`. The connection test reports: refused by the guard (no hint), timeout or refused (with a firewall hint), non-200, a page that is not Telegraf firewall metrics, or a page that is too large. Gateways are counted by `url` from any `ping_*` series that is always present, so a gateway with 100% loss still counts. Messages quote only the configured host and port and counts of interfaces and gateways, never device text.

**Firewall:** TCP to the Telegraf port on pfSense, from Prometheus (scrapes) and from the backend (connection test). On pfSense this is a rule on the interface facing Vantage.

**Cards:**
- `pfsense-overview`, "Resources", 4 by 3 (the id predates the name, so saved dashboards keep it): `cpu_usage_idle`, `mem_used_percent`, `disk_used_percent{path="/"}` (Telegraf `disk` input), `pf_entries`, `system_uptime` (`system` input), and the hottest `temp_temp` only when Telegraf's `temp` input reports a sensor.
- `pfsense-interfaces`, "Interfaces", 4 by 3: `net_bytes_recv` / `net_bytes_sent` per interface, shown under `interface_names` or the pfSense description (`pfsense_interface_info`, optional), without `hidden_interfaces`, busiest first (in + out). The interface named `WAN` (or reported as `wan`) is pinned to the top; a VLAN interface `parent.vlan` (e.g. `igc1.10`) is indented under its parent when the parent is shown. Six rows, then "Show all N interfaces". The names are display-only: metric labels keep the OS names.
- `pfsense-gateways`, "Gateways", 4 by 2: the pfSense interfaces picked in the card's settings (edit the dashboard, then ⚙ on the card; up to 8, stored in the card's settings in the dashboard layout as `interface1` … `interface8`, OS names). No address is typed anywhere. The picker lists the interfaces in `net_bytes_recv` (without `lo`, `pflog`, `pfsync`, `enc` and `hidden_interfaces`), named like the Interfaces card. One row per picked interface, in the order picked: its name (the OS name beside it when renamed) and in/out from `8 * rate(net_bytes_recv/sent[5m])`. An interface missing from the latest scrape, or any interface while the device's data is stale, shows NO DATA in the critical colour; healthy rows are muted text. Below full size problems come first; small shows the first picked interface, or the first one without data. Nothing picked: "No interfaces chosen."

**Limits and caveats:** Telegraf's `net` input carries no link state and no gateway status, so the Gateways card can't say a gateway is down or lossy, only that an interface's counters stopped arriving (NO DATA). It doesn't read pfSense's own gateway monitoring (dpinger) or the `ping` input. Hostnames are resolved again by Prometheus when it scrapes, so only an IP-literal host is fully protected against DNS rebinding. The metric names are Telegraf's defaults; a custom Telegraf measurement prefix or name override will hide them.

**Fixtures:** `backend/tests/integrations/builtin/fixtures/telegraf_pfsense.prom`, hand-written and trimmed to the metrics above; documentation addresses only. `telegraf_pfsense_live.prom`: live capture, scrubbed (2026-10-28, pfSense with the Telegraf package): one interface and three ping targets, one of them down (loss 100, no RTT series); host `pfsense.example`, documentation addresses and loopback only.

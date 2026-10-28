# Cloudflare Tunnel (`cloudflared`)

**Style:** scrape-only. **Category:** `tunnel`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/cloudflared/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** the connector's own local metrics endpoint, started with cloudflared's `--metrics host:port` option. Prometheus scrapes `/metrics` directly; Vantage does not poll. `test_connection` makes one `GET /metrics` through the guarded client (TLS verified, body capped at 8 MiB) and checks that the page carries a `cloudflared_*` metric. Both are plain reads. This is not the Cloudflare cloud API (a separate integration), so there are no `cloud_hosts` and nothing leaves the private network.

**Config:** see `CloudflaredConfig`. `host` is found by name by the save-time check (IP literal or DNS name), together with `port`, which must be the one given to `--metrics`. cloudflared binds its metrics endpoint to localhost by default, so it has to be started with a reachable address for Prometheus to scrape it.

**Secrets:** none. The metrics endpoint is unauthenticated, and Prometheus' config is fixed (Contract 4), so it scrapes without credentials.

**Metrics:** cloudflared's native names, carrying the four standard labels from `/sd/targets`. The card reads `cloudflared_tunnel_ha_connections` (active connections to the edge), `cloudflared_tunnel_total_requests` and `cloudflared_tunnel_request_errors` (counters), `cloudflared_tunnel_response_by_code`, and the `cloudflared_proxy_connect_latency` histogram (time to reach the origin). Vantage emits no `vantage_cloudflared_*` metric and no `vantage_instance_up`.

**Status and failures:** always `unknown`: the collector does not poll this integration. Whether the scrape works is Prometheus' `up`. A host that fails DNS or the SSRF guard is left out of `/sd/targets`. A failed connection test shows a fixed message naming `host:port`, never exception text.

**Firewall:** TCP to the metrics port, from Prometheus (scrapes) and from the backend (connection test).

**Cards:** `cloudflared`, "Cloudflare Tunnel", 4 by 3.

**Limits and caveats:** hostnames are resolved again by Prometheus when it scrapes, so only an IP-literal host is fully protected against DNS rebinding. Metric names can vary between cloudflared releases; the connection test only requires some `cloudflared_`-prefixed metric. The version shown comes from `build_info` when present.

**Fixtures:** `backend/tests/integrations/builtin/fixtures/cloudflared.prom`, hand-written and trimmed, with fake values.

# Linux host (`linux_host`)

**Style:** scrape-only. **Category:** `host`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/linux_host/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** Prometheus scrapes node_exporter's `/metrics` directly; Vantage does not poll. `test_connection` makes one `GET /metrics` through the guarded client (TLS verified, body capped at 8 MiB) and checks that the page is node_exporter's by its `node_exporter_build_info` metric.

**Config:** see `LinuxHostConfig`. `host` is found by name by the save-time check (an ASCII host: IP literal or DNS name), together with `port`. `scheme` is `https` only if Prometheus trusts the exporter's certificate.

**Secrets:** none. Prometheus' config is fixed (Contract 4), so it scrapes with no credentials: node_exporter must answer without authentication.

**Metrics:** node_exporter's own names (`node_cpu_seconds_total`, `node_memory_*`, `node_filesystem_*`, ...) and Prometheus' `up`, carrying the four standard labels from `/sd/targets`. Vantage emits no `vantage_linux_host_*` metric and no `vantage_instance_up`.

**Status and failures:** always `unknown`: the collector does not poll this integration (it does not override `poll`). Whether the scrape works is Prometheus' `up`. A host that fails DNS or the SSRF guard is left out of `/sd/targets` and disappears from Prometheus instead of showing `up == 0`.

**Firewall:** TCP to the exporter port, from Prometheus (scrapes) and from the backend (connection test).

**Cards:** `linux-host`, "Linux host", 4 by 3.

**Limits and caveats:** hostnames are resolved again by Prometheus when it scrapes, so only an IP-literal host is fully protected against DNS rebinding.

**Fixtures:** `backend/tests/integrations/builtin/fixtures/node_exporter.prom`, hand-written and trimmed.

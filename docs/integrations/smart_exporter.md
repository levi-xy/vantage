# SMART disk health (`smart_exporter`)

**Style:** scrape-only. **Category:** `storage`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/smart_exporter/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** Prometheus scrapes [smartctl_exporter](https://github.com/prometheus-community/smartctl_exporter)'s `/metrics` directly; Vantage does not poll. `test_connection` makes one `GET /metrics` through the guarded client (TLS verified, body capped at 8 MiB) and checks the page by its `smartctl_version` metric. It is read-only: Vantage sends no request but that `GET`, and the exporter only runs `smartctl` queries against the disks (it needs read access to the disk devices on its own host, not anything Vantage grants).

**Config:** see `SmartExporterConfig`. `host` is found by name by the save-time check (an ASCII host: IP literal or DNS name), together with `port`. `scheme` is `https` only if Prometheus trusts the exporter's certificate.

**Secrets:** none. Prometheus' config is fixed (Contract 4), so it scrapes with no credentials: the exporter must answer without authentication.

**Metrics:** native smartctl_exporter names, carrying the four standard labels from `/sd/targets`. The card reads `smartctl_device` (disk model/serial/interface info, label `device`), `smartctl_device_smart_status` (per disk, `1` = SMART overall-health passed, `0` = failed) and `smartctl_device_temperature` (per disk, Celsius). Vantage emits no `vantage_smart_exporter_*` metric and no `vantage_instance_up`.

**Status and failures:** always `unknown`: the collector does not poll this integration. Whether the scrape works is Prometheus' `up`. A host that fails DNS or the SSRF guard is left out of `/sd/targets`. In the connection test, a refused connection or timeout returns a firewall hint; a page that is not smartctl_exporter's or an HTTP error returns a plain failure.

**Firewall:** TCP to the exporter port (default 9633), from Prometheus (scrapes) and from the backend (connection test).

**Cards:** `smart-disks`, "Disk health", 4 by 3; reads the three metrics above.

**Limits and caveats:** disks the exporter cannot read (permissions, RAID controllers it is not configured for) are simply absent. Hostnames are resolved again by Prometheus when it scrapes, so only an IP-literal host is fully protected against DNS rebinding.

**Setup on a NAS:** [exporters/nas.md](../exporters/nas.md) and the compose bundle in `deploy/exporters/nas/`, which runs the exporter with only the listed disks and `SYS_RAWIO` instead of `privileged`.

**Fixtures:** `backend/tests/integrations/builtin/fixtures/smartctl_exporter.prom`, hand-written and trimmed (fake serials).

# Linux host (`linux_host`)

**Style:** scrape-only. **Category:** `host`. **Version:** `0.2.0`. Code: `backend/app/integrations/builtin/linux_host/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** Prometheus scrapes node_exporter's `/metrics` directly; Vantage does not poll. `test_connection` makes one `GET /metrics` through the guarded client (TLS verified, body capped at 8 MiB) and checks that the page is node_exporter's by its `node_exporter_build_info` metric. Its message names the version and node, and, where present, the md arrays, ZFS pools and temperature sensors it found, plus any array or pool that isn't healthy (at most five named), e.g. `node_exporter 1.12.1 on nas-example: 2 md arrays, 1 ZFS pool, 5 temperature sensors; not healthy: md1`. An md array is not healthy when `node_md_degraded` > 0, `node_md_disks{state="failed"}` > 0 or `node_md_state{state="inactive"}` is 1; a ZFS pool when its current `node_zfs_zpool_state` isn't `online`. The test still passes in that case: it tests the connection, and the admin sees the state before saving.

**Config:** see `LinuxHostConfig`. `host` is found by name by the save-time check (an ASCII host: IP literal or DNS name), together with `port`. `scheme` is `https` only if Prometheus trusts the exporter's certificate.

**Secrets:** none. Prometheus' config is fixed (Contract 4), so it scrapes with no credentials: node_exporter must answer without authentication.

**Metrics:** node_exporter's own names (`node_cpu_seconds_total`, `node_memory_*`, `node_filesystem_*`, ...) and Prometheus' `up`, carrying the four standard labels from `/sd/targets`. Vantage emits no `vantage_linux_host_*` metric and no `vantage_instance_up`. On a NAS (node_exporter's default collectors, v1.10 or later for md data from sysfs):

| Area | Metrics |
|---|---|
| Software RAID (mdadm) | `node_md_state{device,state}` (`1` on the current state: active, inactive, recovering, resync, check), `node_md_disks{device,state}` (active / failed / spare), `node_md_disks_required`, `node_md_degraded`, `node_md_blocks`, `node_md_blocks_synced` (rebuild progress) |
| ZFS | `node_zfs_zpool_state{zpool,state}` (`1` on the current state), where the kernel exposes `/proc/spl/kstat/zfs/<pool>/state` (OpenZFS 0.8 and later) |
| Sensors | `node_hwmon_temp_celsius{chip,sensor}`, `node_hwmon_fan_rpm{chip,sensor}`, labels in `node_hwmon_sensor_label` |
| Volumes | `node_filesystem_size_bytes`, `node_filesystem_avail_bytes`, `node_filesystem_readonly` (need the host root mounted when node_exporter runs in a container) |

**Status and failures:** always `unknown`: the collector does not poll this integration (it does not override `poll`). Whether the scrape works is Prometheus' `up`. A host that fails DNS or the SSRF guard is left out of `/sd/targets` and disappears from Prometheus instead of showing `up == 0`.

**Firewall:** TCP to the exporter port, from Prometheus (scrapes) and from the backend (connection test).

**Cards:** `linux-host`, "Linux host", 4 by 3. It reads CPU, memory, load and filesystems, and shows the RAID, ZFS and sensor metrics above only when the host has them, so a NAS needs no separate card type. The NAS drive-bay view (M5.5) combines this instance with a `smart_exporter` instance; its card spec is left to M5.5, which has to decide how one card refers to a second instance.

**Limits and caveats:** hostnames are resolved again by Prometheus when it scrapes, so only an IP-literal host is fully protected against DNS rebinding. Unraid's array uses its own parity driver, not mdadm, so it reports no `node_md_*`.

**Setup on a NAS:** [exporters/nas.md](../exporters/nas.md) and the compose bundle in `deploy/exporters/nas/`.

**Fixtures:** `backend/tests/integrations/builtin/fixtures/node_exporter.prom` (a plain host) and `node_exporter_nas.prom` (a NAS: a healthy and a degraded md array, a ZFS pool, hwmon temperatures and fans, filesystems), hand-written with example values.

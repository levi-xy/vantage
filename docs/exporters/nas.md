# Monitoring a NAS

A NAS shows up in Vantage as two instances: a **Linux host** (node_exporter, port 9100)
for CPU, memory, volumes, RAID, ZFS and sensors, and a **SMART disk health** instance
(smartctl_exporter, port 9633) for each disk's health and temperature. Both exporters
run on the NAS from one compose file:
[`deploy/exporters/nas/`](../../deploy/exporters/nas/). Read its
[README](../../deploy/exporters/nas/README.md) first: it explains what each container
is given and why, which disks to list, and the firewall rule.

Examples below use `10.0.0.30` for the NAS and `10.0.0.10` for the Vantage host.

> **Not tested on real hardware.** The steps for each NAS OS follow the vendors'
> documented container features, but nobody has run them on these systems for this
> project yet. Menu names change between OS versions. Where a step says **steps to
> verify**, check the result before relying on it, and please report what differs.

## 1. Prepare

On any NAS OS:

1. Enable SSH for an administrator account (needed at least to list the disks).
2. List the disks and edit the `devices:` list in `docker-compose.yml`
   ([Which disks](../../deploy/exporters/nas/README.md#which-disks)).
3. Decide the listen address: the NAS's LAN address in `NAS_LISTEN_ADDRESS` (an `.env`
   file next to the compose file), or replace `${NAS_LISTEN_ADDRESS:-0.0.0.0}` in the
   compose file with it if your OS's compose UI doesn't read `.env` files.

## 2. Start the exporters

### UGOS Pro (UGREEN)

Since UGOS Pro 1.19, the Docker app's **Projects** UI refuses a bind mount of the host
root (`/`), which node_exporter needs for volume usage. Start the project over SSH
instead:

1. Control Panel → Terminal → enable SSH. Log in as an administrator.
2. Copy `deploy/exporters/nas/` to a shared folder, for example
   `/volume1/docker/vantage-nas-exporters/`, and edit the device list there.
3. Start it:

   ```
   cd /volume1/docker/vantage-nas-exporters
   sudo docker compose up -d
   sudo docker compose ps
   ```

4. Leave SSH disabled again afterwards if you don't use it. The containers restart on
   boot by themselves (`restart: unless-stopped`).

The containers may appear in the Docker app's container list. Manage them with
`docker compose` over SSH, not by editing them in the UI, which could recreate them
without the root mount.

**If the root can't be mounted at all** (a future UGOS release blocking it for the CLI
too): remove the `/:/host:ro,rslave` volume and mount each data volume instead, e.g.
`- /volume1:/host/volume1:ro,rslave`, keeping `--path.rootfs=/host`. What is lost:
usage of the system partition and of any volume you didn't mount (they report
`node_filesystem_device_error`), and udev disk labels. RAID (`node_md_*`), sensors
(`node_hwmon_*`), memory, CPU, load and network metrics come from the kernel's `/proc`
and `/sys` and still work. **Steps to verify:** `curl -s http://10.0.0.30:9100/metrics |
grep -E '^node_(md_state|hwmon_temp_celsius|filesystem_size_bytes)'`.

UGOS Pro storage pools are expected to be Linux software RAID (mdadm), in which case
`node_md_*` is present. **Steps to verify** with the same command.

### TrueNAS SCALE

TrueNAS SCALE 24.10 (Electric Eel) and later run apps on Docker and accept a compose
file as a custom app:

1. Apps → Discover Apps → the ⋮ menu → **Install via YAML**.
2. Name it `vantage-nas-exporters` and paste `docker-compose.yml` with your device list
   and listen address filled in (the YAML box doesn't read an `.env` file).
3. Save; the app should reach **Running**.

Older SCALE releases (Kubernetes-based apps) can't run this compose file as is; the
"Custom App" form there has no equivalent for every setting. Upgrade to 24.10 or later,
or run the two containers by hand with the same settings.

TrueNAS storage is ZFS: pool health comes from `node_zfs_zpool_state`, and there is no
mdadm. TrueNAS has no host firewall UI; restrict 9100 and 9633 with the listen address
and a firewall in front of the NAS, or with `iptables` rules from an init script
(System → Advanced → Init/Shutdown Scripts). **Steps to verify:** `curl -s
http://10.0.0.30:9100/metrics | grep '^node_zfs_zpool_state'` shows one `1` per pool,
on the line with its current state.

### Unraid

**Compose Manager plugin (recommended):**

1. Apps (Community Applications) → install **Docker Compose Manager**.
2. Docker tab → Compose section → **Add New Stack**, name it `vantage-nas-exporters`.
3. Edit the stack, paste `docker-compose.yml` with your device list, and optionally the
   `.env` contents in the stack's env file. **Compose Up**.

**User templates instead:** Docker tab → **Add Container**, once per exporter, with
the same image tag, network type `Host` (node_exporter) or `Bridge` with port 9633
(smartctl_exporter), the `/` → `/host` path as read-only (slave) for node_exporter,
the devices for smartctl_exporter, the command-line flags as **Post Arguments**, and
the remaining settings as **Extra Parameters**, for example `--pid=host --read-only
--cap-drop=ALL --security-opt=no-new-privileges --pids-limit=64 --user=65534:65534` for
node_exporter. The compose file is the reference for every setting.

The Unraid array is **not** Linux mdadm: it uses Unraid's own parity driver, so expect
no `node_md_*` metrics for it. ZFS pools (Unraid 6.12 and later) do report
`node_zfs_zpool_state`. Disk health of array disks comes from the SMART instance; the
exporter doesn't wake spun-down disks. Unraid has no host firewall UI: use the listen
address and a firewall in front of the NAS. **Steps to verify:** which `node_md_*` and
`node_zfs_*` series appear on your system.

### OpenMediaVault

1. System → Plugins → install **openmediavault-compose**.
2. Services → Compose → Settings: choose a shared folder for compose files.
3. Services → Compose → Files → **Create**: name it `vantage-nas-exporters`, paste
   `docker-compose.yml` (device list filled in) and, if you like, the `.env` contents
   in the environment field. Save, then **Up**.

OMV's software RAID is Linux mdadm, so `node_md_*` is present; ZFS pools (with the
openmediavault-zfs plugin) report `node_zfs_zpool_state`. OMV's own firewall (System →
Network → Firewall) filters port 9100; port 9633 is Docker-published and needs the
`DOCKER-USER` rule from the README. **Steps to verify:** the curl checks below.

## 3. Check the exporters

From the **Vantage host**:

```
curl -s http://10.0.0.30:9100/metrics | grep -E '^node_(md_state|md_disks|zfs_zpool_state|hwmon_temp_celsius|hwmon_fan_rpm|filesystem_size_bytes)'
curl -s http://10.0.0.30:9633/metrics | grep -E '^smartctl_device(_smart_status|_temperature)?\{'
```

From any other machine, both requests should fail (firewall).

## 4. Add the NAS to Vantage

As an admin, add two instances with the same **site**:

| Instance | Integration | Host | Port |
|---|---|---|---|
| e.g. `nas-1` | Linux host (node_exporter) | `10.0.0.30` | 9100 |
| e.g. `nas-1-disks` | SMART disk health (smartctl_exporter) | `10.0.0.30` | 9633 |

Use **Test connection** before saving. A working Linux host test names the
node_exporter version and the NAS, plus what it found for a NAS: md arrays, ZFS pools
and temperature sensors, and any array or pool that isn't healthy, e.g.
`node_exporter 1.12.1 on nas-1: 2 md arrays, 1 ZFS pool, 5 temperature sensors; not
healthy: md1`. If it finds no arrays, pools or sensors where you expect them, check the
container's mounts before anything else. The SMART test reports the smartctl version
and how many disks it sees; compare that with your device list.

Both instances are scrape-only, so their status stays `unknown`; Prometheus' `up`
shows whether scraping works. The cards: the Linux host card (CPU, memory, volumes,
and RAID, ZFS and sensors where present) and the Disk health card. A drive-bay view
that combines both instances is planned with the dashboard cards (M5.5).

## What isn't covered

Per-container stats (cAdvisor), virtual machines (libvirt) and UPS (NUT) are out of
scope for this bundle; each may become an optional add-on.

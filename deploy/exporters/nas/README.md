# NAS exporter bundle

Two Prometheus exporters for a NAS, in one compose file you run **on the NAS**. It is
not part of the Vantage stack in `deploy/`: Vantage's Prometheus scrapes these
exporters over the network, and Vantage itself only reads them.

| Service | Image | Port | Feeds the Vantage integration |
|---|---|---|---|
| `node-exporter` | `prom/node-exporter:v1.12.1` (2026-07-14) | 9100 | Linux host (`linux_host`) |
| `smartctl-exporter` | `prometheuscommunity/smartctl-exporter:v0.15.0` (2026-10-07) | 9633 | SMART disk health (`smart_exporter`) |

Both are Apache-2.0, from the Prometheus project and Prometheus community. Tags are
pinned to exact releases; to upgrade, change the tag after reading the release notes.

Per-OS setup (UGOS Pro, TrueNAS SCALE, Unraid, OpenMediaVault) and adding the NAS to
Vantage: [docs/exporters/nas.md](../../../docs/exporters/nas.md).

## Quick start

1. Copy this directory to the NAS.
2. Edit the `devices:` list of `smartctl-exporter` to name this NAS's disks
   ([Which disks](#which-disks)).
3. Optional, recommended: create `.env` next to the compose file with the NAS's LAN
   address, so both exporters listen only there:

   ```
   NAS_LISTEN_ADDRESS=10.0.0.30
   ```

4. `docker compose up -d`, then restrict the ports ([Firewall](#firewall)).

## No authentication

Neither exporter asks for credentials, and Vantage can't send any: its Prometheus
config is fixed and holds nothing device-specific. Anyone who can reach port 9100 or
9633 can read the metrics, which include the host name, kernel version, mount points,
disk models and serial numbers. Nobody can change anything through them (both serve
read-only `GET /metrics`), but treat the data as internal and let only the Vantage host
connect.

## Firewall

Allow TCP 9100 and 9633 **from the Vantage host only** (Prometheus scrapes from there,
and Vantage's "Test connection" too) and drop them from everywhere else. Examples with
the NAS at `10.0.0.30` and Vantage at `10.0.0.10`:

- **NAS firewall UI** (where the OS has one): an allow rule for TCP 9100 and 9633 from
  source `10.0.0.10`, then a deny rule for those ports from any source, in that order.
- **node_exporter (port 9100)** runs in the host network, so ordinary host rules apply:

  ```
  iptables -A INPUT -p tcp --dport 9100 -s 10.0.0.10 -j ACCEPT
  iptables -A INPUT -p tcp --dport 9100 -j DROP
  ```

- **smartctl_exporter (port 9633)** is a Docker-published port. Docker forwards it
  before the host's `INPUT` rules (and before UFW or most NAS firewall UIs) see it, so
  filter it in Docker's `DOCKER-USER` chain:

  ```
  iptables -I DOCKER-USER -p tcp --dport 9633 ! -s 10.0.0.10 -j DROP
  ```

How rules persist across reboots depends on the OS; a network firewall or VLAN between
clients and the NAS works too. Setting `NAS_LISTEN_ADDRESS` keeps the exporters off
other interfaces (Docker bridges, a VPN, a second NIC) but doesn't replace the rule.

**Steps to verify:** from the Vantage host, `curl -s http://10.0.0.30:9100/metrics |
head` answers; from another machine on the LAN the same command times out or is
refused. Repeat for 9633.

## What each container is given, and why

Both containers: read-only root filesystem, `cap_drop: [ALL]`, `no-new-privileges`, a
pids limit and a memory limit, `restart: unless-stopped`, and never `privileged`.

### node_exporter

node_exporter is built to read the host it runs on. In a container, three things make
it see the NAS instead of the container:

| Setting | Why | Without it |
|---|---|---|
| `/:/host:ro,rslave` with `--path.rootfs=/host` | Filesystem sizes and mount points of the NAS's volumes and pools; udev disk labels (`--path.udev.data`). Read-only; `rslave` makes pools mounted after the container starts visible. | `node_filesystem_*` shows only the container's own mounts: no volume or pool usage. |
| `pid: host` | Process counts and per-process data of the NAS. | Process metrics describe the container. |
| `network_mode: host` | `node_network_*` for the NAS's real NICs. The listen address comes from `--web.listen-address`. | Network metrics describe the container's virtual interface. |

It runs as `nobody` (65534) with no capabilities, so it reads only what is
world-readable: that is enough for every default collector. RAID (`node_md_*`, from
`/proc/mdstat` and md sysfs), ZFS (`node_zfs_*`, from `/proc/spl/kstat/zfs`), sensors
(`node_hwmon_*`, from `/sys/class/hwmon`), memory, CPU and load come from the kernel's
`/proc` and `/sys`, which the container sees even without the root mount.

The collectors a NAS dashboard needs are all on by default in v1.12.1: `mdadm`, `zfs`,
`hwmon`, `filesystem`, `diskstats`, `netdev`, `thermal_zone`, `meminfo`, `cpu`,
`loadavg`, `uname`. None is turned off: the remaining defaults cost little and are
harmless when the data doesn't exist (no ZFS module loaded means no `node_zfs_*`).

### smartctl_exporter

Reading SMART data sends ATA/SCSI pass-through commands to the disk, which the kernel
allows only to a process with `CAP_SYS_RAWIO`. Upstream's example runs the container
`privileged` as root; this file doesn't:

- **Only the listed disks** are passed in with `devices:`. The exporter scans `/dev`
  inside the container and finds just those.
- **uid 0, but with every capability dropped except `SYS_RAWIO`.** uid 0 opens the disk
  nodes as their owner (`root:disk`, mode 0660) without `CAP_DAC_OVERRIDE`.
- **`SYS_ADMIN` is off by default.** The kernel lets NVMe admin commands, including
  "Get Log Page" that holds the NVMe SMART log, through only with `CAP_SYS_ADMIN`. If
  the NAS has NVMe drives you want covered, uncomment `SYS_ADMIN` and the
  `/dev/nvme0` line. The trade-off: `SYS_ADMIN` is a broad capability (mounts,
  namespaces and many device ioctls), so a compromised exporter could do much more
  with it. The container stays read-only, unprivileged and limited to the listed
  devices, but leave it off unless you need NVMe health. Without it, NVMe drives are
  simply missing from the SMART card; SATA and SAS disks are unaffected.
- The trade-off of `SYS_RAWIO` itself: pass-through commands can also write to the
  listed disks. The exporter only issues read commands (`smartctl` queries), but the
  capability doesn't enforce that, which is why only the disks you list are passed in.

The exporter's default `--smartctl.powermode-check=standby` skips disks that are spun
down, so polling doesn't wake sleeping disks; such a disk shows its last values.

## Which disks

On the NAS, list the whole disks (not partitions):

```
lsblk -d -o NAME,MODEL,SERIAL,SIZE,TRAN,TYPE
smartctl --scan
ls -l /dev/disk/by-id/ | grep -v part
```

Put one line per disk under `devices:`, for example `- /dev/sdc:/dev/sdc`. NVMe drives
use the controller node, `/dev/nvme0`, not `/dev/nvme0n1`. A listed device that doesn't
exist stops the container from starting.

`sdX` names can change between reboots. To keep a disk's name stable, map its
`/dev/disk/by-id/` link to a fixed name inside the container, e.g.
`- /dev/disk/by-id/ata-EXAMPLE_MODEL_EXAMPLE-SERIAL:/dev/sda`. The name inside the
container is what Vantage shows as the disk's `device` label.

Disks behind a hardware RAID controller need a device type (`--smartctl.device`
with `;megaraid,N` and similar); see the smartctl_exporter README. Most NAS units use
plain SATA/SAS (HBA) disks and need nothing extra.

## Upgrading

Change the image tags, then `docker compose pull && docker compose up -d`. Release
notes: <https://github.com/prometheus/node_exporter/releases> and
<https://github.com/prometheus-community/smartctl_exporter/releases>.

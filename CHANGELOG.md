# Changelog

All notable changes, newest first. Versions follow [semantic versioning](https://semver.org/); before 1.0, a minor version (0.2) may change behaviour, and its notes say how to upgrade.

## 0.1.1 (unreleased)

- The version is shown in the UI: "Vantage <version>" in the sidebar footer and a new **Settings → About** page (open to viewers too) with the release version ("Built as 0.1.1-rc.1" next to it when the images were built as a release candidate), the server's and the web app's source commits and build dates, database schema, Prometheus version and uptime. A warning appears when the web app and the server come from different builds (a stale browser tab or a half-pulled upgrade). New read-only `GET /api/v1/about`; `GET /api/v1/me` gains `server_version` and `server_commit`.
- Final releases ship the exact images of their tested release candidate (promoted by digest, not rebuilt). Both compose files therefore tell the backend which release it is: the backend's new `VANTAGE_RELEASE` setting, next to the image tags in the standalone `compose.yml`, and generated from the image version in the setup.sh bundle (it follows `VANTAGE_VERSION` in `.env`). Images now carry their source commit (`org.opencontainers.image.revision`).
- Upgrading needs no action: pull both images and recreate the stack. No migration. With a compose file from 0.1.0 (no `VANTAGE_RELEASE`), Settings → About shows the version the images were built as (`0.1.1-rc.N`) instead of 0.1.1; take this release's compose file (standalone `compose.yml`, or the bundle with `./setup.sh`, see [OPERATIONS.md](docs/OPERATIONS.md#upgrading)) to see 0.1.1.

## 0.1.0 (2026-11-12)

The first release.

**Platform**
- Devices are added from the UI: pick a type, fill the generated form, test the connection (with a firewall hint when it times out), save. Secrets are encrypted at rest (AES-GCM, bound to their row) and never returned.
- FastAPI backend, Prometheus underneath (HTTP service discovery, a fixed bootstrap config), React frontend served by nginx over HTTPS. One `setup.sh` creates the keys, the certificate and `.env`, and starts the stack.
- A standalone compose file for Portainer, Dockge and similar (`compose.yml` on each release, also shown in the README) that needs no files on the host: a one-shot `init` container creates the master key, the Prometheus token, a self-signed certificate and the Prometheus config in volumes. Release downloads also come without a version in their name (`vantage-deploy.tar.gz`, `compose.yml`), so the README links always fetch the latest release.
- Runs under Docker in an unprivileged Proxmox LXC: the backend's ping permission (`net.ipv4.ping_group_range`) is `0 65535` instead of the kernel maximum, which such a container refused, and the backend user's group has a fixed ID (10001) inside it.
- First-run setup needs a one-time setup code that `setup.sh` prints (or `python -m app.cli setup-code`), so nobody else on the network can create the first admin.
- Users with admin and viewer roles, persistent login lockout, sessions stored hashed, an audit log (with usernames) for every change.
- Every outbound connection goes through the SSRF guard (private ranges by default; loopback, link-local, metadata and the host's own addresses always refused; DNS resolved once).

**Integrations:** Linux host (node_exporter), SMART (smartctl_exporter), Proxmox VE, pfSense (Telegraf), SNMP device (v1, v2c, v3), MikroTik SwOS, cloudflared, Cloudflare account, Emby, HTTP check, ping check.

- An optional exporter bundle to run on a NAS (node_exporter + smartctl_exporter); on UGREEN UGOS Pro and Synology DSM it is pasted into the Docker app's web UI, no SSH.

**Dashboard**
- Several dashboards as tabs; cards dragged, resized and shown in less detail when small; a phone layout. A new device's cards are put on the main dashboard.
- Hardware-shaped cards: switch front panel, pfSense interfaces (VLANs grouped) and gateways, Proxmox nodes and guests (with tags), drive health, Emby (with a live "Now playing" list, never stored).
- Each device has its own colour on its cards; WAN throughput with live rates.
- Header health (Healthy / Warning / Critical) with a list of what's wrong.

**Network maps**
- A Map page with one or more maps that admins draw: devices (card, cloud, switch, NAS and access-point shapes, icons from the dashboard-icons set), front panels with RJ45 jacks and SFP cages, cables with speed, style and VLANs, Proxmox hosts with their guests filled in, text, tables, a VLAN list and a legend.
- Live port and device state, cable rates on hover; on a phone, a list of the map's devices and problems instead of the drawing.

**Alerts**
- Rules from templates or PromQL, one alert per series, Telegram channels.
- An alert resolves only after two clean checks on two different polls; a missing value moves it to No data instead of resolving it.
- Delivery through an outbox with retries for 24 hours (at least once, never lost); a banner when messages are stuck.
- Vantage's own alerts: Prometheus unreachable (saying why and how to fix it), metrics not collected, evaluator stalled, master key mismatch. `/healthz` for an external watchdog.

**Operations:** one timezone, chosen by an admin, for every page, message and log line (the clock stays the host's); backup, offline restore and master-key rotation (`app.cli`), guarded by a maintenance lock; signed multi-arch images on GitHub Container Registry with an SBOM; an install bundle per release.

**License:** MIT (the release candidates up to 0.1.0-rc.2 were Apache 2.0).

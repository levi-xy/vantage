# Vantage

**A self-hosted, security-first dashboard for homelabs and small infrastructure.**

[![CI](https://github.com/levi-xy/vantage/actions/workflows/ci.yml/badge.svg)](https://github.com/levi-xy/vantage/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/levi-xy/vantage?include_prereleases&sort=semver)](https://github.com/levi-xy/vantage/releases)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Vantage puts your firewall, hypervisor, NAS, switches, tunnels and services on one live dashboard, with cards shaped like the hardware instead of generic graphs. You add devices from the web UI: no config files, and as many of each type as you need. It only ever reads from your devices, and it alerts you on Telegram when something goes wrong.

## Features

- **Add devices from the UI.** Pick a type, fill in the form, test the connection, save. If the test times out, Vantage tells you which firewall rule is missing.
- **Hardware-shaped cards.** Switch front panels with live port state, pfSense interfaces and gateways, Proxmox nodes and guests, drive health, tunnel traffic, what's playing on Emby.
- **Dashboards you arrange.** Drag and resize cards, keep several dashboards as tabs, and use it on your phone too. Each device has its own colour, so its cards are easy to spot.
- **Network maps.** Draw your network yourself: devices with real port panels, cables with speed and VLANs, Proxmox hosts with their guests. Status and traffic on the map are live.
- **History and alerts.** Prometheus stores the history. You manage alert rules in the UI, and alerts go to Telegram. A missing value never counts as "recovered": the alert shows **No data** until the device answers again.
- **Watches itself.** Vantage alerts you when Prometheus is down, metrics stop arriving or alert checks stall, and `/healthz` lets an outside monitor watch Vantage.
- **Secure by default.** It never changes anything on a device. Credentials are encrypted and never sent back to the browser. Every outbound connection is checked, the dashboard is served over HTTPS, there are admin and viewer roles, and every change is in the audit log.

## Quick start

You need Docker Engine with Compose v2 (2.17 or newer) and `openssl`. Images are published for amd64 and arm64.

Download the install bundle from the [latest release](https://github.com/levi-xy/vantage/releases/latest) and run the setup script:

```bash
V=0.1.0   # the release version
curl -fLO https://github.com/levi-xy/vantage/releases/download/v$V/vantage-deploy-$V.tar.gz
tar -xzf vantage-deploy-$V.tar.gz && cd vantage-deploy-$V
./setup.sh
```

`setup.sh` creates the master key, the Prometheus token, a self-signed HTTPS certificate and `.env` (it never replaces ones that already exist), starts the stack and prints the address: `https://<server-address>:8443`, reachable from any machine on your LAN, plus a one-time setup code. Open it, create the admin account with that code, then add your first device.

**Back up `secrets/master.key` somewhere off the server.** Without it, stored device credentials can't be decrypted, and it isn't part of Vantage's own backups.

<details>
<summary>Verify the download (optional)</summary>

To check that the bundle arrived intact, run this in the folder you downloaded it to:

```bash
curl -fLO https://github.com/levi-xy/vantage/releases/download/v$V/SHA256SUMS
sha256sum -c SHA256SUMS
```

The images are signed with [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) (keyless, tied to the release workflow):

```bash
cosign verify ghcr.io/levi-xy/vantage-backend:$V \
  --certificate-identity-regexp '^https://github.com/levi-xy/vantage/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Do the same for `vantage-frontend`.
</details>

<details>
<summary>Build from source instead</summary>

```bash
git clone https://github.com/levi-xy/vantage.git
cd vantage/deploy
./setup.sh
```
</details>

Next steps: [reach it from other machines](docs/OPERATIONS.md#reaching-the-dashboard-from-other-machines), [use your own certificate](docs/OPERATIONS.md#https-certificate), [backups](docs/OPERATIONS.md#backups) and [upgrading](docs/OPERATIONS.md#upgrading).

## Supported integrations

| Integration | Collects via | What you see |
|---|---|---|
| Linux host | node_exporter | CPU, memory, disks, network for any Linux server or NAS |
| SMART add-on | smartctl_exporter | Disk health and temperatures |
| Proxmox VE | API (read-only token) | Nodes, VMs, containers, storage, guest tags |
| pfSense | Telegraf | Interfaces, gateways, firewall states, resources |
| SNMP device | SNMP v1, v2c, v3 | Switches, routers, UPSes, printers |
| MikroTik SwOS | SNMP | CRS/CSS switches running SwOS |
| cloudflared | Metrics endpoint | Tunnel connections, requests, errors |
| Cloudflare account | API (read-only token) | Site traffic, cache, threats, tunnel health |
| Emby | API | Now playing, transcoding, libraries |
| HTTP / ping check | Probe from Vantage | Anything with a URL or an IP address |

Not listed? The Linux host, SNMP and HTTP/ping integrations cover most equipment, and new types can be added as plugins ([how](docs/INTEGRATIONS.md)). Setup guides for each integration are in [docs/integrations](docs/integrations).

## How it works

```
            Browser
               │  HTTPS
        ┌──────▼───────┐
        │  Frontend    │  React, served by nginx
        └──────┬───────┘
               │  REST + WebSocket (the only thing the browser talks to)
        ┌──────▼──────────────────────────────────────────┐
        │  Backend (FastAPI)                              │
        │   • Integration plugins                         │
        │   • Encrypted credential store (SQLite/Postgres)│
        │   • Pollers for API/SNMP devices → /metrics     │
        │   • Target discovery for Prometheus             │
        │   • Query proxy, alerts, audit log              │
        └──────┬───────────────────────────┬──────────────┘
               │ discovery + /metrics      │ API / SNMP polls
        ┌──────▼───────┐                   ▼
        │  Prometheus  │──scrape──►  exporters on devices
        └──────────────┘
```

When you save a device, the backend stores it and Prometheus picks it up through HTTP service discovery within a minute. Devices with an API (Proxmox, Cloudflare, Emby, SNMP) are polled by the backend and their values exposed as metrics, so their history lands in Prometheus too. You never edit `prometheus.yml`. More in [ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Security

Vantage holds a map of your network and credentials to your devices, so it's built to limit the damage if it's ever compromised:

- **Read-only:** integrations use read-only tokens and roles wherever the device offers them, and Vantage never changes device configuration.
- **Secrets stay in the backend:** encrypted at rest with a master key kept outside the database, write-only in the UI, never in responses, logs or the audit log.
- **The browser talks only to the backend**, never to devices or Prometheus.
- **Outbound connections are checked:** private address ranges by default; loopback, link-local, cloud metadata and the Vantage host's own addresses are always refused; the internet only for the fixed API hosts of cloud integrations.
- **Accounts:** Argon2 password hashing, server-side sessions, CSRF protection, login lockout, admin and viewer roles, an audit log of every change.

Keep the dashboard off the internet: a monitoring VLAN with access from your admin machines or VPN is the intended setup. The full threat model is in [SECURITY.md](docs/SECURITY.md).

## Documentation

- [Running Vantage](docs/OPERATIONS.md): network access, certificates, upgrades, backups, alert timing
- [Security](docs/SECURITY.md): threat model, controls, recovery procedures
- [Architecture](docs/ARCHITECTURE.md): components and data flow
- [Integrations](docs/INTEGRATIONS.md): the plugin interface, and [per-integration setup guides](docs/integrations)
- [Changelog](CHANGELOG.md)

## Development

You need Python 3.12+ and Node 22.12+ (macOS, Linux or Windows).

```bash
python3.12 scripts/dev_setup.py   # Windows: py -3.12 scripts/dev_setup.py
python scripts/check.py           # every CI check locally (--fast skips the audits)
```

`dev_setup.py` creates `backend/.venv`, installs the backend and frontend dependencies, and installs the pre-commit hooks if `pre-commit` is on your PATH.

## Roadmap

- **v0.2:** more alert channels, a "what changed" timeline across devices, checks of the remaining integrations against real hardware
- **v0.3:** security visibility: firewall logs, IDS alerts, failed logins, new-device detection
- **Later:** anomaly detection, opt-in summaries from a local model, OIDC/SSO, multiple sites

## Contributing

Pull requests for new integrations and bug fixes are welcome; the core (architecture, security, dependencies) stays with the maintainer. Read [CONTRIBUTING.md](CONTRIBUTING.md) before you start. Bug reports and integration findings from real hardware are welcome as [issues](https://github.com/levi-xy/vantage/issues).

## License

MIT License. See [LICENSE](LICENSE) and, for bundled third-party components, [NOTICE](NOTICE). Copyright 2026 levi-xy.

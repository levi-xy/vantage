# Vantage

> Working name. A self-hosted, security-first dashboard for homelabs and small infrastructure.

**Status: pre-alpha, in design and early development.** Nothing here is ready to run yet.

Vantage puts your firewall, hypervisor, NAS, switches, tunnels and services on one live dashboard, designed around your real hardware instead of generic graphs. Every device is added from the UI. No config files to edit, and as many instances of each type as you need.

---

## Why another dashboard?

| | Status pages (Homepage, Homarr) | Grafana | Vantage |
|---|---|---|---|
| Add devices from the UI | Partly | No (config/provisioning) | **Yes** |
| History and alerting | No | Yes | **Yes** |
| Cards shaped like the hardware (switch faceplate, drive bays, topology) | No | With effort | **Built in** |
| Correlates events across devices | No | No | **Planned** |
| Read-only by design | Varies | Yes | **Yes** |

---

## Features (planned for v0.1)

- **Add anything from the UI:** pick a device type, fill a form, test the connection, save. Run as many instances of each type as you need.
- **Integrations as plugins:** each device type is a self-contained module with its form, collection logic and cards. The community can add more.
- **Live, hardware-shaped cards:** network topology map, switch port faceplate, NAS drive bays, VM lists, tunnel and site traffic, media streams.
- **Editable layout:** cards appear automatically for each new device; drag them into place.
- **History and alerts:** Prometheus underneath, alert rules managed in the UI, Telegram delivery (more channels later).
- **"What changed":** a timeline that links events across devices (later: AI-assisted summaries).

---

## Supported integrations (planned)

| Integration | Collects via | Examples |
|---|---|---|
| Linux host | node_exporter scrape | Any Linux server, many NAS devices |
| SMART add-on | smartctl_exporter scrape | Disk health and temperatures |
| Proxmox VE | API (read-only token) | Hosts, VMs, LXCs, storage |
| pfSense | Telegraf scrape | Interfaces, gateways, firewall states |
| SNMP device | SNMP poll | Switches, UPSes, printers |
| MikroTik SwOS | SNMP profile | CRS/CSS switches on SwOS |
| cloudflared | Metrics scrape | Tunnel connections, requests, errors |
| Cloudflare account | API (read-only token) | Site traffic, cache, threats, tunnel health |
| Emby | API | Now playing, transcoding, libraries |
| HTTP / ping check | Backend probe | Anything with an IP or URL |

Don't see your device? The generic integrations (Linux host, SNMP, HTTP/ping) cover most gear, and new types can be added as plugins.

---

## Architecture

```
            Browser
               │  HTTPS, login
        ┌──────▼───────┐
        │  Frontend    │  React + Tailwind + ECharts
        └──────┬───────┘
               │  REST + WebSocket (the only thing the browser talks to)
        ┌──────▼──────────────────────────────────────────┐
        │  Backend (FastAPI)                              │
        │   • Integration registry (plugins)              │
        │   • Encrypted credential store (SQLite/Postgres)│
        │   • Pollers for API/SNMP sources → /metrics     │
        │   • Target discovery for Prometheus (/sd)       │
        │   • Query proxy, alerts, audit log              │
        └──────┬───────────────────────────┬──────────────┘
               │ http_sd + /metrics        │ API / SNMP polls
        ┌──────▼───────┐                   ▼
        │  Prometheus  │──scrape──►  exporters on devices
        └──────────────┘
```

**How "add from the UI" works:** when you save a device, the backend stores it, and Prometheus picks it up through `http_sd_configs` within a minute. API-based sources (Proxmox, Cloudflare, Emby, SNMP) are polled by the backend and exposed as metrics, so their history lands in Prometheus too. `prometheus.yml` is a fixed bootstrap file you never edit.

---

## Security model

Vantage holds a map of your network and credentials to your devices, so it's built to limit damage if it's ever compromised.

- **Read-only by design:** integrations use read-only tokens and roles wherever the device supports them. Vantage never changes device configuration, including firewall rules.
- **Secrets encrypted at rest** with a master key stored outside the database. Secret fields are write-only: they can be set or replaced but are never sent back to the browser.
- **The browser never talks to devices or Prometheus directly**, only to the authenticated backend.
- **SSRF protection:** new devices are restricted to private address ranges by default. Loopback, link-local and cloud metadata addresses are always blocked. Internet access is limited to the fixed API hosts of cloud integrations.
- **Auth:** strong password hashing (Argon2), secure session cookies, CSRF protection, login rate limiting, admin and viewer roles.
- **Audit log** of every change: who added, edited or removed what, without secret values.
- **Firewall hints:** when a connection test fails, Vantage shows the exact rule your firewall needs. You apply it yourself.

**Recommended deployment:** a dedicated monitoring VLAN, outbound internet blocked except the cloud API hosts you use, and dashboard access only from admin machines or your VPN. Never publish the dashboard to the internet.

---

## Quick start (planned)

```bash
git clone https://github.com/<owner>/vantage.git
cd vantage/deploy
cp .env.example .env        # then generate a master key (see docs)
docker compose up -d
```

Open `https://<host>`, create the admin account in the setup wizard, then add your first device under **Integrations → Add**.

## Development

Works the same on macOS, Linux and Windows. You need Python 3.12+ and Node 22.12+.

```bash
python3.12 scripts/dev_setup.py   # Windows: py -3.12 scripts/dev_setup.py
python scripts/check.py           # runs every CI check locally (--fast skips the audits)
```

`dev_setup.py` creates `backend/.venv`, installs the backend and frontend dependencies, and installs the pre-commit hooks if `pre-commit` is on your PATH.

The repo uses LF line endings on every OS (`.gitattributes`). A Windows checkout made before that file existed still has CRLF files; with no local changes, refresh it once with `git rm -rq --cached . && git reset -q --hard`.

Coding agents: `/task <ID>` runs one `BUILD-PLAN.md` task end to end (`builder` agent opens the PR, a fresh `reviewer` agent reviews it). See `.claude/`.

---

## Writing an integration

Each integration is a Python module with:

- a **config schema** (the form fields, with secrets marked)
- a **connection test**
- **scrape targets** and/or a **poll** function
- the **cards** it provides
- the **firewall hints** it needs

See `docs/INTEGRATIONS.md` (coming soon) for the full interface and a template.

---

## Roadmap

- **v0.1:** core platform, UI-managed devices, built-in integrations, editable dashboard, alerts to Telegram
- **v0.2:** network topology map, more alert channels, backup and restore
- **v0.3:** security visibility: firewall logs, IDS alerts, failed logins, new-device detection
- **Later:** anomaly detection and AI-assisted "what changed" summaries, OIDC/SSO, multiple sites

---

## Contributing

Not open to contributions yet. Guidelines will follow the first release.

## License

Apache 2.0 (planned).

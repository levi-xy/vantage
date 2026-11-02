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
- **Network map:** a dashboard card draws your devices with live status and the links you add in edit mode (device, optional port, label); a link shows its throughput when both ends name a port with traffic counters.
- **Editable layout:** cards appear automatically for each new device; drag them into place.
- **History and alerts:** Prometheus underneath, alert rules managed in the UI, Telegram delivery (more channels later). Telegram channels are set up on the **Alerts** page, with a "Send test message" button; the bot token is stored encrypted and never shown again.
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

## Quick start

Needs Docker Engine with Compose v2, and `openssl`.

```bash
git clone https://github.com/<owner>/vantage.git
cd vantage/deploy
./setup.sh
```

`setup.sh` creates the master key, the Prometheus token, a self-signed HTTPS certificate and `.env` (keeping any that already exist), starts the stack and prints the address. Back up `deploy/secrets/master.key` somewhere off the server: without it, stored device credentials can't be decrypted. The dashboard listens on `https://127.0.0.1:8443` by default; set `VANTAGE_BIND_ADDRESS` in `.env` to reach it from other machines (add any other names you use in `VANTAGE_TLS_NAMES` before the first run), then run `docker compose up -d`.

### HTTPS certificate

The bundled nginx serves the dashboard over HTTPS (TLS 1.2+) from `deploy/certs/tls.crt` and `deploy/certs/tls.key`; plain HTTP to the same port is redirected. Browsers warn about the generated self-signed certificate until you accept it. To use your own certificate (from your internal CA or a public one):

1. Put the PEM certificate, full chain, in `deploy/certs/tls.crt` and its unencrypted PEM key in `deploy/certs/tls.key`. `setup.sh` never replaces existing files.
2. Run `./setup.sh` again (it fixes the key's ownership and mode: readable only by the frontend container), then `docker compose restart frontend`.

To regenerate the self-signed pair (for example after changing `VANTAGE_TLS_NAMES`), delete both files and run `./setup.sh`. Once browsers trust your certificate you can turn on HSTS with `VANTAGE_HSTS=true`. Plain HTTP is an explicit opt-out: `VANTAGE_HTTPS=false` plus `VANTAGE_COOKIE_SECURE=false` for logins from other machines; anyone on the network path can then read passwords and session cookies.

Open the address, create the admin account in the setup wizard, then add your first device.

Metric history: Prometheus keeps 30 days of history or 10 GB on disk, whichever limit is reached first. When the size cap is hit, Prometheus drops the oldest data first, so with many devices you may get less than 30 days. Change the limits with `VANTAGE_PROMETHEUS_RETENTION` (default `30d`) and `VANTAGE_PROMETHEUS_RETENTION_SIZE` (default `10GB`) in `.env`, then `docker compose up -d`.

Device down detection: a polled device is marked down after 3 failed poll cycles in a row (one quick retry per cycle), so a single timeout doesn't flip it. At the default 60 s interval that takes about 2–4 minutes, at 5-minute intervals 10–16 minutes. Change it with `VANTAGE_DOWN_AFTER_FAILED_CYCLES` (1 to 10) in `.env`; the timing is in [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md#poll-cycles-the-retry-and-down).

**Upgrade note (poll cycles):** the "instance down" alert templates for polled integrations now use `for` 0 s, because the collector already waits N cycles. Rules you created from the old templates keep `for` 180 s and are not changed automatically: edit them on the **Alerts** page and set `for` to 0, or they wait 3 more minutes on top.

How alerts resolve: an alert resolves only after two clean checks on two different polls, where the device's value was seen and the condition was false. A missing value (device down, metric gone, query failing, removed from service discovery) never counts as recovery: the alert moves to **No data** and stays open until the value returns or an admin closes it. A "NO DATA" message goes out once an alert has been in No data for 5 minutes, and one reminder after 24 hours. Rules whose expression can't be split into a value and a threshold (for example `a and b`, or `absent(...)`) resolve when their result is empty and the device has polled again; the rule list warns about them.

**Upgrade note (alert states, migration 0008):** events gain a `no_data` state, `no_data_reason`, `state_since` and `reminder_sent_at`; the migration is additive and runs on start. Older Vantage versions don't know `no_data`: to roll back, run `docker compose exec backend alembic downgrade 0007` before starting the older image, which turns every `no_data` event back into `firing`.

Logs: the backend logs at INFO by default (for example each delivered alert message). Change it with `VANTAGE_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING` or `ERROR`) in `.env`.

Backups: `docker compose exec -T backend python -m app.cli backup - > vantage-backup.json.gz` (restore with `app.cli restore`). The master key is not in the backup; keep its own copy. `app.cli rotate-master-key` re-encrypts every stored secret with a new key. See [docs/SECURITY.md](docs/SECURITY.md#backup-restore-and-master-key-rotation).

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

See [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md) for the full interface, the rules and a checklist.

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

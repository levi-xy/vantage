# Vantage

> Working name. A self-hosted, security-first dashboard for homelabs and small infrastructure.

**Status: v0.1, first release.** It runs, and its alerting, delivery and recovery paths have been drilled on a test stack and lab devices. Most integrations are tested only against recorded or hand-written answers so far; the table below says which ones have met real equipment. Expect rough edges and report them.

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

## Features

- **Add anything from the UI:** pick a device type, fill a form, test the connection, save. Run as many instances of each type as you need.
- **Integrations as plugins:** each device type is a self-contained module with its form, collection logic and cards. The community can add more.
- **Live, hardware-shaped cards:** switch port faceplate, NAS drive bays, VM lists, tunnel and site traffic, media streams.
- **Network map:** a Map page, where you draw your network (devices, ports, cables, VLANs) with live status, is coming; the old map card is gone.
- **Dashboards you arrange:** a new device's cards land on the main dashboard; drag, resize (cards show less when small), add more dashboards as tabs.
- **History and alerts:** Prometheus underneath, alert rules managed in the UI, Telegram delivery (more channels later). Telegram channels are set up on the **Alerts** page, with a "Send test message" button; the bot token is stored encrypted and never shown again.
- **Self-monitoring:** Vantage alerts when Prometheus is unreachable, isn't collecting, or alert evaluation stops, and says why; `/healthz` for an external watchdog.
- **Security first:** read-only toward devices, secrets encrypted and never returned, every outbound call through an SSRF guard, HTTPS by default. See [docs/SECURITY.md](docs/SECURITY.md).

---

## Supported integrations

| Integration | Collects via | Examples | Tested against |
|---|---|---|---|
| Linux host | node_exporter scrape | Any Linux server, many NAS devices | Fixtures |
| SMART add-on | smartctl_exporter scrape | Disk health and temperatures | Fixtures |
| Proxmox VE | API (read-only token) | Hosts, VMs, LXCs, storage, guest tags | Fixtures |
| pfSense | Telegraf scrape | Interfaces, gateways, firewall states | Lab pfSense |
| SNMP device | SNMP poll (v1, v2c, v3) | Switches, routers, UPSes, printers | Lab VyOS (v2c) |
| MikroTik SwOS | SNMP profile | CRS/CSS switches on SwOS | A real switch, observed |
| cloudflared | Metrics scrape | Tunnel connections, requests, errors | Fixtures |
| Cloudflare account | API (read-only token) | Site traffic, cache, threats, tunnel health | Fixtures |
| Emby | API | Now playing (who is watching what), transcoding, libraries | Fixtures |
| HTTP / ping check | Backend probe | Anything with an IP or URL | Lab (ping); fixtures (HTTP) |

"Fixtures" means recorded or hand-written device answers in the test suite: the parsing is tested, but the integration hasn't met that product's real API yet. Field names that differ on real gear show up as missing values, not errors; please report them.

Don't see your device? The generic integrations (Linux host, SNMP, HTTP/ping) cover most gear, and new types can be added as plugins.

---

## Architecture

```
            Browser
               │  HTTPS, login
        ┌──────▼───────┐
        │  Frontend    │  React + Tailwind
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

Needs Docker Engine with Compose v2 (2.17 or newer), and `openssl`. Images are published for amd64 and arm64.

**From a release** (no source checkout; Docker pulls the published images). Take the bundle from the [latest release](https://github.com/levi-xy/vantage/releases/latest), replacing `0.1.0` with its version:

```bash
V=0.1.0
curl -fLO https://github.com/levi-xy/vantage/releases/download/v$V/vantage-deploy-$V.tar.gz
curl -fLO https://github.com/levi-xy/vantage/releases/download/v$V/SHA256SUMS
sha256sum -c SHA256SUMS
tar -xzf vantage-deploy-$V.tar.gz
cd vantage-deploy-$V
./setup.sh
```

The images are signed. To check one before running it (needs [cosign](https://docs.sigstore.dev/cosign/system_config/installation/)):

```bash
cosign verify ghcr.io/levi-xy/vantage-backend:$V \
  --certificate-identity-regexp '^https://github.com/levi-xy/vantage/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

**From source** (builds the images on your machine):

```bash
git clone https://github.com/levi-xy/vantage.git
cd vantage/deploy
./setup.sh
```

Either way, `setup.sh` creates the master key, the Prometheus token, a self-signed HTTPS certificate and `.env` (keeping any that already exist), starts the stack and prints the address. Back up `secrets/master.key` (in the folder you ran `setup.sh` from) somewhere off the server: without it, stored device credentials can't be decrypted. The dashboard listens on `https://127.0.0.1:8443` by default; set `VANTAGE_BIND_ADDRESS` in `.env` to reach it from other machines (add any other names you use in `VANTAGE_TLS_NAMES` before the first run), then run `docker compose up -d`.

**Vantage can't tell you when Vantage itself is down.** It sends its own alerts when Prometheus is unreachable, isn't collecting metrics, or alert evaluation stops, but if the backend or its host is down, or the notification dispatcher itself is stuck, no message is sent. Point an external monitor (Uptime Kuma, cron + curl, a hosted dead-man's switch) on another machine at `https://<vantage>/healthz` and alert on anything but 200. See [Self-monitoring](docs/SECURITY.md#self-monitoring-and-healthz).

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

### Upgrading

Back up first (see Backups below), then:

- **From a release:** download the new bundle and unpack it next to the old one, then copy over your `secrets/`, `certs/` and `.env` from the old folder (never the other files: the new compose file belongs to the new release). In the new folder run `docker compose down`, then `./setup.sh`. Or, if the release notes say the compose file didn't change, set `VANTAGE_VERSION=<new version>` in `.env` and run `docker compose pull && docker compose up -d`.
- **From source:** `git pull`, then `docker compose up -d --build` in `deploy/`.

Database migrations run by themselves when the backend starts. When a release changes the compose network (the release notes say so), run `docker compose down` before `up -d`: a plain `up -d` leaves the frontend without its fixed address and the dashboard down. The [CHANGELOG](CHANGELOG.md) lists each version's upgrade notes.

Logs: the backend logs at INFO by default (for example each delivered alert message). Change it with `VANTAGE_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING` or `ERROR`) in `.env`.

Backups: `docker compose exec -T backend python -m app.cli backup - > vantage-backup.json.gz`, while Vantage runs. The master key is not in the backup; keep its own copy. Restoring (`app.cli restore`) and `app.cli rotate-master-key` (re-encrypts every stored secret with a new key) are offline: stop the backend first (`docker compose stop backend`) and run them with `docker compose run --rm -T backend python -m app.cli ...`; they refuse while the backend runs, and the backend doesn't start while one of them runs. See [docs/SECURITY.md](docs/SECURITY.md#backup-restore-and-master-key-rotation).

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

- **v0.1:** core platform, UI-managed devices, built-in integrations, multiple dashboards, network map, alerts to Telegram with self-monitoring, backup, restore and key rotation
- **v0.2:** more alert channels, a "what changed" timeline across devices, hardware checks of the fixture-only integrations
- **v0.3:** security visibility: firewall logs, IDS alerts, failed logins, new-device detection
- **Later:** anomaly detection and AI-assisted "what changed" summaries, OIDC/SSO, multiple sites

---

## Contributing

Not open to contributions yet. Guidelines will follow the first release.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 levi-xy.

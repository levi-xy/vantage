# Changelog

All notable changes, newest first. Versions follow [semantic versioning](https://semver.org/); before 1.0, a minor version (0.2) may change behaviour, and its notes say how to upgrade.

## 0.1.0 (unreleased)

The first release.

**Platform**
- Devices are added from the UI: pick a type, fill the generated form, test the connection (with a firewall hint when it times out), save. Secrets are encrypted at rest (AES-GCM, bound to their row) and never returned.
- FastAPI backend, Prometheus underneath (HTTP service discovery, a fixed bootstrap config), React frontend served by nginx over HTTPS. One `setup.sh` creates the keys, the certificate and `.env`, and starts the stack.
- Users with admin and viewer roles, persistent login lockout, sessions stored hashed, an audit log (with usernames) for every change.
- Every outbound connection goes through the SSRF guard (private ranges by default; loopback, link-local, metadata and the host's own addresses always refused; DNS resolved once).

**Integrations:** Linux host (node_exporter), SMART (smartctl_exporter), Proxmox VE, pfSense (Telegraf), SNMP device (v1, v2c, v3), MikroTik SwOS, cloudflared, Cloudflare account, Emby, HTTP check, ping check. The README's table says which have been tested against real equipment.

**Dashboard**
- Several dashboards as tabs; cards dragged, resized and shown in less detail when small; a phone layout. A new device's cards are put on the main dashboard.
- Hardware-shaped cards: switch front panel, pfSense interfaces (VLANs grouped) and gateways, Proxmox nodes and guests (with tags), drive health, Emby (with a live "Now playing" list, never stored).
- Each device has its own colour on its cards; a network map card; WAN throughput with live rates.
- Header health (Healthy / Warning / Critical) with a list of what's wrong.

**Alerts**
- Rules from templates or PromQL, one alert per series, Telegram channels.
- An alert resolves only after two clean checks on two different polls; a missing value moves it to No data instead of resolving it.
- Delivery through an outbox with retries for 24 hours (at least once, never lost); a banner when messages are stuck.
- Vantage's own alerts: Prometheus unreachable (saying why and how to fix it), metrics not collected, evaluator stalled, master key mismatch. `/healthz` for an external watchdog.

**Operations:** backup, offline restore and master-key rotation (`app.cli`), guarded by a maintenance lock; signed multi-arch images on GitHub Container Registry with an SBOM; an install bundle per release.

**License:** Apache 2.0.

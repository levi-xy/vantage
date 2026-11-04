# Architecture

How Vantage is put together, for someone about to change it. The binding rules (interfaces, data model, API shapes) are the contracts in [AGENTS.md](../AGENTS.md); the threat model and the security controls are in [SECURITY.md](SECURITY.md); the integration interface is in [INTEGRATIONS.md](INTEGRATIONS.md). This page explains how the parts fit.

## The four containers

`deploy/docker-compose.yml` runs:

| Container | What it does | Talks to |
|---|---|---|
| `frontend` | nginx: serves the built React app over HTTPS and proxies `/api` (including the `/api/v1/ws` WebSocket) and `/healthz` to the backend. Fixed address, so the backend trusts exactly it as the proxy naming the client. | backend |
| `backend` | FastAPI app plus its background tasks (below). Holds the database (SQLite in a volume by default, or Postgres) and the master key (a Docker secret). | devices (through the SSRF guard), Prometheus, Telegram |
| `prometheus` | Stores every metric. Its config is a fixed file: one job discovers targets from the backend (`/sd/targets`), one scrapes the backend's own `/metrics`. No published port. | backend, scrape targets |
| (devices) | Exporters Prometheus scrapes (node_exporter, Telegraf on pfSense, cloudflared), or APIs and SNMP agents the backend polls. | |

The browser only ever talks to the frontend container, and through it to the backend. It never reaches Prometheus or a device.

## Inside the backend

```
backend/app/
  main.py          app factory: wires everything below and starts the background tasks
  core/            config, database, crypto (AES-GCM secret store), auth, net_guard (SSRF),
                   audit, key_check (master-key canary), maintenance (offline-command lock)
  models/          SQLAlchemy models (Contract 2)
  api/             FastAPI routers (Contract 3)
  integrations/    base.py (Contract 1), registry.py, builtin/<one package per type>
  collectors/      the poll scheduler, the in-memory instance status, the /metrics registry
  sd/              /sd/targets for Prometheus' HTTP service discovery
  query/           the PromQL proxy (/query) with its budget and concurrency caps
  alerts/          evaluator, state machine, PromQL splitter, outbox and dispatcher,
                   self-monitoring (system alerts, watchdog), key monitor
  live/            live WAN rates for the main dashboard (pushed over /ws, never stored)
  heartbeat.py     the backend heartbeat and the maintenance-lock check
  cli/             backup, restore, rotate-master-key, user commands, startup-gate
```

Background tasks, all in the one backend process:

- **Poll collector:** for each enabled, polled instance, calls the integration's `poll()` every `poll_interval_seconds()`. A cycle is one attempt plus at most one quick retry; after N failed cycles in a row (default 3) the instance is `down`. The samples become Prometheus metrics on `/metrics`, with the four standard labels added.
- **Alert evaluator:** every tick, a `vector(1)` probe, then each rule's query through the same proxy code, then the per-series state machine (pending, firing, no data, recovering, resolved). Changes are written together with their notification rows (the outbox) in one transaction.
- **Dispatcher:** sends outbox rows to Telegram, oldest first per channel, with retries for 24 hours.
- **Self-monitor and watchdog:** turn a failing probe, a missing self-scrape or a stalled evaluator into system alerts, and restart a stalled evaluator.
- **Key monitor and heartbeat:** re-check the master key every 15 s; write the heartbeat and look for the maintenance lock every 10 s, pausing work while an offline command runs.
- **Live rates:** while a browser is connected, read the main dashboard's throughput ports every ~2 s and push the numbers over `/ws`.

## How data flows

**Adding a device.** The UI renders a form from the integration's JSON schema. `POST /instances` validates the config, checks every host against the SSRF guard, splits secret fields (encrypted into `instances.secrets`) from the rest (`instances.config`), and audits the change. A polled integration is picked up by the collector on its next pass; a scraped one appears in `/sd/targets`, which Prometheus reads within a minute.

**Collecting.** Two paths end in Prometheus:

- *Scraped:* Prometheus connects to the device's exporter itself. `/sd/targets` lists only targets the instance's guard allows (Prometheus resolves names again when scraping, so IP literals are the fully protected form).
- *Polled:* the backend calls the device's API or SNMP agent through guarded clients from `InstanceContext` (it never opens its own sockets), and exposes the results on `/metrics`, which Prometheus scrapes.

**Showing.** Cards ask the backend, never Prometheus: `POST /query` runs PromQL within a time, size and concurrency budget. Instance status comes from the collector's in-memory state on `GET /instances`; changes are pushed over `/ws`. A few things are deliberately not metrics and live only in memory: live WAN rates and Emby's "now playing" list.

**Alerting.** A rule is PromQL; each series in its result is one alert, identified by a fingerprint of its identity labels. A series resolves only after two clean checks on two different polls (the value was seen, from a fresh source, and the condition was false); a missing value moves it to No data instead. Notifications go through the outbox, so an alert change and its message are committed together: at least once, never lost.

## Where state lives

| State | Where | Survives a restart |
|---|---|---|
| Devices, users, dashboards, rules, alert history, outbox, audit log | Database | Yes (and in backups) |
| Device secrets, channel tokens | Database, encrypted with the master key | Yes; unreadable without the key |
| Master key, Prometheus token, TLS key | Docker secrets from `deploy/secrets/`, `deploy/certs/` | Yes; not in backups (back up the key yourself) |
| Metrics history | Prometheus volume (30 days or 10 GB by default) | Yes |
| Instance status, alert timers, probe state, live rates, now playing | Backend memory | No: rebuilt after start |

## Trust boundaries

1. **Browser → frontend:** HTTPS, session cookie (httpOnly; the database stores only its hash), CSRF token on every change, roles enforced by the backend.
2. **Backend → devices and the internet:** only through `core/net_guard.py`. Private ranges by default, never loopback, link-local, metadata or the host's own addresses; public hosts only for an integration's declared cloud hosts, over verified HTTPS.
3. **Backend ↔ Prometheus:** a bearer token both ways (`/sd/targets`, `/metrics`); Prometheus has no published port.
4. **Secrets at rest:** AES-GCM with the master key, bound to their row; the key-check canary keeps a wrong key from writing new secrets.

Everything an integration does is read-only toward the device (hard rule 2); the tests of each integration check that only allowed reads are sent.

## Releases

A pushed `vX.Y.Z` tag runs `.github/workflows/release.yml`: multi-arch images to `ghcr.io/levi-xy/vantage-backend` and `-frontend` with SBOM and provenance, signed with cosign (keyless), then a draft GitHub release holding the install bundle that `deploy/make_bundle.py` builds (the `deploy/` files with a compose file that pulls those images) and its checksums.

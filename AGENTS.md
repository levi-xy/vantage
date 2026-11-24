# AGENTS.md

Read this file before doing any work in this repository. It defines the architecture, the contracts between components, and the rules every change must follow. If a task conflicts with this file, stop and ask the maintainer instead of guessing.
As an agent, don't add your signature to commits or PRs.

---

## Project in one paragraph

Vantage is a self-hosted, read-only, security-first infrastructure dashboard. Users add devices from the UI; each device type is an **integration plugin**. A FastAPI backend stores devices and encrypted credentials, polls API/SNMP sources, exposes their results as Prometheus metrics, and serves Prometheus its scrape targets through HTTP service discovery. A React frontend renders hardware-shaped cards and talks **only** to the backend.

---

## Hard rules (never break these)

1. **No secrets in the repo.** No real tokens, passwords, community strings, IPs, hostnames or domains from any real network, including in tests, fixtures, docs and examples. Use RFC 5737 / RFC 1918 example ranges (`192.0.2.0/24`, `10.0.0.0/8`) and obviously fake values (`example-token`).
2. **Read-only toward devices.** No integration may call an endpoint that changes device state. If a library exposes write methods, don't call them. Reviews must check this.
3. **Secrets never leave the backend.** API responses must never contain secret values, not even masked fragments. Secret fields are write-only.
4. **The browser talks only to the backend.** No direct frontend calls to Prometheus, devices or third-party APIs.
5. **All outbound connections go through the SSRF guard** (`core/net_guard.py`). No raw `httpx`/`socket`/SNMP calls that bypass it.
6. **No new dependency without justification** in the PR description (why, license, maintenance status). Prefer the standard library and the existing stack.
7. **Every change has tests.** CI must be green before review.
8. **Don't change a contract in this file** (interfaces, data model, API shapes) without the maintainer's approval in the task. If a contract is wrong, propose the change in the PR description.
9. **Never weaken security to make a test pass** (disabling CSRF, auth, TLS verification by default, the SSRF guard).

---

## Tech stack

| Area | Choice |
|---|---|
| Backend | Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic, httpx (async), pysnmp, prometheus_client, APScheduler or an asyncio task loop |
| Auth/crypto | argon2-cffi, `cryptography` (AES-GCM via Fernet or AESGCM), itsdangerous or signed session cookies |
| Database | SQLite by default; Postgres supported through SQLAlchemy |
| Frontend | React 18+, TypeScript (strict), Vite, Tailwind CSS, small charts as plain inline SVG (no chart library), react-grid-layout, TanStack Query, react-jsonschema-form (or equivalent) for dynamic forms |
| Metrics | Prometheus (bootstrap config only) |
| Quality | ruff, mypy (strict for `core/` and `integrations/base.py`), pytest + pytest-asyncio, ESLint, Prettier, Vitest, Playwright for a few end-to-end tests |
| Security tooling | gitleaks (pre-commit + CI), pip-audit, npm audit, bandit |
| Packaging | Docker images, `deploy/docker-compose.yml` |

---

## Repository layout

```
vantage/
├── AGENTS.md
├── README.md
├── BUILD-PLAN.md
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── core/          # config, db, crypto, auth, net_guard, audit
│   │   ├── models/        # SQLAlchemy models
│   │   ├── api/           # FastAPI routers
│   │   ├── integrations/
│   │   │   ├── base.py    # the Integration contract
│   │   │   ├── registry.py
│   │   │   └── builtin/   # one package per integration
│   │   ├── collectors/    # poll scheduler, metrics registry
│   │   ├── sd/            # Prometheus http_sd endpoint
│   │   ├── query/         # PromQL proxy
│   │   └── alerts/        # rules, evaluator, channels
│   ├── migrations/
│   └── tests/
├── frontend/
│   └── src/
│       ├── app/           # routing, auth, layout shell
│       ├── api/           # typed API client
│       ├── cards/         # card framework + one folder per card type
│       ├── pages/
│       └── components/
├── deploy/
│   ├── docker-compose.yml
│   ├── prometheus/prometheus.yml   # fixed bootstrap, never device-specific
│   └── .env.example
└── docs/
    ├── ARCHITECTURE.md
    ├── INTEGRATIONS.md
    └── SECURITY.md
```

---

## Contract 1: the Integration interface

`backend/app/integrations/base.py`. Every integration subclasses this.

```python
from abc import ABC
from typing import ClassVar, Literal
from pydantic import BaseModel, SecretStr

Category = Literal["firewall", "hypervisor", "storage", "switch",
                   "tunnel", "cloud", "media", "host", "generic"]

class TestResult(BaseModel):
    ok: bool
    message: str                     # human-readable, never contains secrets
    hint: "FirewallHint | None" = None

class FirewallHint(BaseModel):
    protocol: Literal["tcp", "udp", "icmp"]
    port: int | None                 # required for tcp/udp; None (and only None) for icmp
    destination: str                 # the instance host, as configured
    reason: str

class ScrapeTarget(BaseModel):
    address: str                     # host:port
    metrics_path: str = "/metrics"
    scheme: Literal["http", "https"] = "http"

class CardSpec(BaseModel):
    card_type: str                   # matches a frontend card folder name
    title: str
    default_size: tuple[int, int]    # grid width, height

class Integration(ABC):
    type_id: ClassVar[str]           # stable, e.g. "pfsense"
    display_name: ClassVar[str]
    category: ClassVar[Category]
    version: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]   # secrets typed as SecretStr
    cloud_hosts: ClassVar[tuple[str, ...]] = ()  # fixed public API hosts, if any

    async def test_connection(self, ctx: "InstanceContext") -> TestResult: ...
    def scrape_targets(self, ctx: "InstanceContext") -> list[ScrapeTarget]:
        return []
    async def poll(self, ctx: "InstanceContext") -> list["Sample"]:
        return []
    def poll_interval_seconds(self) -> int:
        return 60
    def cards(self) -> list[CardSpec]: ...
    def firewall_hints(self, ctx: "InstanceContext") -> list[FirewallHint]: ...
```

- `InstanceContext` gives the integration its decrypted config, the instance id/name/labels, and **guarded HTTP, SNMP and ICMP ping clients** (from `net_guard`). Integrations never construct their own clients or sockets.
- `InstanceContext.snmp_client(host, auth, *, port=161, timeout=5.0, retries=2)`: `auth` is `SnmpV1 | SnmpV2c | SnmpV3`. The keyword-only `timeout` (seconds per attempt, more than 0 and at most 5) and `retries` (0 to 2) can only lower the guarded client's own defaults (5 s, 2 retries); anything outside raises `ValueError`. Every `get()`/`walk()` still stops after the client's 15 s total. The client offers GET and WALK only.
- `InstanceContext.http_client(*, verify: bool | ssl.SSLContext = True, base_url="", headers=None)`. `True` verifies against the system CAs; an `ssl.SSLContext` verifies against a certificate the admin supplied (pinning, for self-signed devices); `False` turns verification off and is only ever an explicit, off-by-default per-instance opt-in. (Unchanged, existing behaviour of the guard: a cloud host is still refused unless verification of both the certificate and the hostname is on.)
- `Sample` = metric name, labels, value, type (gauge/counter). The collector adds the standard labels.
- Metric names: `vantage_<type_id>_<metric>` for polled metrics. Scraped exporter metrics keep their native names.
- Polled-metric labels are fixed by the collector: `vantage_instance_id`, `vantage_instance`, `vantage_integration`, `vantage_site` and `vantage_install` (this install's id, the `settings` row `install_id`). Integrations may add their own labels but never override these.
- Scrape targets get the same five labels through service discovery.
- An instance with no site gets `vantage_site=""`. Prometheus treats an empty label as absent, so such series carry four labels; that's intended. Never substitute a placeholder site name. The UI shows "No site".
- An integration is registered by being listed in `registry.py`. No dynamic imports from user input.

---

## Contract 2: data model

| Table | Key fields |
|---|---|
| `users` | id, username, password_hash (argon2), role (`admin` / `viewer`), created_at, last_login_at |
| `instances` | id (uuid), type_id, name (unique), site, tags (json), enabled, config (json, non-secret fields only), secrets (encrypted blob), color (nullable: one of a fixed palette of named colours, `blue` / `violet` / `teal` / `pink` / `orange` / `lime` / `cyan` / `indigo` / `fuchsia` / `sky` / `purple`), created_at, updated_at |
| `network_maps` | id, name, position (int, tab order, dense from 0), document (json: the drawing, see below), revision (int, bumped by every save), updated_at |
| `dashboards` | id, name, layout (json: card positions and per-card settings), position (int, tab order, dense from 0) |
| `alert_rules` | id, name, expr (PromQL), for_seconds, severity, instance_id (nullable), channel_ids, enabled, deleted_at (nullable; soft delete), revision |
| `alert_events` | id, rule_id (nullable, FK `alert_rules` ON DELETE SET NULL), fingerprint, labels (json), instance_id (nullable, FK `instances` ON DELETE SET NULL), rule_name, severity, state (`firing` / `no_data` / `resolved`), started_at, ended_at, end_reason (nullable: `recovered` / `rule_disabled` / `rule_deleted` / `rule_changed` / `closed_by_user`), summary, no_data_reason (nullable: `source_stale` / `source_down` / `metric_missing` / `query_failed`, and `prometheus_unreachable` / `metrics_not_collected`; no CHECK, later reasons may be added), state_since, reminder_sent_at, source (`rule` / `system`, default `rule`) and system_kind (nullable: `prometheus_unreachable` / `metrics_not_collected` / `evaluator_stalled`), and `key_mismatch` |
| `channels` | id, type (`telegram`), name, config (json), secrets (encrypted blob), system_alerts (boolean, default true) |
| `alert_notifications` | id, event_id (nullable, FK `alert_events`), channel_id (nullable, FK `channels` ON DELETE SET NULL), kind (`firing` / `resolved` / `rule_change` / `summary` / `no_data`; `no_data` = the NO DATA message and its 24 h reminder), text, status (`pending` / `sending` / `delivered` / `failed` / `folded` / `cancelled`), attempts, next_attempt_at, claimed_at, claim_token, folded_into (nullable, FK self), error_kind, created_at, finished_at |
| `audit_log` | id, user_id, actor_type (`user` / `cli` / `system`), action (`create` / `update` / `delete` / `login` / `logout` / `login_failed`), target_type, target_id, at, changes (json, **secret fields replaced with `"<changed>"`**) |
| `settings` | key, value (json); the row `key_check` holds the master-key check value; the rows `maintenance_lock` and `backend_heartbeat` are runtime state, never backed up or restored; the row `timezone` = an IANA zone name (absent = `UTC`), backed up and restored like other settings; the row `setup_code` = `{sha256, created_at}` of the one-time setup code, runtime state, never backed up or restored; the row `install_id` = `{"id": "<uuid4>"}`, this install's id, made once (migration 0015, or at startup when missing), never changed, backed up and restored (a restore takes the backup's id when it has one, else keeps the current one); the backend refuses to start when it can't be loaded; the row `prometheus_auth` = `{kind: none / basic / bearer, username, secrets}`, the optional auth from the backend to Prometheus: `secrets` is the password or token encrypted with the master key under `row_context("settings", "prometheus_auth")`; backed up and restored like other settings, and counted as a stored secret by the key check, restore and `rotate-master-key` |
| `login_failures` | id, user_id (FK `users`, cascade delete), at |
| `sessions` | id (SHA-256 of the session token), user_id (FK `users`, cascade delete), created_at, expires_at, last_seen_at |

- `instances.config` and `instances.secrets` are split by the integration's schema: `SecretStr` fields go to `secrets`, everything else to `config`.
- `instances.color` colour-codes a device: every card of the device shows it, so its cards are found at a glance. The palette has no red, amber or green (problems and healthy). A new instance without a colour gets the first palette colour no instance uses yet (once all are used, the least used, in palette order); the migration coloured existing instances the same way in `created_at` order.
- Network maps: a map is a drawing the admins make themselves; nothing is laid out automatically. `document` = `{version, vlans, elements}`: `vlans` names the map's VLANs (id, name, palette colour); `elements` (at most 500, the whole document at most 256 KB) are boxes (optionally linked to an instance, with an optional port strip: the device's own interfaces or typed by hand, each port optionally carrying its untagged/tagged VLANs), cables (between boxes or ports; speed, line style, optional palette colour, label, whether to show the live rate, bends), frames (optionally linked to a Proxmox instance: its guests are filled in automatically, and the admin can hide, reorder and add tiles), text panels, tables and a legend. All text is plain (never rendered as HTML, no links fetched); colours are names from the instance palette. An `instance_id` that no longer exists is kept and shown as a removed device. The backend validates the shape, sizes and references between elements; it does not read live data for maps (the browser uses `/query` and the instance endpoints, like cards).
- Detailed network maps (`version` stays 1, every new field is optional with a default, the caps stay): nothing in a map is tied to a vendor or to the maintainer's network; shapes, panel looks, port groups, tiles, icons and groups are per-map data the admin edits, and a built-in "model" is only a preset copied into the map. `vlans` gain `subnet` (≤ 43) and `note` (≤ 64). A box gains `shape` (`card` / `cloud` / `device` / `nas` / `ap`), `role`, `model`, `address`, `version` (identity lines; empty = filled from the linked device where known), `sections` (≤ 4 of tiles / services / list, each ≤ 12 items; an item may carry a live `figure` key), `nas` (`bays` 1–16, `bay_disks`) and `panel`, which replaces `ports`: `{look, place, size, numbers, captions, source, groups (≤ 8), ports (≤ 128)}`, a group being `{id, kind (rj45 / sfp / sfp_plus / sfp28 / qsfp / console / usb), count, rows, order (`rows` = top row first / `odd_top` / `odd_bottom`: two-row columns numbered in pairs, odd on top or bottom), first, prefix}` and a port `{id, group, n, caption, interface, vlans, hidden}`. A migration (and tolerant parsing of the old `ports` key) turns an old strip into a panel: ports of one kind become one group (kinds in first-seen order), every port keeps its `id`, so cables still point at it. `icon` is a generic name or `di:<slug>` (`[a-z0-9-]{1,64}`, only the pattern is checked; the frontend falls back to the generic icon when the file is missing). Frames gain `groups` (guest groups), `bars`, `side`, `model` / `address` / `version`; a new element `networks` lists the VLANs; tables gain `badges` (column indexes). **New dependency:** the dashboard-icons library (homarr-labs/dashboard-icons, Apache-2.0), pinned to one commit and checksummed by `frontend/icons.manifest.json`, fetched at frontend image build by `frontend/scripts/fetch-icons.mjs` and served by Vantage's own web server as images (`/icons/di/<slug>.svg`); the browser never contacts a CDN and the SVGs are never inlined. Cables stay right-angle only; map saves stay unaudited; no new backend polling (live values come from the existing combined `/query` calls).
- Setup code (`app/core/setup_code.py`): `python -m app.cli setup-code` (`--quiet` prints only the code), run by `deploy/setup.sh` once the stack is up, makes a fresh code (16 characters from an alphabet without 0/O/1/I/L, about 79 bits, shown `ABCD-EFGH-JKMN-PQRS`) while setup is open (no user, never closed), stores only its SHA-256 in the `settings` row `setup_code` (replacing any earlier one), prints it, and audits a `create` on `setup_code` (`actor_type` `cli`, no value). Once setup is done it stores nothing. `POST /setup` normalises the typed code (trimmed, upper case, dashes and spaces ignored), compares hashes in constant time and deletes the row in the transaction that creates the admin; creating a user any other way also deletes it.
- `login_failures` holds one row per failed login for an **existing** user: who and when, nothing else (no submitted username, password or client address). The per-user limit counts rows in a sliding 15-minute window, so lockouts survive a restart and can't be evicted. Usernames that match no user, and the per-client limit, stay in the in-memory limiter. A successful login, `app.cli unlock <username>` and `app.cli reset-password` clear the user's rows.
- A login refused (429) because the account is locked writes an `audit_log` entry: `action=login_failed`, the user as target, `changes` = `{"username": <stored username>, "locked": true}` (never the submitted username or password). At most one per account per minute, best effort; refusals by the per-client limit or for unknown usernames aren't audited.
- `audit_log.actor_type` says who acted, and every audit write sets it: `user` = a request through the API (`user_id` is the logged-in user), `cli` = the admin CLI on the server, `system` = the backend itself. `cli` and `system` entries never have a `user_id`.
- `sessions` holds one row per login session. The cookie carries a random token; the database stores only its SHA-256 hash, so a copy of the database can't be used to log in. Deleting a row ends that session: logout, a new login on the same browser, password reset, expiry or idle timeout, and user deletion (cascade).
- Alerts: an alert is one series of a rule's result, identified by (rule_id, fingerprint); `app/alerts/labels.py` defines the fingerprint (SHA-256 over the series' identity labels: every label except `__*`, `job`, `instance`, `vantage_instance`, `vantage_site`, `vantage_integration`, `vantage_install`, the integrations' display labels and sensitive-looking names). A partial unique index allows one open event (`firing` or `no_data`) per (rule_id, fingerprint). `rule_name`, `severity`, `labels` and `instance_id` are snapshots taken when the event opens; `labels` holds only filtered labels (sensitive values `"<redacted>"`, URLs without credentials or query, at most 16). Events from before this change have `fingerprint = "legacy"`. No rule operation deletes an event row: deleting a rule sets `deleted_at` and closes its open events. Every rule change bumps `revision`.
- Alert states: an open event resolves (`recovered`) only after two clean checks on two different polls: the rule's query succeeded, the series is absent from its result, the rule's value query (the *witness*, split from the expression by `app/alerts/promql.py`) returned the series with a value failing the condition, and the series' source (the device's last successful poll, or its last `up == 1` scrape) is fresh (younger than 2 × poll interval + 30 s, or 90 s when scraped) and newer than the one the previous clean check used; a series without `vantage_instance_id` has no source check. Rules the splitter can't split (`recovery_mode = empty_result`) resolve on an empty result with a fresh, advanced source. A missing value never resolves an alert: a stale source (`source_stale`, or `source_down` when the instance is down), a series missing from a newer poll (`metric_missing`) or the rule's query failing 3 ticks in a row (`query_failed`) moves it to `no_data`, which stays open until the value returns or an admin closes it. A failed `vector(1)` probe changes nothing (for less than 2 minutes; then see system alerts below). Messages: NO DATA after 5 minutes in `no_data` and one reminder after 24 h (notification kind `no_data`), "data back, still firing" (kind `firing`) only if the NO DATA message was sent; all through the outbox in the same transaction. RECOVERING counts and pending timers live in memory and start over on a restart and on a rule revision change.
- Notification delivery: `alert_notifications` is the outbox. One row per (alert change, channel) is inserted in the same transaction as the change (FIRING, RESOLVED, a rule edit's closure message); its text is rendered then, holds no secret and is capped at 4,096 characters. One dispatcher in the backend claims rows (`pending` → `sending`, conditional update; `FOR UPDATE SKIP LOCKED` on Postgres), only the oldest unfinished row per channel, at most 4 sends at once and 1 per channel. Several ready rows for a channel go as one message (20 lines, then "and N more, see Alerts"); the carried rows become `folded` into the sent one. Retries after 10 s, 30 s, 1 min, 2 min, 5 min, then every 10 min until the row is 24 h old (`failed`, `expired`); a Telegram 429 waits `retry_after` (at most 10 min); Telegram 400/401/403/404 and unreadable channel credentials fail at once. `error_kind` is a fixed code, never a response text. A channel with 500 unfinished rows stores new rows as `folded` into one pending `summary` row that counts them; such a row keeps `finished_at` empty ("queued in summary") until the summary finishes, then takes its outcome (delivered, `failed` with the summary's `error_kind`, or `cancelled`). Deleting a channel cancels its unfinished rows. At startup every `sending` row goes back to `pending` (attempt kept): delivery is at least once, so a duplicate is possible, a loss is not. No row is deleted in v0.1.
- System alerts and shared failures (`app/alerts/selfcheck.py`): Vantage's own alerts are `alert_events` rows with `source = system`, no rule (`rule_id` NULL) and a fixed `system_kind`: `prometheus_unreachable` (the evaluator's `vector(1)` probe failing for 2 minutes in a row: connection refused, timeout, HTTP 5xx, Prometheus refusing the configured credentials (401/403) or stored credentials that can't be decrypted; never a rule's own bad PromQL), `metrics_not_collected` (the newest `vantage_build_info` sample of this install older than 2 minutes, or none for 2 minutes, judged only once Prometheus has answered for 2 minutes; the scrape job's name is free; it carries a fixed reason, `never_seen` (no sample within the last hour) or `stale`) or `evaluator_stalled` (a watchdog sees no evaluator progress (a query answered or failed, a tick finished; a cancelled tick never counts) for 3 intervals, 90 s, and restarts the evaluator task once until a tick really finishes). A partial unique index allows one open event per `system_kind`. They have the same FIRING → RESOLVED lifecycle, need no Prometheus, can't be closed by hand (409), and go through the outbox to every channel with `system_alerts` true. A system alert resolves on the first tick that sees its cause gone ("Prometheus back after 12 min."). Shared causes: while `prometheus_unreachable` fires, every open alert goes into `no_data` with that reason (and, for alerts on polled devices, `metrics_not_collected` likewise); its NO DATA notification row is stored `folded` with `folded_into` the system alert's message to the same channel, and takes that message's outcome (a row is only ever folded into a message to its own channel: on a channel without `system_alerts` the NO DATA message waits the usual 5 minutes and is sent there, batched as usual); no 24 h reminder is sent for it. The first tick that reaches Prometheus again re-checks every alert: still-true ones return to `firing` without a message, and the system alert's RESOLVED message counts them ("5 alerts still firing, 2 recovering"). A device's other alerts in `no_data` (`source_down` / `source_stale`) are folded the same way into the FIRING message of its open instance-down alert (a rule whose every part is `vantage_instance_up` or `up` with a condition true for 0 and false for 1) on the channels that rule uses, or, once that message has been claimed, into one short follow-up message per channel; otherwise they are sent normally. A NO DATA message is held while its device has failed poll cycles but isn't down yet. A rule's query error shows in the rule API from the third failure in a row and sends no message. The probe and freshness state live in memory and start over on a restart.
- Time: the clock comes from the host. Containers share the host's kernel clock; Vantage never sets it (no `CAP_SYS_TIME`) and sends no NTP traffic; the host keeps time with its own NTP (chrony or systemd-timesyncd). Admins choose ONE timezone (IANA name, e.g. `Europe/Berlin`) and everyone sees times in it: web pages, Telegram messages, backend log lines (timestamp with offset) and CLI output. Stored data stays UTC; Prometheus is untouched. `app/core/timezone.py` holds the zone (loaded at startup, updated on PUT; the CLI reads the row).
- The master key is read from `VANTAGE_MASTER_KEY_FILE` (preferred) or `VANTAGE_MASTER_KEY`. The app refuses to start without it.
- Master-key check (`app/core/key_check.py`, `app/alerts/key_monitor.py`): the `settings` row `key_check` holds a fixed known value encrypted with the master key under its own row context (`row_context("settings", "key_check")`). The backend decrypts it at startup and every 15 s. If it fails, the backend runs in **key_mismatch mode**: reads, logins and the UI keep working; polling, live rates and notification delivery stop; secret writes are refused (see Contract 3); `/healthz` answers `key_mismatch`; an ERROR line is logged every 5 minutes (per-table counts, never values); a system alert `key_mismatch` is stored, its messages recorded `failed` with `error_kind = credentials_unreadable`. The first check that passes again resolves the alert and resumes. Upgrade without the row: every stored blob decrypts with the current key (or none is stored) → the row is created; none decrypt → `wrong_key`; some → `mixed_key`. A row that doesn't open while no secret blob is stored is sealed again with the current key (WARNING log), in the backend and in restore. Secret writes take a shared lock on the row. `app.cli rotate-master-key` re-encrypts the row in the same transaction as the secrets; restore keeps the backup's row (or creates one when every blob decrypts), checks it against the current key and refuses a mixed-key backup.
- Maintenance lock (`app/core/maintenance.py`, `app/heartbeat.py`): `app.cli restore` and `app.cli rotate-master-key` are offline commands. Each takes the `settings` row `maintenance_lock` = `{operation, started_at, host, pid, token}` with a plain INSERT (fails while the row exists: one holder), holds it until its transaction has ended, success or failure, then deletes it. A lock left by a killed command never expires: only `app.cli maintenance-unlock` removes it (it prints the holder; audited, `target_type` `maintenance_lock`). The backend rewrites the `settings` row `backend_heartbeat` = `{state, at, host, pid, token}` every 10 s, then reads the lock; a clean shutdown deletes its heartbeat. Handshake: each side commits its own marker, then reads the other's, so the two never both go ahead. After taking the lock the CLI refuses (and releases the lock) while the heartbeat is younger than 30 s; there is no `--force`. A lock seen by the running backend puts it into **maintenance mode** within one heartbeat: polling, live rates, alert evaluation (and its watchdog) and notification delivery pause; every state-changing API request answers 503 `{"detail", "reason": "maintenance"}` except login, logout and `POST /query`; reads keep working; `/healthz` answers `maintenance`. The next heartbeat without a lock resumes. The backend container's first step, before `alembic upgrade head`, is `python -m app.cli startup-gate`: it writes the heartbeat (`state` `starting`), then reads the lock, and with a lock present logs the holder, removes its heartbeat again and exits 1 (the restart policy retries); a database without tables passes. Backups leave both rows out; restore neither counts, replaces nor restores them.

---

## Contract 3: HTTP API (v1)

All routes under `/api/v1`, JSON, session auth, CSRF token on state-changing requests. In maintenance mode (see Contract 2, maintenance lock) every state-changing request except `POST /auth/login`, `POST /auth/logout` and `POST /query` answers 503 `{"detail": <short message>, "reason": "maintenance"}` and changes nothing; reads keep working. `POST /query` may answer 429 with `Retry-After` when its concurrency caps (in total and per session) are full, and when `VANTAGE_PUBLIC_HOSTS` is set every `/api/*` request whose `Host` isn't listed answers 400 `{"detail": "unknown host"}` (the `/ws` handshake is refused).

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/setup/status` | none | Is first-run setup needed |
| POST | `/setup` | none, only when no users exist | Create the first admin: `{username, password, setup_code}`. 409 once setup is done (checked first); a missing or wrong `setup_code`, or none stored, is 403 `{"detail", "reason": "setup_code_invalid"}` and creates nothing; wrong codes count on the per-client login limit (429). The code is never echoed, logged or audited |
| POST | `/auth/login`, `/auth/logout` | none / any | Session |
| GET | `/me` | any | Current user: `{id, username, role, csrf_token, server_version, server_commit}`; `server_version` = the release version: `VANTAGE_RELEASE` from the compose file (bumped with the image tags) when it is a plain version (`X.Y.Z` or `X.Y.Z-pre`; anything else is ignored with a WARNING), else the version the image was built as (the build's `VANTAGE_VERSION`, `"dev"` for an unstamped build). A promoted final release runs its rc's images, so only `VANTAGE_RELEASE` names it. `server_commit` = the source revision the image was built from (the build's `VANTAGE_COMMIT`), null when unknown. Unauthenticated routes (`/setup/status`, `/healthz`) never show a version |
| GET | `/about` | any | What is running: `{backend_version, backend_build_version, backend_commit, backend_build_date, schema_version, prometheus_version, started_at}`; `backend_version` = the release version (as `server_version`), `backend_build_version` = the version the image was built as (an rc's for a promoted final release), `backend_commit` = as `server_commit`, `backend_build_date` from the image build's `VANTAGE_BUILD_DATE`, `schema_version` = the current Alembic revision, `prometheus_version` from Prometheus' `/api/v1/status/buildinfo` through the query proxy's client (3 s timeout, cached 60 s), `started_at` = the backend's last start (UTC). Any value that can't be known is null; never an address or error text. Read-only; 401 without a session |
| POST | `/me/password` | any | Change your own password: `{current_password, new_password}`; ends your other sessions |
| GET/POST | `/users` | admin | List users / create one: `{username, password, role}` (no response carries a password or hash) |
| PATCH/DELETE | `/users/{id}` | admin | Change the role (`{role}`) / delete |
| POST | `/users/{id}/password` | admin | Reset a user's password: `{password}`; ends all of that user's sessions |
| GET | `/settings/time` | any | `{timezone, server_time, clock_source: "host", timezones: [...]}`: `server_time` = the backend's current UTC ISO time, `timezones` = sorted `zoneinfo.available_timezones()` |
| PUT | `/settings/time` | admin | `{timezone}`: 422 for an unknown zone; audited (`action` update, `target_type` `settings`, `target_id` `timezone`, the old and new names in `changes`). Maintenance-mode 503 applies as for any state-changing request |
| GET | `/settings/prometheus` | admin | `{url, mode, auth: {kind, username, secret_set, unreadable}}`: `url` = the configured `VANTAGE_PROMETHEUS_URL` (never credentials), `mode` = `bundled` / `external`; the secret is never returned; `unreadable` = the stored row can't be turned into request headers (damaged, or sealed with another key) |
| PUT | `/settings/prometheus` | admin | `{auth: {kind, username?, secret?}}`: an omitted secret = unchanged (422 "re-enter the password or token" when the kept secret can't be read); switching `kind` between `basic` and `bearer`, or from `none`, needs a new secret (422; a password is never reused as a token); saving a secret is a secret write (503 `key_mismatch` when the check fails); clearing (`kind: none`) is always allowed, also in key_mismatch mode, as the way to remove an unreadable credential. Audited (`target_type` `settings`, `target_id` `prometheus_auth`, the secret as `"<changed>"`). The backend's Prometheus requests (query proxy, evaluator, self-check, About) send these credentials; they still go through the SSRF guard |
| POST | `/settings/prometheus/test` | admin | `{ok, reason}`: a `vector(1)` probe with the saved settings; `reason` is a fixed code as in `/alerts/delivery-status`. CSRF-checked; 503 in maintenance mode like other state-changing requests |
| GET | `/integrations` | any | List types: type_id, name, category, JSON schema (secret fields flagged `writeOnly`) |
| GET | `/instances` | any | List instances (no secrets), each with `status` (see below) |
| POST | `/instances` | admin | Create (validates against schema + SSRF guard). Optional `color` (a palette name, else 422); omitted or null = auto-assigned. Every secret write (instance create/update with secrets, channel create/update with config) re-checks the master-key check value in its own transaction and is refused with 503 `{"detail", "reason": "key_mismatch"}`, nothing written, when it fails |
| GET/PATCH/DELETE | `/instances/{id}` | any / admin / admin | Read (no secrets), update (omitted secret = unchanged; `color` = a palette name, null or unknown is 422, audited like the other fields), delete |
| POST | `/instances/{id}/test` | admin | Run `test_connection` |
| POST | `/instances/test` | admin | Test an unsaved config |
| GET | `/instances/{id}/cards` | any | Card specs for the instance |
| GET | `/instances/{id}/now-playing` | any | Emby only (404 for other types): `{checked_at, sessions: [{user, title}]}`, who is playing what from the latest successful poll, cleaned and capped (50 rows). Kept in memory only, never in Prometheus (no viewing history); empty before the first poll, after a restart and for a disabled device |
| POST | `/query` | any | PromQL proxy: `{query, start?, end?, step?}`; backend enforces limits and rejects queries over the time/size budget. Scoped to the install like every query Vantage sends (see below) |
| GET/POST | `/dashboards` | any / admin | List `[{id, name, position}]` ordered by position, then id (creates "Default" when there is none, also when a viewer is first to load it) / create `{name, copy_from?}`: an empty layout or a copy of another dashboard's, appended last; at most 50 (409)  |
| GET/PUT | `/dashboards/{id}` | any / admin | Layout (PUT admin only) |
| PATCH/DELETE | `/dashboards/{id}` | admin | Rename / move `{name?, position?}`, positions kept dense / delete; the last dashboard can't be deleted (409). Dashboards are global (no owner, no sharing flag); the first by position is the main one, served at `/`, the others at `/d/{id}`. Names 1–64 characters, trimmed, duplicates allowed. Create, rename/move and delete are audited (`target_type` `dashboard`, the name in `changes`); layout saves are not  |
| GET/POST | `/maps` | any / admin | List `[{id, name, position}]` ordered by position, then id (none exist until an admin creates one) / create `{name, copy_from?}`: an empty drawing or a copy of another map's, appended last; at most 50 (409) |
| GET/PUT/PATCH/DELETE | `/maps/{id}` | any / admin / admin / admin | Read `{id, name, position, document, revision, updated_at}` / save `{document, revision}`: 409 when `revision` isn't the stored one (another admin saved meanwhile), else stored with `revision` + 1 / rename or move `{name?, position?}`, positions kept dense / delete (the last map may be deleted). Names 1–64 characters, trimmed, duplicates allowed. Create, rename/move and delete are audited (`target_type` `network_map`, the name in `changes`); saves of the drawing are not  |
| CRUD | `/alerts/rules`, `/channels` | admin | Alerts. Channels carry `system_alerts` (boolean, default true; settable on create and PATCH). Rule responses carry `recovery_mode` (`value` / `empty_result`) and `error` (the last query error seen for the rule, in memory, short, no secrets); a rule scoped to one instance whose results carry no `vantage_instance_id` is refused (422) at save; if Prometheus can't answer, it is saved and `error` set. Rule edits close affected open events in the same transaction: a new expression (ignoring whitespace) closes all (`rule_changed`), a new scope closes those outside it (`rule_changed`), disabling closes all (`rule_disabled`), DELETE is a soft delete that closes all (`rule_deleted`; a second DELETE is 404, deleted rules are never listed); name, severity, `for_seconds` and channels keep them. One message per rule and channel is queued in the same transaction when events were closed |
| GET | `/alerts/events` | any | Alert history; each event carries `instance_id`, `rule_name`, `severity`, `labels`, `fingerprint` and `end_reason`, plus `no_data_reason` and `state_since`, and `source` (`rule` / `system`) and `system_kind`; `state` filters by `firing`, `no_data` or `resolved`; `rule_id` is null once the rule is deleted. Each event also carries `deliveries`: `[{channel_id, channel_name, kind, status, attempts, error_kind, created_at, finished_at}]`, `channel_id`/`channel_name` null once the channel is deleted |
| GET | `/alerts/delivery-status` | any | `{stuck, system_alerts}`: `stuck` = messages unfinished for more than 15 minutes, plus messages that failed within the last 24 h (not those of a resolved `key_mismatch` system alert); the UI shows a banner while it is above 0. `system_alerts` = `[{system_kind, since, reason}]`, the firing system alerts, oldest first; the UI shows a banner for each. `reason`: for `prometheus_unreachable`, why the latest probe failed, a fixed code (`name_not_found` / `address_refused` / `connection_refused` / `timeout` / `server_error` / `connection_failed` / `auth_failed` (Prometheus answered 401/403) / `credentials_unreadable` (the stored Prometheus credentials can't be decrypted; nothing was sent); later codes may be added), in memory, null when not known; null for the other kinds. For `metrics_not_collected`: `never_seen` / `stale` (in memory, null when not known). The banner turns it into a cause and a fix, never an address |
| POST | `/alerts/events/{id}/close` | admin | Close an open (`firing` or `no_data`) event by hand (`end_reason = closed_by_user`), audited; 404 if unknown, 409 if already closed or a system alert |
| GET | `/audit` | admin | Audit log, newest first: `limit` (1–200, default 50), `before_id` (keyset paging); each entry carries `actor_type` |
| WS | `/ws` | any | Live pushes: instance status changes, alert events (each with `instance_id` and `fingerprint`; `state` may be `no_data`, with `no_data_reason` and `state_since`; each with `source` and `system_kind`), card data refresh hints, and `live_rate`: the summed in/out bits per second of the main dashboard's throughput card's ports (the first dashboard by position; other dashboards show the Prometheus rate), about every 2 s while a browser is connected (numbers only; not stored) |

User management: the last admin can't be demoted or deleted (409), and an admin can't delete their own account (409; another admin has to). Every user change (create, role, password reset or change, delete) is audited, the password only as `"<changed>"`.

Install scope: every PromQL query Vantage sends (`/query`, alert rules and their witness and probe queries, the self-check, About, the startup seed) goes through `app/query/proxy.py`'s client, which first rewrites it (`app/query/scope.py`) so every vector selector carries a `vantage_install` matcher, replacing any matcher on that label in the query: `=~"<id>|"` in `bundled` mode (series stored before the label existed stay visible), `="<id>"` in `external` mode. `VANTAGE_PROMETHEUS_MODE` (`bundled` / `external`) sets the mode; unset, it is `bundled` only when `VANTAGE_PROMETHEUS_URL` is unset or the bundled default `http://prometheus:9090`, else `external`. The rewriter reads the query with its own lexer and fails closed: anything it can't place with confidence (control characters other than space, tab and newline; keywords such as `and`, `or`, `offset` in operand position, where Prometheus would read them as metric names; `info()`, which joins `target_info` series no selector names) is refused with 400 `query not supported` and never sent.

Instance responses (list and single) carry `color` (the palette name) and `status`: `{state, checked_at, last_success_at, error}` with `state` one of `pending` / `up` / `down` / `unknown`. It comes from the poll collector's in-memory state, so `state`, `checked_at` and `error` reset on restart; `last_success_at` is seeded from Prometheus at startup (empty if Prometheus can't answer). `down` means N failed poll cycles in a row (`VANTAGE_DOWN_AFTER_FAILED_CYCLES`, default 3); until then the state stays `up` (or `pending`) and `error` shows the latest failure. `pending` = enabled and polled by the collector, but not polled yet (new, just enabled, or just after a restart), or not yet answered with fewer than N failed cycles. `unknown` = the collector doesn't poll this instance (disabled, or a scrape-only integration). `error` is the collector's short, secret-free description. `/ws` pushes changes to the same object.

Outside `/api/v1`:
- `GET /healthz`: unauthenticated. `200 {"status": "ok"}`, or `503 {"reason": <code>}` with exactly one fixed code and nothing else; when several apply, the first of `maintenance` (the maintenance lock is held), `key_mismatch` (the master key doesn't match the stored secrets), `evaluator_stalled`, `prometheus_unreachable`, `metrics_not_collected`, `notifications_stuck` (a message unfinished for more than 15 minutes or failed within 24 h, except the failed messages of a resolved `key_mismatch` system alert, whose RESOLVED message stands in for them; counted at most every 5 s) wins. `GET /healthz/live`: 200 while the web server runs (setup.sh and any container healthcheck use it, so an outage never restarts the backend).
- `GET /sd/targets`: Prometheus HTTP SD format, **bearer-token protected** (token from env, shared only with Prometheus)
- `GET /metrics`: polled metrics + Vantage's own health metrics, bearer-token protected. `vantage_instance_up` is emitted only for instances the collector polls; scrape-only targets rely on Prometheus' own `up`. Every sample carries `vantage_install`, and the gauge `vantage_build_info{version, commit}` = 1 reports the running build (the self-check looks for it). Next to it, `vantage_poll_consecutive_failed_cycles` counts each polled instance's failed poll cycles in a row. `vantage_alert_notifications_pending` (gauge: unfinished outbox rows) and `vantage_alert_notifications_failed_total` (counter: rows that ended `failed` since the backend started) describe notification delivery.
- Prometheus token: read from `VANTAGE_PROMETHEUS_TOKEN_FILE` (preferred) or `VANTAGE_PROMETHEUS_TOKEN`, at least 32 characters. Not configured → the app starts and both endpoints answer 503. Configured but unusable (unreadable, empty, too short) → the app refuses to start.

---

## Contract 4: Prometheus bootstrap

`deploy/prometheus/prometheus.yml` is fixed and contains only:
- a job using `http_sd_configs` pointed at `http://backend:8000/sd/targets` with bearer auth. It carries generic, non-device-specific scrape limits: `sample_limit: 50000`, `label_limit: 64`, `label_value_length_limit: 2048`, `body_size_limit: 16MB`
- a job scraping `http://backend:8000/metrics` with bearer auth
- Prometheus self-scrape

Nothing device-specific is ever added to it.

---

## Contract 5: SSRF guard

`core/net_guard.py` resolves the hostname **once**, checks every resolved address, then connects to that checked address (prevents DNS rebinding).

- **Allowed by default:** RFC 1918, RFC 6598 (CGNAT), IPv6 ULA (`fc00::/7`)
- **Always denied:** loopback, link-local (`169.254.0.0/16`, `fe80::/10`), `0.0.0.0/8`, multicast, reserved `240.0.0.0/4` (incl. broadcast), deprecated site-local `fec0::/10`, `::/96` (unspecified, loopback, IPv4-compatible), the cloud metadata addresses (`169.254.169.254`, `fd00:ec2::254`, `100.100.100.200`, `168.63.129.16`), and the Vantage host's own addresses
- **Configurable:** extra allowed CIDRs via settings (admin only)
- **Public internet:** only an integration's declared `cloud_hosts`, over HTTPS, with certificate verification on; a declared cloud host must resolve to public addresses only (any private, CGNAT, ULA or otherwise non-global answer denies it)
- Redirects are not followed automatically. If needed, each hop is re-checked
- Timeouts on every request (connect 5 s, total 15 s by default)
- **ICMP ping:** real ICMP echo is allowed, only through a guarded ping client in `net_guard`. It follows the same rules as every other outbound call: resolve once, check every address, ping only the checked address, never a cloud host; it also caps the packet count and total time. Raw sockets never appear in integration code. The backend container may send ICMP: prefer unprivileged ICMP datagram sockets (the `net.ipv4.ping_group_range` sysctl on the container); add `cap_add: NET_RAW` only if that proves unworkable. The container keeps running as a non-root user either way.
- **Scrape targets:** Prometheus connects to targets itself, outside `net_guard`, so `/sd/targets` checks every target with the instance's guard first and leaves out any it refuses (a DNS failure also leaves the target out). Prometheus resolves hostnames again when scraping, so only IP-literal targets are fully protected against DNS rebinding.

---

## Frontend rules

- Cards are registered by `card_type`; each card gets `instanceId` and its settings, and fetches data only through the typed API client (`/query` and instance endpoints).
- Forms for instances are generated from the integration's JSON schema. Secret fields render as password inputs, are never pre-filled, and show "set" / "not set".
- Must work at phone width (single column) and desktop (grid). The network map page shows no drawing on a phone: it lists the map's devices and problems instead.
- Light and dark mode.
- All times are shown in the configured timezone (Contract 3, `/settings/time`), not the browser's. Admin pages (time, users, audit log, notification channels) live under one Settings area (`/settings/...`).
- No secrets, tokens or session data in `localStorage`. The session is an httpOnly cookie.

---

## Testing rules

- Integration tests use **recorded or hand-written fixtures**. Never require real devices or real credentials in CI.
- Every integration must have tests for: schema validation, `test_connection` success and failure (including the firewall hint on timeout), poll parsing, and "no write endpoints called".
- API tests must assert secret values never appear in any response body.
- The SSRF guard has its own test suite covering every denied range and DNS rebinding.

---

## Workflow for agents

- **One task per branch**, named `m<milestone>/<short-task>` (for example `m4/proxmox-integration`). One PR per task.
- Keep PRs small enough to review in one sitting. If a task grows, split it and say so.
- PR description must include: what changed, how it was tested, any contract questions, new dependencies and why.
- **A different agent reviews every PR** than the one that wrote it, using the checklist below. The maintainer merges.
- Don't touch files outside your task's scope except to fix something that blocks you; note it in the PR.
- When the maintainer approves a contract change, change the contract text and record the approval in `docs/decisions/` (see its README); don't add dated "approved" notes to this file.
- If information is missing, leave a clear `TODO(maintainer):` and say so in the PR rather than inventing behavior.
- A change to what users see or do in the UI (a page, button, label, field, role or workflow) updates `docs/USER-GUIDE.md` in the same PR.

## Review checklist

- [ ] Hard rules 1–9 respected
- [ ] No secret can reach a response, log line, error message or audit entry
- [ ] All outbound calls go through `net_guard`
- [ ] No device write endpoints called
- [ ] Tests cover success, failure and the security assertions
- [ ] Contracts unchanged, or the change is approved
- [ ] Changed a default (a setting or environment variable default, a compose value, a role's permissions, existing behaviour)? `CHANGELOG.md` says so as a **Behaviour change**, with what upgrading users have to do, if anything
- [ ] Docs updated (`docs/INTEGRATIONS.md` for new integrations)
- [ ] `docs/USER-GUIDE.md` updated when the PR changes what users see or do in the UI

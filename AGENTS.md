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
| Frontend | React 18+, TypeScript (strict), Vite, Tailwind CSS, ECharts, react-grid-layout, TanStack Query, react-jsonschema-form (or equivalent) for dynamic forms |
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
- `InstanceContext.snmp_client(host, auth, *, port=161, timeout=5.0, retries=2)` (maintainer-approved 2026-10-28): `auth` is `SnmpV1 | SnmpV2c | SnmpV3`. The keyword-only `timeout` (seconds per attempt, more than 0 and at most 5) and `retries` (0 to 2) can only lower the guarded client's own defaults (5 s, 2 retries); anything outside raises `ValueError`. Every `get()`/`walk()` still stops after the client's 15 s total. The client offers GET and WALK only.
- `InstanceContext.http_client(*, verify: bool | ssl.SSLContext = True, base_url="", headers=None)` (maintainer-approved 2026-10-28). `True` verifies against the system CAs; an `ssl.SSLContext` verifies against a certificate the admin supplied (pinning, for self-signed devices); `False` turns verification off and is only ever an explicit, off-by-default per-instance opt-in. (Unchanged, existing behaviour of the guard: a cloud host is still refused unless verification of both the certificate and the hostname is on.)
- `Sample` = metric name, labels, value, type (gauge/counter). The collector adds the standard labels.
- Metric names: `vantage_<type_id>_<metric>` for polled metrics. Scraped exporter metrics keep their native names.
- Polled-metric labels are fixed by the collector: `vantage_instance_id`, `vantage_instance`, `vantage_integration`, `vantage_site`. Integrations may add their own labels but never override these.
- Scrape targets get the same four labels through service discovery.
- An instance with no site gets `vantage_site=""`. Prometheus treats an empty label as absent, so such series carry three labels; that's intended. Never substitute a placeholder site name. The UI shows "No site".
- An integration is registered by being listed in `registry.py`. No dynamic imports from user input.

---

## Contract 2: data model

| Table | Key fields |
|---|---|
| `users` | id, username, password_hash (argon2), role (`admin` / `viewer`), created_at, last_login_at |
| `instances` | id (uuid), type_id, name (unique), site, tags (json), enabled, config (json, non-secret fields only), secrets (encrypted blob), created_at, updated_at |
| `links` | id, a_instance_id, b_instance_id, label, a_port, b_port (for the network map) |
| `dashboards` | id, name, layout (json: card positions and per-card settings) |
| `alert_rules` | id, name, expr (PromQL), for_seconds, severity, instance_id (nullable), channel_ids, enabled, deleted_at (nullable; soft delete), revision (maintainer-approved 2026-11-01) |
| `alert_events` | id, rule_id (nullable, FK `alert_rules` ON DELETE SET NULL), fingerprint, labels (json), instance_id (nullable, FK `instances` ON DELETE SET NULL), rule_name, severity, state (`firing` / `no_data` / `resolved`; `no_data` maintainer-approved 2026-11-02), started_at, ended_at, end_reason (nullable: `recovered` / `rule_disabled` / `rule_deleted` / `rule_changed` / `closed_by_user`), summary (maintainer-approved 2026-11-01), no_data_reason (nullable: `source_stale` / `source_down` / `metric_missing` / `query_failed`; no CHECK, later reasons may be added), state_since, reminder_sent_at (maintainer-approved 2026-11-02) |
| `channels` | id, type (`telegram`), name, config (json), secrets (encrypted blob) |
| `alert_notifications` | id, event_id (nullable, FK `alert_events`), channel_id (nullable, FK `channels` ON DELETE SET NULL), kind (`firing` / `resolved` / `rule_change` / `summary`), text, status (`pending` / `sending` / `delivered` / `failed` / `folded` / `cancelled`), attempts, next_attempt_at, claimed_at, claim_token, folded_into (nullable, FK self), error_kind, created_at, finished_at (maintainer-approved 2026-11-01) |
| `audit_log` | id, user_id, actor_type (`user` / `cli` / `system`), action (`create` / `update` / `delete` / `login` / `logout` / `login_failed`), target_type, target_id, at, changes (json, **secret fields replaced with `"<changed>"`**) |
| `settings` | key, value (json) |
| `login_failures` | id, user_id (FK `users`, cascade delete), at |
| `sessions` | id (SHA-256 of the session token), user_id (FK `users`, cascade delete), created_at, expires_at, last_seen_at |

- `instances.config` and `instances.secrets` are split by the integration's schema: `SecretStr` fields go to `secrets`, everything else to `config`.
- `login_failures` holds one row per failed login for an **existing** user: who and when, nothing else (no submitted username, password or client address). The per-user limit counts rows in a sliding 15-minute window, so lockouts survive a restart and can't be evicted. Usernames that match no user, and the per-client limit, stay in the in-memory limiter. A successful login, `app.cli unlock <username>` and `app.cli reset-password` clear the user's rows.
- A login refused (429) because the account is locked writes an `audit_log` entry: `action=login_failed`, the user as target, `changes` = `{"username": <stored username>, "locked": true}` (never the submitted username or password). At most one per account per minute, best effort; refusals by the per-client limit or for unknown usernames aren't audited.
- `audit_log.actor_type` says who acted, and every audit write sets it: `user` = a request through the API (`user_id` is the logged-in user), `cli` = the admin CLI on the server, `system` = the backend itself. `cli` and `system` entries never have a `user_id`.
- `sessions` holds one row per login session. The cookie carries a random token; the database stores only its SHA-256 hash, so a copy of the database can't be used to log in. Deleting a row ends that session: logout, a new login on the same browser, password reset, expiry or idle timeout, and user deletion (cascade).
- Alerts (maintainer-approved 2026-11-01): an alert is one series of a rule's result, identified by (rule_id, fingerprint); `app/alerts/labels.py` defines the fingerprint (SHA-256 over the series' identity labels: every label except `__*`, `job`, `instance`, `vantage_instance`, `vantage_site`, `vantage_integration`, the integrations' display labels and sensitive-looking names). A partial unique index allows one open event (`firing` or `no_data`, maintainer-approved 2026-11-02) per (rule_id, fingerprint). `rule_name`, `severity`, `labels` and `instance_id` are snapshots taken when the event opens; `labels` holds only filtered labels (sensitive values `"<redacted>"`, URLs without credentials or query, at most 16). Events from before this change have `fingerprint = "legacy"`. No rule operation deletes an event row: deleting a rule sets `deleted_at` and closes its open events. Every rule change bumps `revision`.
- Alert states (maintainer-approved 2026-11-02): an open event resolves (`recovered`) only after two clean checks on two different polls: the rule's query succeeded, the series is absent from its result, the rule's value query (the *witness*, split from the expression by `app/alerts/promql.py`) returned the series with a value failing the condition, and the series' source (the device's last successful poll, or its last `up == 1` scrape) is fresh (younger than 2 × poll interval + 30 s, or 90 s when scraped) and newer than the one the previous clean check used; a series without `vantage_instance_id` has no source check. Rules the splitter can't split (`recovery_mode = empty_result`) resolve on an empty result with a fresh, advanced source. A missing value never resolves an alert: a stale source (`source_stale`, or `source_down` when the instance is down), a series missing from a newer poll (`metric_missing`) or the rule's query failing 3 ticks in a row (`query_failed`) moves it to `no_data`, which stays open until the value returns or an admin closes it. A failed `vector(1)` probe changes nothing. Messages: NO DATA after 5 minutes in `no_data`, one reminder after 24 h, "data back, still firing" only if the NO DATA message was sent; all through the outbox in the same transaction. RECOVERING counts and pending timers live in memory and start over on a restart and on a rule revision change.
- Notification delivery (maintainer-approved 2026-11-01): `alert_notifications` is the outbox. One row per (alert change, channel) is inserted in the same transaction as the change (FIRING, RESOLVED, a rule edit's closure message); its text is rendered then, holds no secret and is capped at 4,096 characters. One dispatcher in the backend claims rows (`pending` → `sending`, conditional update; `FOR UPDATE SKIP LOCKED` on Postgres), only the oldest unfinished row per channel, at most 4 sends at once and 1 per channel. Several ready rows for a channel go as one message (20 lines, then "and N more, see Alerts"); the carried rows become `folded` into the sent one. Retries after 10 s, 30 s, 1 min, 2 min, 5 min, then every 10 min until the row is 24 h old (`failed`, `expired`); a Telegram 429 waits `retry_after` (at most 10 min); Telegram 400/401/403/404 and unreadable channel credentials fail at once. `error_kind` is a fixed code, never a response text. A channel with 500 unfinished rows stores new rows as `folded` into one pending `summary` row that counts them; such a row keeps `finished_at` empty ("queued in summary") until the summary finishes, then takes its outcome (delivered, `failed` with the summary's `error_kind`, or `cancelled`). Deleting a channel cancels its unfinished rows. At startup every `sending` row goes back to `pending` (attempt kept): delivery is at least once, so a duplicate is possible, a loss is not. No row is deleted in v0.1.
- The master key is read from `VANTAGE_MASTER_KEY_FILE` (preferred) or `VANTAGE_MASTER_KEY`. The app refuses to start without it.

---

## Contract 3: HTTP API (v1)

All routes under `/api/v1`, JSON, session auth, CSRF token on state-changing requests.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/setup/status` | none | Is first-run setup needed |
| POST | `/setup` | none, only when no users exist | Create the first admin |
| POST | `/auth/login`, `/auth/logout` | none / any | Session |
| GET | `/me` | any | Current user |
| POST | `/me/password` | any | Change your own password: `{current_password, new_password}`; ends your other sessions |
| GET/POST | `/users` | admin | List users / create one: `{username, password, role}` (no response carries a password or hash) |
| PATCH/DELETE | `/users/{id}` | admin | Change the role (`{role}`) / delete |
| POST | `/users/{id}/password` | admin | Reset a user's password: `{password}`; ends all of that user's sessions |
| GET | `/integrations` | any | List types: type_id, name, category, JSON schema (secret fields flagged `writeOnly`) |
| GET | `/instances` | any | List instances (no secrets), each with `status` (see below) |
| POST | `/instances` | admin | Create (validates against schema + SSRF guard) |
| GET/PATCH/DELETE | `/instances/{id}` | any / admin / admin | Read (no secrets), update (omitted secret = unchanged), delete |
| POST | `/instances/{id}/test` | admin | Run `test_connection` |
| POST | `/instances/test` | admin | Test an unsaved config |
| GET | `/instances/{id}/cards` | any | Card specs for the instance |
| POST | `/query` | any | PromQL proxy: `{query, start?, end?, step?}`; backend enforces limits and rejects queries over the time/size budget |
| GET/PUT | `/dashboards/{id}` | any / admin | Layout |
| CRUD | `/links` | any / admin | Network map links |
| CRUD | `/alerts/rules`, `/channels` | admin | Alerts. Rule responses carry `recovery_mode` (`value` / `empty_result`) and `error` (the last query error seen for the rule, in memory, short, no secrets); a rule scoped to one instance whose results carry no `vantage_instance_id` is refused (422) at save; if Prometheus can't answer, it is saved and `error` set (maintainer-approved 2026-11-02). Rule edits close affected open events in the same transaction (maintainer-approved 2026-11-01): a new expression (ignoring whitespace) closes all (`rule_changed`), a new scope closes those outside it (`rule_changed`), disabling closes all (`rule_disabled`), DELETE is a soft delete that closes all (`rule_deleted`; a second DELETE is 404, deleted rules are never listed); name, severity, `for_seconds` and channels keep them. One message per rule and channel is queued in the same transaction when events were closed (maintainer-approved 2026-11-01) |
| GET | `/alerts/events` | any | Alert history; each event carries `instance_id`, `rule_name`, `severity`, `labels`, `fingerprint` and `end_reason` (maintainer-approved 2026-11-01), plus `no_data_reason` and `state_since` (maintainer-approved 2026-11-02); `state` filters by `firing`, `no_data` or `resolved`; `rule_id` is null once the rule is deleted. Each event also carries `deliveries`: `[{channel_id, channel_name, kind, status, attempts, error_kind, created_at, finished_at}]`, `channel_id`/`channel_name` null once the channel is deleted (maintainer-approved 2026-11-01) |
| GET | `/alerts/delivery-status` | any | `{stuck}`: messages unfinished for more than 15 minutes, plus messages that failed within the last 24 h; the UI shows a banner while it is above 0 (maintainer-approved 2026-11-01) |
| POST | `/alerts/events/{id}/close` | admin | Close an open (`firing` or `no_data`) event by hand (`end_reason = closed_by_user`), audited; 404 if unknown, 409 if already closed (maintainer-approved 2026-11-01) |
| GET | `/audit` | admin | Audit log, newest first: `limit` (1–200, default 50), `before_id` (keyset paging); each entry carries `actor_type` |
| WS | `/ws` | any | Live pushes: instance status changes, alert events (each with `instance_id` and `fingerprint`, maintainer-approved 2026-11-01; `state` may be `no_data`, with `no_data_reason` and `state_since`, maintainer-approved 2026-11-02), card data refresh hints, and (maintainer-approved 2026-10-30) `live_rate`: the summed in/out bits per second of the dashboard throughput card's ports, about every 2 s while a browser is connected (numbers only; not stored) |

User management: the last admin can't be demoted or deleted (409), and an admin can't delete their own account (409; another admin has to). Every user change (create, role, password reset or change, delete) is audited, the password only as `"<changed>"`.

Instance responses (list and single) carry `status`: `{state, checked_at, last_success_at, error}` with `state` one of `pending` / `up` / `down` / `unknown`. It comes from the poll collector's in-memory state, so `state`, `checked_at` and `error` reset on restart; `last_success_at` is seeded from Prometheus at startup (empty if Prometheus can't answer) (maintainer-approved 2026-11-01). `down` means N failed poll cycles in a row (`VANTAGE_DOWN_AFTER_FAILED_CYCLES`, default 3); until then the state stays `up` (or `pending`) and `error` shows the latest failure (maintainer-approved 2026-11-01). `pending` = enabled and polled by the collector, but not polled yet (new, just enabled, or just after a restart), or not yet answered with fewer than N failed cycles. `unknown` = the collector doesn't poll this instance (disabled, or a scrape-only integration). `error` is the collector's short, secret-free description. `/ws` pushes changes to the same object.

Outside `/api/v1`:
- `GET /sd/targets`: Prometheus HTTP SD format, **bearer-token protected** (token from env, shared only with Prometheus)
- `GET /metrics`: polled metrics + Vantage's own health metrics, bearer-token protected. `vantage_instance_up` is emitted only for instances the collector polls; scrape-only targets rely on Prometheus' own `up`. Next to it, `vantage_poll_consecutive_failed_cycles` counts each polled instance's failed poll cycles in a row (maintainer-approved 2026-11-01). `vantage_alert_notifications_pending` (gauge: unfinished outbox rows) and `vantage_alert_notifications_failed_total` (counter: rows that ended `failed` since the backend started) describe notification delivery (maintainer-approved 2026-11-01).
- Prometheus token: read from `VANTAGE_PROMETHEUS_TOKEN_FILE` (preferred) or `VANTAGE_PROMETHEUS_TOKEN`, at least 32 characters. Not configured → the app starts and both endpoints answer 503. Configured but unusable (unreadable, empty, too short) → the app refuses to start.

---

## Contract 4: Prometheus bootstrap

`deploy/prometheus/prometheus.yml` is fixed and contains only:
- a job using `http_sd_configs` pointed at `http://backend:8000/sd/targets` with bearer auth
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
- **ICMP ping (maintainer-approved):** real ICMP echo is allowed, only through a guarded ping client in `net_guard`. It follows the same rules as every other outbound call: resolve once, check every address, ping only the checked address, never a cloud host; it also caps the packet count and total time. Raw sockets never appear in integration code. The backend container may send ICMP: prefer unprivileged ICMP datagram sockets (the `net.ipv4.ping_group_range` sysctl on the container); add `cap_add: NET_RAW` only if that proves unworkable. The container keeps running as a non-root user either way.
- **Scrape targets:** Prometheus connects to targets itself, outside `net_guard`, so `/sd/targets` checks every target with the instance's guard first and leaves out any it refuses (a DNS failure also leaves the target out). Prometheus resolves hostnames again when scraping, so only IP-literal targets are fully protected against DNS rebinding.

---

## Frontend rules

- Cards are registered by `card_type`; each card gets `instanceId` and its settings, and fetches data only through the typed API client (`/query` and instance endpoints).
- Forms for instances are generated from the integration's JSON schema. Secret fields render as password inputs, are never pre-filled, and show "set" / "not set".
- Must work at phone width (single column) and desktop (grid).
- Light and dark mode.
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
- If information is missing, leave a clear `TODO(maintainer):` and say so in the PR rather than inventing behavior.

## Review checklist

- [ ] Hard rules 1–9 respected
- [ ] No secret can reach a response, log line, error message or audit entry
- [ ] All outbound calls go through `net_guard`
- [ ] No device write endpoints called
- [ ] Tests cover success, failure and the security assertions
- [ ] Contracts unchanged, or the change is approved
- [ ] Docs updated (`docs/INTEGRATIONS.md` for new integrations)

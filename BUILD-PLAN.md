# Build Plan (agent-driven)

How Vantage gets built by coding agents, milestone by milestone. Every agent reads `AGENTS.md` first; this file says **what** to build and **in which order**, and `AGENTS.md` says **how**.

---

## How to run the agents

**Roles**
- **Builder agent:** takes one task, works on its own branch, opens a PR.
- **Reviewer agent:** a separate session that hasn't seen the work. It reviews the PR against the `AGENTS.md` checklist and runs the tests.
- **Maintainer (you):** answers contract questions, tests on the real lab, merges.

**In Claude Code:** `/task M2.1` does all of this for one task: it checks dependencies, runs the `builder` agent (`.claude/agents/builder.md`), waits for CI, runs a fresh `reviewer` agent, allows one fix round, and reports back. `/task M4b M4c` runs several in parallel worktrees. The templates below are the manual fallback.

**Ground rules for you**
- **Never give agents real credentials, IPs or lab access.** They build against fixtures. You test against your real network yourself, using private deployment notes kept outside this repo.
- Give each agent one task ID from this file plus `AGENTS.md`. Paste the task's acceptance criteria into the prompt.
- Merge in dependency order (see the map below). Don't start parallel tasks until their dependencies are merged.
- After each milestone, run the app yourself and click through it before starting the next one.

**Prompt template for a builder agent**
```
Read AGENTS.md and BUILD-PLAN.md. Do task <ID> only.
Work on branch m<N>/<name>. Follow all hard rules and contracts.
When done: all tests pass, open a PR with the description AGENTS.md requires,
and list anything you were unsure about as TODO(maintainer).
```

**Prompt template for a reviewer agent**
```
Read AGENTS.md. Review PR <link/branch> for task <ID> in BUILD-PLAN.md.
Check every item in the review checklist and the task's acceptance criteria.
Run the tests. Try to make secrets leak and to bypass net_guard.
Report: approve / request changes, with specific findings.
```

---

## Dependency map

```
M0 ──► M1 ──► M2 ──┬──► M4a ─┐
                   │         ├──► M5 ──┬──► M6
       M3 ◄────────┘  M4b…h ─┘         ├──► M7
   (needs M1 API)                      └──► M8 (after M6, M7)
```

- **Sequential:** M0 → M1 → M2. These set the contracts everything else depends on.
- **M3** (frontend shell) can start once M1's auth API is merged.
- **M4 tasks run in parallel**, one agent each, once M2 is merged.
- **M6 and M7** run in parallel after M5.

---

## M0: Scaffold and guardrails

| ID | Task | Acceptance criteria |
|---|---|---|
| M0.1 | Monorepo skeleton per `AGENTS.md` layout | Backend and frontend apps start; `GET /healthz` returns 200; frontend shows a placeholder page |
| M0.2 | Tooling | ruff, mypy, pytest, ESLint, Prettier, Vitest configured; one passing test each side |
| M0.3 | CI (GitHub Actions) | Lint + type check + tests + gitleaks + pip-audit + npm audit on every PR |
| M0.4 | Pre-commit | gitleaks, ruff, Prettier hooks |
| M0.5 | Deploy skeleton | `deploy/docker-compose.yml` with backend, frontend, Prometheus; fixed `prometheus.yml` per Contract 4; `.env.example` with no real values |

## M1: Backend core

| ID | Task | Acceptance criteria |
|---|---|---|
| M1.1 | Config + DB | SQLAlchemy models for every table in Contract 2; Alembic initial migration; SQLite default, Postgres via URL |
| M1.2 | Crypto service | Encrypt/decrypt secrets with the master key; app refuses to start without a key; tests for round-trip and wrong-key failure |
| M1.3 | Auth | Setup wizard API (only works when no users exist), login/logout, Argon2, httpOnly + Secure + SameSite cookies, CSRF, login rate limit, admin/viewer roles |
| M1.4 | Audit log | Every create/update/delete writes an entry; secret fields recorded as `"<changed>"`; test proves no secret reaches the table |
| M1.5 | SSRF guard | `net_guard` per Contract 5 with guarded httpx and SNMP clients; full test suite including DNS rebinding |

## M2: Integration framework

| ID | Task | Acceptance criteria |
|---|---|---|
| M2.1 | Contract + registry | `base.py` exactly per Contract 1; registry; `GET /integrations` returns JSON schemas with secrets flagged `writeOnly` |
| M2.2 | Instance API | CRUD + test endpoints per Contract 3; config/secrets split; omitted secret on PATCH = unchanged; test proves secrets never appear in responses |
| M2.3 | Service discovery | `/sd/targets` returns all enabled scrape targets in Prometheus HTTP SD format with the 4 standard labels; bearer auth |
| M2.4 | Poll collector | Scheduler runs `poll()` per instance on its interval; results on `/metrics` with standard labels; failures marked as instance status without crashing other polls; `vantage_instance_up` metric |
| M2.5 | Reference integrations | **HTTP/ping check** and **Linux host** (node_exporter scrape), fully tested. These are the templates for M4 |
| M2.6 | Integration docs | `docs/INTEGRATIONS.md`: how to write one, using M2.5 as the worked example |

**Carried over from reviews (include in the M2.6 task):**
- Host fields: the SSRF pre-check on save finds them by name only (`host`, `*_host`, `url`, `*_url`). Document the convention, and decide on explicit marking (proposal: `Field(json_schema_extra={"vantage_host": True})`, naming as fallback). See the `TODO(maintainer)` in `backend/app/integrations/instance_config.py`. (PR #12)
- URL fields must not carry credentials; they're refused at save time. Credentials go in `SecretStr` fields. (PR #12)
- Secret fields are top-level `SecretStr` (or `SecretStr | None`) only; nested and list secrets are rejected by the conformance suite. (PR #10)
- Author text on a secret field (`description`, `json_schema_extra`) is served in the schema as-is: never put example secret values there. (PR #10)

## M3: Frontend shell

| ID | Task | Acceptance criteria |
|---|---|---|
| M3.1 | App shell | Routing, layout, light/dark mode, phone and desktop widths, typed API client |
| M3.2 | Setup and login | First-run wizard, login, logout, session handling, role-aware navigation |
| M3.3 | Integrations UI | "Add device": pick type → form generated from schema → test connection (showing firewall hints on failure) → save; list, edit, disable, delete instances |
| M3.4 | Audit page | Admin-only table of audit entries |

**Carried over from reviews (include in the M3.2 task):**
- Call `api.me()` at app start, before any POST/PATCH/PUT/DELETE: the CSRF token lives only in memory, so after a reload writes are rejected until `me()` has run. (PR #11)
- The API client clears the CSRF token on every 401, including a failed `POST /auth/login`. `onUnauthorized` must not redirect-loop when the 401 comes from the login call itself. (PR #11)
- Navigation isn't role-aware yet (everyone sees "Audit log"); M3.2 hides admin-only links from viewers. (PR #11)
- Instance responses carry `secrets_set: {field: bool}` for the "set" / "not set" display (M3.3). (PR #12)

## M4: Built-in integrations (parallel, one agent each)

Each task: config schema, `test_connection`, scrape targets and/or `poll`, cards list, firewall hints, fixtures, tests per `AGENTS.md`, and a section in `docs/INTEGRATIONS.md`.

| ID | Integration | Notes |
|---|---|---|
| M4a | SMART add-on | Scrapes smartctl_exporter; per-disk health and temperature |
| M4b | Proxmox VE | API with a read-only token; nodes, guests (status, CPU, RAM, disk), storage pools; TLS verify on by default, with an explicit per-instance option for self-signed certs |
| M4c | pfSense | Scrapes Telegraf `prometheus_client`; interfaces, gateways (latency, loss), states, CPU/RAM |
| M4d | SNMP device | Generic IF-MIB poll (traffic, status, errors) with v1/v2c/v3 support |
| M4e | MikroTik SwOS | SNMP profile on top of M4d, with port naming and what SwOS exposes |
| M4f | cloudflared | Scrapes tunnel metrics; connections, requests, errors, latency |
| M4g | Cloudflare account | GraphQL analytics + tunnel status via `cloud_hosts = ("api.cloudflare.com",)`; per-zone traffic, cache, status codes, countries, threats; respects API rate limits |
| M4h | Emby | Sessions (now playing, transcode vs direct), library counts, server info; read-only endpoints only |

## M5: Dashboard

| ID | Task | Acceptance criteria |
|---|---|---|
| M5.1 | Query proxy | `/query` per Contract 3 with time range, step and series limits; rejects over-budget queries |
| M5.2 | Card framework | Card registry, grid via react-grid-layout, layout saved per dashboard, new instances' cards added automatically |
| M5.3 | Live updates | WebSocket pushes instance status and refresh hints; UI reconnects automatically |
| M5.4 | Overview row | Devices healthy, active alerts, WAN status, total throughput |
| M5.5 | Integration cards | One card per M4 integration (split into sub-tasks per card if parallelizing), including switch faceplate and NAS drive bays |

## M6: Network map

| ID | Task | Acceptance criteria |
|---|---|---|
| M6.1 | Links API | CRUD per Contract 3, validates instance ids |
| M6.2 | Map card | Draws instances and links; live status and link throughput where metrics exist; edit mode to add/remove links |

## M7: Alerts

| ID | Task | Acceptance criteria |
|---|---|---|
| M7.1 | Rules and evaluator | Backend evaluates PromQL rules on an interval, with `for` duration; firing/resolved events stored |
| M7.2 | Telegram channel | Sends via `cloud_hosts = ("api.telegram.org",)`; test-message button; secrets write-only |
| M7.3 | Default templates | Per integration: instance down, disk > 85%, SMART failure, high temperature, gateway loss, port errors rising, tunnel down, RAID degraded (when the metric exists) |
| M7.4 | Alerts UI | Rules CRUD, channels, event history |

## M8: Hardening and v0.1 release

| ID | Task | Acceptance criteria |
|---|---|---|
| M8.1 | HTTPS | Serves HTTPS with a user-supplied cert or a generated self-signed one |
| M8.2 | Backup/restore | Export and import DB + reminder that the master key is backed up separately |
| M8.3 | Security review | A dedicated reviewer agent does a full threat-model review; findings fixed; `docs/SECURITY.md` written |
| M8.4 | Docs and release | README quick start verified on a clean machine; versioned Docker images; changelog; license file |

---

## Later milestones

- **Security visibility:** firewall log ingestion (Loki), IDS alerts (Suricata/Snort), failed-login tracking, new-device detection
- **"What changed":** event timeline correlating alerts and metric changes across instances
- **AI:** anomaly detection on metrics; LLM-generated summaries (opt-in, local model option, never sends secrets)
- **Platform:** OIDC/SSO, passkeys, more alert channels, multiple sites, RouterOS integration, community plugin packaging

---

## Your checkpoints as maintainer

| After | Do this on the real lab |
|---|---|
| M2 | Run it, add a real Linux host running node_exporter and confirm it shows UP in Prometheus |
| M4 | Add each of your real devices; note anything an integration can't read |
| M5 | Live with the dashboard for a few days; list what's missing or noisy |
| M7 | Trigger a test alert to Telegram; unplug something harmless and confirm the alert |
| M8 | Re-run your firewall lockdown checks (blocked internet, allowed ports only) with the release build |

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

**Model tiers.** Each task has a tier; `/task` reads it and starts the builder and reviewer with those models. The reviewer is never weaker than the builder.

| Tier | Builder | Reviewer | Tasks |
|---|---|---|---|
| **Critical** (secrets, auth, crypto, `net_guard`, contracts, anything reachable from outside) | Opus | Opus | M1.6, M1.7, M1.8, M2.7, M3.5, M4b, M4d, M4g, M5.1, M7.2, M8.1, M8.2, M8.3 |
| **Standard** | Sonnet | Sonnet | Everything else: M2.6, M4a, M4c, M4e, M4f, M4h, M5.2–M5.5, M6.1–M6.2, M7.1, M7.3, M7.4, M8.4 |
| **Light** (docs-only or mechanical, no decisions) | Haiku | Sonnet | Docs-only PRs and plan edits; none of the numbered tasks yet |

- A task not listed is Standard, unless it touches secrets, auth, crypto, `net_guard` or a contract in `AGENTS.md`: then it is Critical.
- A fix round uses the same builder; a re-review uses a fresh reviewer at the same tier.
- Moving a task up or down is the maintainer's call: edit the table.

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

**Follow-up (test before changing):** `deploy/.env.example` says `chmod 644 secrets/*`, which makes `master.key` readable by every user on the host. Move to `600` or `640` with ownership matching the container user, fix the comment, and check the stack still starts.

## M1: Backend core

| ID | Task | Acceptance criteria |
|---|---|---|
| M1.1 | Config + DB | SQLAlchemy models for every table in Contract 2; Alembic initial migration; SQLite default, Postgres via URL |
| M1.2 | Crypto service | Encrypt/decrypt secrets with the master key; app refuses to start without a key; tests for round-trip and wrong-key failure |
| M1.3 | Auth | Setup wizard API (only works when no users exist), login/logout, Argon2, httpOnly + Secure + SameSite cookies, CSRF, login rate limit, admin/viewer roles |
| M1.4 | Audit log | Every create/update/delete writes an entry; secret fields recorded as `"<changed>"`; test proves no secret reaches the table |
| M1.5 | SSRF guard | `net_guard` per Contract 5 with guarded httpx and SNMP clients; full test suite including DNS rebinding |
| M1.6 | Admin CLI | `python -m app.cli`, run on the server (`docker compose exec backend python -m app.cli ...`): `reset-password <username>` (prompts for the new password, never takes it as an argument, never prints it; audited; ends that user's sessions), `create-user <username> --role admin\|viewer`, `list-users`. Needs no login: shell access to the host is the trust boundary. Tests prove the reset is audited, old sessions stop working, and no password reaches logs or the audit table |
| M1.7 | Trusted proxy and login rate limit | `VANTAGE_TRUSTED_PROXIES` (CIDR list; the compose file sets it to the frontend container). Only when the direct peer is a trusted proxy does the backend take the client address from a header that nginx **overwrites** from `$remote_addr` (not appends, unlike today's `X-Forwarded-For`); otherwise it uses the peer address. The login limiter keys on that real client address; remove the `TODO(maintainer)` in `auth.py`. Tests: a spoofed header from an untrusted peer is ignored, and one client's failures can't lock out another |
| M1.8 | Persistent per-user login counters | Failed-login counts per username live in the database (a data-model change, so Contract 2 is updated in the PR), not in the in-memory limiter. A spray of junk usernames can then never push a near-locked account out of the table, and lockouts survive a restart. The in-memory per-client limiter stays. Admin CLI (M1.6) gets `unlock <username>` so a legitimately locked admin can recover at once. Tests: the spray scenario from the PR #23 reviews (4 guesses on `admin`, then thousands of junk-username attempts from many addresses) leaves `admin` locked after the 5th guess; counters expire; nothing secret is stored |
| M1.9 | Audit locked-out login attempts | A login refused with 429 because the account is locked (its `login_failures` window is full) writes an audit entry: `action=login_failed`, the user as target, `changes` = `{"username": <the stored username>, "locked": true}`, never the submitted password or the submitted username (it is attacker-controlled; a case variant such as `ADMIN` counts against, and is recorded as, the stored account). Commit the entry before raising the 429, since the request's transaction rolls back. To keep a spray from filling the table, write at most one such entry per account per minute (look up the latest `login_failed` entry for that user with `locked: true`; plain `login_failed` entries must not suppress it). This is best-effort: parallel requests may each write one, and that's acceptable. Refusals by the per-client limit or for usernames that match no user stay unaudited as today. `docs/SECURITY.md` and the Contract 2 notes in `AGENTS.md` describe it (maintainer approved, 2026-10-28). Tests: the 6th and later attempts on a locked account (correct or wrong password) each refuse with 429, and only the first in each minute is audited with `locked: true`; nothing secret in the entry; the 5-failure lockout and `unlock` behave as before |

**Carried over from reviews (PR #34):**
- Done: the `sessions` table (M1.3) is now in the `AGENTS.md` Contract 2 table (maintainer approved, 2026-10-28).
- Login timing: for an existing username, login writes a `login_failures` row before the Argon2 check. An unknown username only runs the dummy Argon2 check. The difference is a few ms against tens of ms, and both paths give the same 401/429. Accepted as low severity. Note it in `docs/SECURITY.md` (M8.3).

## M2: Integration framework

| ID | Task | Acceptance criteria |
|---|---|---|
| M2.1 | Contract + registry | `base.py` exactly per Contract 1; registry; `GET /integrations` returns JSON schemas with secrets flagged `writeOnly` |
| M2.2 | Instance API | CRUD + test endpoints per Contract 3; config/secrets split; omitted secret on PATCH = unchanged; test proves secrets never appear in responses |
| M2.3 | Service discovery | `/sd/targets` returns all enabled scrape targets in Prometheus HTTP SD format with the 4 standard labels; bearer auth |
| M2.4 | Poll collector | Scheduler runs `poll()` per instance on its interval; results on `/metrics` with standard labels; failures marked as instance status without crashing other polls; `vantage_instance_up` metric |
| M2.5 | Reference integrations | **HTTP/ping check** and **Linux host** (node_exporter scrape), fully tested. These are the templates for M4 |
| M2.6 | Integration docs | `docs/INTEGRATIONS.md`: how to write one, using M2.5 as the worked example |
| M2.7 | ICMP ping | Guarded ICMP ping client in `net_guard`, exposed on `InstanceContext`, per Contract 5; the compose file lets the backend container send ICMP (prefer the `ping_group_range` sysctl over `NET_RAW`); a `ping_check` reference integration (packet loss and RTT min/avg/max, poll-based) with `net_guard`-style tests: private ranges only, denied addresses refused, DNS rebinding, packet and time caps |

**Carried over from reviews (include in the M2.6 task):**
- Host fields: the SSRF pre-check on save finds them by name only (`host`, `*_host`, `url`, `*_url`). Document the convention, and decide on explicit marking (proposal: `Field(json_schema_extra={"vantage_host": True})`, naming as fallback). See the `TODO(maintainer)` in `backend/app/integrations/instance_config.py`. (PR #12)
- URL fields must not carry credentials; they're refused at save time. Credentials go in `SecretStr` fields. (PR #12)
- Secret fields are top-level `SecretStr` (or `SecretStr | None`) only; nested and list secrets are rejected by the conformance suite. (PR #10)
- Author text on a secret field (`description`, `json_schema_extra`) is served in the schema as-is: never put example secret values there. (PR #10)
- `poll()` must await its I/O: the 60 s poll timeout only interrupts code that awaits, so a blocking call stalls every instance's polling. (PR #16)
- The collector assumes one backend process: with several uvicorn workers every worker polls and serves its own `/metrics`. Document "one worker" for deployments. (PR #16)
- Anything in a URL, including a query-string token, is stored in plaintext config and returned by the API: credentials belong in `SecretStr` fields, and the docs must say so. (PR #19)
- M2.6 comes after M2.7, so the ICMP ping client and `ping_check` are documented as well; the "HTTP check" is HTTP only.
- Scrape targets: one that fails DNS or the SSRF guard is left out of `/sd/targets`, so it disappears from Prometheus instead of showing `up == 0` (AGENTS.md Contract 5). (PR #14)

**Found in the real-stack Docker test (2026-10-28), not yet scheduled:**
- The poll collector keeps each instance's config in memory. It rereads the config on an API change or a restart, not on every poll. So a config that becomes invalid without going through the API (a direct database edit) shows "stored config is not valid for this integration" only after a restart. Upgrades always restart the backend, so this is low priority; decide whether to recheck on every poll.
- Prometheus logs one "connection refused" on its first `/sd/targets` fetch, because the backend isn't listening yet. Harmless; it recovers on the next refresh.
- Re-run on `main` after PRs #30–#34: the uvloop ping bug and the gateway drift are fixed. Still open:
  - Done (`m2/poll-status-noreply`): `NoReply` is now a `PollFailure` and the status shows "no reply from …". Was: a `ping_check` host that doesn't answer shows `poll failed (NoReply)` in its status. PR #33 gave `PingError` a readable message, but `NoReply` is `ping_check`'s own exception, so it still falls through to the generic branch in `collectors/scheduler.py`. Show the same text `test_connection` gives ("no reply from …"). Test the poll status for this case.
  - Login attempts refused with 429 while an account is locked aren't audited, so an admin can't see that someone kept trying. Scheduled as M1.9 (maintainer approved, 2026-10-28).
- Done (`m2/poll-status-noreply`, `test_poll_status_never_contains_config_or_secret_values`). From the PR #33 review: the scheduler's `PingError` branch puts the exception text in the instance status, trusting every `PingError` message to be secret-free by convention only. Add a test that the poll status never contains config values.
- CI never sends real ICMP (both event-loop variants of the socket test skip there), so `ping_check` changes still need a run on the real Docker stack.
- Docs nits from the PR #26 review, for the next edit of `docs/INTEGRATIONS.md`:
  - §11: "the API attaches only the first hint" holds only for the API's own timeout path.
  - §3 and `docs/integrations/ping_check.md`: point to the validator in `PingCheckConfig` instead of calling the bound a plain product.

## M3: Frontend shell

| ID | Task | Acceptance criteria |
|---|---|---|
| M3.1 | App shell | Routing, layout, light/dark mode, phone and desktop widths, typed API client |
| M3.2 | Setup and login | First-run wizard, login, logout, session handling, role-aware navigation |
| M3.3 | Integrations UI | "Add device": pick type → form generated from schema → test connection (showing firewall hints on failure) → save; list, edit, disable, delete instances |
| M3.4 | Audit page | Admin-only table of audit entries |
| M3.5 | User management | Admin-only screen and API: list and create users, change a role, reset another user's password, change your own, delete. The last admin can't be deleted or demoted. All audited. Contract 3 gains the user endpoints (the maintainer approves them in the PR) |

**Carried over from reviews (include in the M3.3 task):**
- Instance responses carry `secrets_set: {field: bool}` for the "set" / "not set" display. (PR #12)
- Add `status` to instance responses per AGENTS.md Contract 3 (backend: read it from the poll collector's in-memory store; `unknown` for instances it doesn't poll) and show it in the instance list. Show "No site" for instances without a site. (maintainer decision, PRs #14/#16)
- Logout: when logout fails and the follow-up `me()` also fails with a network error, the CSRF token stays null and every write gets 403 until reload. Move the session to the `unreachable` state instead, with a test. (PR #15)
- After an explicit logout, don't keep the current path as the post-login return path: the next person to log in on that browser shouldn't land on the previous user's page. (PR #15)

**Carried over from reviews (include in the M3.5 task):**
- `api/auth.py` login, lost-race path: it reads `user.id` after `db.rollback()`, which expires `user`. Once users can be deleted, a delete that lands during a login makes that reload fail, so the request returns 500 instead of 401. Keep the id in a local before the rollback. (PR #27)
- Refuse duplicate usernames case-insensitively in the user API, as `create-user` does: the login limiter keys on the lowercased name. (PR #27)
- Resetting another user's password from the API races logins the same way the CLI reset does. Reuse the CLI's order (write the user row, then delete sessions). Add a deterministic test for the order where the login holds the row lock first; today only the "reset commits first" order is tested. (PR #27)
- `create-user` reports any `IntegrityError` as "already exists", even when it isn't a duplicate. Share a narrower check with the API. (PR #27)
- Once users can be deleted, add a test for a user deleted while their login is in progress, between the `login_failures` commit and the password check. Today `expire_on_commit=False` keeps the loaded user usable, so it should return 401, not 500. (PR #34)
- Needs a maintainer decision (Contract 2), raised in PR #27: an `actor_type` column on `audit_log`, so CLI and system actions aren't told apart only by `user_id = NULL` plus `changes.source`; and a unique index on `lower(username)`.

## M4: Built-in integrations (parallel, one agent each)

Each task: config schema, `test_connection`, scrape targets and/or `poll`, cards list, firewall hints, fixtures, tests per `AGENTS.md`, and its own doc file `docs/integrations/<type_id>.md` (template in `docs/INTEGRATIONS.md`).

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

**Carried over from reviews (scrape integrations; one small follow-up task, or include in the next M4 task):**
- `test_connection` in the scrape integrations (`linux_host`, `smart_exporter`, `cloudflared`, `pfsense`) reads up to 8 MiB and parses it synchronously on the event loop, so a hostile or broken host can stall the loop briefly. Lower the cap (real exporter pages are tens of KB) or parse in a thread. (PR #30)
- The parsers catch only `ValueError` from `prometheus_client`, and the parse runs outside `test_connection`'s `try`. Any other exception on garbage input would make `test_connection` raise instead of returning a failed `TestResult`. Low priority: 3,000 random garbage inputs raised only `ValueError`. Hardening only: catch broadly around the parse and add a garbage-input test per integration. (PR #32)
- Docs: `docs/integrations/cloudflared.md` Config paragraph is garbled ("host is found by name by the save-time check"). Say instead that the SSRF guard resolves and checks the host when the instance is saved. `docs/integrations/pfsense.md` should name the port default (9273) instead of "Telegraf's default". (PR #31, PR #32)
- Not verified against real devices: cloudflared's default metrics port (20241) and metric names (PR #31), and the pfSense gateway metrics, which need a Telegraf `ping` input per gateway (PR #32). Maintainer lab checkpoint.

## M5: Dashboard

| ID | Task | Acceptance criteria |
|---|---|---|
| M5.1 | Query proxy | `/query` per Contract 3 with time range, step and series limits; rejects over-budget queries |
| M5.2 | Card framework | Card registry, grid via react-grid-layout, layout saved per dashboard, new instances' cards added automatically |
| M5.3 | Live updates | WebSocket pushes instance status and refresh hints; UI reconnects automatically |
| M5.4 | Overview row | Devices healthy, active alerts, WAN status, total throughput |
| M5.5 | Integration cards | One card per M4 integration (split into sub-tasks per card if parallelizing), including switch faceplate and NAS drive bays. Card types already declared by merged integrations, so they need a frontend card: `smart-disks` (PR #30), `cloudflared` (PR #31), `pfsense-overview` and `pfsense-gateways` (PR #32), plus the reference integrations' `http-check`, `linux-host` and `ping-check` |

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
| M8.2 | Backup/restore | Export and import DB + reminder that the master key is backed up separately; plus a master-key rotation command that re-encrypts every stored secret with a new key (needs the old key, all-or-nothing, audited) |
| M8.3 | Security review | A dedicated reviewer agent does a full threat-model review; findings fixed; `docs/SECURITY.md` written |
| M8.4 | Docs and release | README quick start verified on a clean machine; versioned Docker images; changelog; license file; `docs/ARCHITECTURE.md`. README gets an "Upgrading" section: when a release changes the compose network, run `docker compose down` then `up -d` (a plain `up -d` leaves the frontend without its fixed address and the dashboard down); otherwise `up -d --build` is enough, and migrations run on start |

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

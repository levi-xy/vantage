# Contributing

Thanks for your interest in Vantage. Pull requests are welcome for **integrations** and
**bug fixes**. The core (architecture, security, the API and data model, dependencies,
CI and deployment) stays with the maintainer.

## What's in scope

| Welcome | Where it lives |
|---|---|
| A new integration (a device type, exporter or service) | `backend/app/integrations/builtin/<type_id>/`, one line in `backend/app/integrations/registry.py`, tests in `backend/tests/integrations/builtin/`, cards in `frontend/src/cards/<card_type>/`, `docs/integrations/<type_id>.md` |
| Fixes and improvements to an existing integration or its cards | the same folders |
| Front panel presets for real hardware | `frontend/src/pages/maps/editor/presetModels.ts` |
| Bug fixes in pages and components | `frontend/src/pages/`, `frontend/src/components/` |
| Docs fixes | `README.md`, `docs/INTEGRATIONS.md`, `docs/OPERATIONS.md`, `docs/integrations/`, `docs/exporters/` |

**Those paths are the only ones open to contributors.** A CI check (*Contribution scope*) fails
any PR from a fork that touches a file anywhere else. That includes:

- the core and security: everything else under `backend/` (the SSRF guard, auth, crypto, the
  API, data model, migrations, alerts, the Integration contract) and the shared
  `integrations/builtin/common.py`
- the login, account, users, audit log, notification channels, setup and time pages, and the
  sign-in screen
- the app shell and API client (`frontend/src/app/`, `frontend/src/api/`), dependencies,
  Dockerfiles, tool configs, CI, `deploy/`, `scripts/`, `AGENTS.md`, `docs/SECURITY.md`,
  `docs/ARCHITECTURE.md`, the license files and this file

If your change needs a file outside those paths (a new dependency, a display label in
`alerts/labels.py`, a field in the Integration contract), don't change it: describe it in the
PR or in your proposal, and the maintainer decides and makes the change.

## Before you start

- **New integration:** open an issue with the *Integration proposal* form first. It saves you
  building something that can't be merged (for example a device that needs a write call to
  read its status).
- **Bug fix:** an issue isn't required for a small fix; for anything larger, open a *Bug report*
  first so we agree on the cause.

## The rules every PR follows

The full rules are in [AGENTS.md](AGENTS.md) (hard rules and contracts) and
[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) (how to write an integration, with a checklist
for the PR). The short version:

1. **Read-only toward devices.** No call that changes device state, ever, even if the API
   offers it.
2. **All network access through the guarded clients** that the integration's context provides
   (`ctx.http_client()`, `ctx.snmp_client()`, `ctx.ping_client()`). No `httpx`, `requests`,
   `socket` or SNMP library calls of your own.
3. **Secrets are `SecretStr` fields** and never appear in a metric, label, log line, error
   message or API response.
4. **No real data from any network** in code, tests, fixtures, docs or screenshots: no real
   IPs, hostnames, domains, serial numbers or credentials. Use `192.0.2.0/24`, `10.0.0.0/8`,
   `switch.example` and `example-token`.
5. **No new dependencies.** Use the standard library and what the project already uses.
6. **Tests with hand-written or recorded fixtures**; CI never talks to a real device. An
   integration needs tests for its schema, connection test (success, failure, firewall hint),
   parsing, and that no write endpoint is called.
7. **Keep PRs small**: one integration or one fix per PR.

## Setting up and running the checks

```
python scripts/dev_setup.py     # creates backend/.venv and installs the frontend packages
python scripts/check.py         # runs the same checks as CI
python scripts/check.py --fast  # skips the slow audits
```

Backend: Python 3.12+. Frontend: the Node version in `.nvmrc`. CI (backend, frontend, gitleaks
and the scope check) must be green before review. CI on a first PR waits until the maintainer
approves the run.

## Review and merging

The maintainer reviews every PR against the checklist in [AGENTS.md](AGENTS.md) and merges with
a squash. Expect questions about where data comes from and what the device does with each call.
A PR may be closed if it's out of scope, needs a contract change that isn't approved, or goes
quiet for a month.

By contributing you agree that your contribution is licensed under the project's
[MIT License](LICENSE).

## Bug reports, findings and questions

- **Bug reports:** the *Bug report* issue form.
- **Hardware findings:** if an integration shows wrong or missing data for your device, or works
  well on a model that isn't listed, use the *Integration finding* form.
- **Questions and ideas:** [Discussions](https://github.com/levi-xy/vantage/discussions).

Whatever you post, leave out IP addresses, hostnames, domains, usernames, tokens, passwords and
community strings.

## Security

Report vulnerabilities privately, as described in
[docs/SECURITY.md](docs/SECURITY.md#reporting-a-vulnerability), never in a public issue or PR.
Security fixes are made by the maintainer, so please don't open a PR for one.

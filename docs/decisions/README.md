# Decisions

The maintainer's approved changes to the contracts in [AGENTS.md](../../AGENTS.md), one file per approval date. AGENTS.md says what the contracts **are**; these files say **when** each part was approved and **what it replaced**. The contract text is the authority. If a file here and AGENTS.md disagree, AGENTS.md wins, and the file gets fixed.

When the maintainer approves a contract change:

1. Change the contract text in AGENTS.md. Don't add "(approved <date>)" notes to it.
2. Add a bullet to `YYYY-MM-DD.md` for the approval date (create the file if needed): what was approved, where it lives in AGENTS.md, what it replaced, and the PR or task.
3. Add the file to the list below.

Approvals before 2026-10-28 (the first contract decisions, the audit fields, the net_guard ranges) were recorded straight into AGENTS.md and have no file here.

| Date | Topics |
|---|---|
| [2026-10-27](2026-10-27.md) | ICMP ping through the guard |
| [2026-10-28](2026-10-28.md) | SNMP client timeouts, HTTP client certificate pinning |
| [2026-10-30](2026-10-30.md) | Live throughput rate over `/ws` |
| [2026-11-01](2026-11-01.md) | One event per series, notification outbox, poll failure tolerance, Prometheus scrape limits |
| [2026-11-02](2026-11-02.md) | NO DATA state, system alerts (P0-4), master-key check and maintenance lock (P0-6), multiple dashboards |
| [2026-11-03](2026-11-03.md) | Device colours, Emby now playing, why Prometheus is unreachable |
| [2026-11-07](2026-11-07.md) | Network maps (replace links), timezone |
| [2026-11-08](2026-11-08.md) | Detailed network maps (M11), port numbering order |
| [2026-11-10](2026-11-10.md) | One-time setup code |
| [2026-11-13](2026-11-13.md) | Viewers read-only for dashboards, release version and About page |

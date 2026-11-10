<!-- Thanks for contributing. Read CONTRIBUTING.md first: PRs from forks may change integrations,
cards, presets and docs, and fix bugs outside the core. -->

## What changed

<!-- One integration or one fix. Link the issue: "Closes #123". -->

## How it was tested

<!-- Which tests you added, and which real device and firmware you tried it on, if any. -->

## Device calls

<!-- For integration changes: every endpoint, OID or command the integration calls, and why each is read-only. Write "none" otherwise. -->

## Checklist

- [ ] The change stays in scope (CONTRIBUTING.md, "What's in scope"); anything the maintainer needs to change is described above
- [ ] Read-only toward devices; network access only through the guarded `ctx` clients
- [ ] No secret can reach a metric, label, log line, error message or API response
- [ ] No real IPs, hostnames, domains, serials or credentials anywhere (code, tests, fixtures, docs, screenshots)
- [ ] No new dependency
- [ ] Tests added; `python scripts/check.py` passes
- [ ] For a new integration: the checklist in docs/INTEGRATIONS.md, section 16, is done, and `docs/integrations/<type_id>.md` is written

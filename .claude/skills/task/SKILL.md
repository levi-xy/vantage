---
name: task
description: Run one BUILD-PLAN.md task end to end - builder agent opens a PR, a fresh reviewer agent reviews it, one fix round, then a summary for the maintainer. Use when the maintainer says "/task M2.1" or "do task M2.1". Several IDs = run them in parallel worktrees.
argument-hint: <task ID> [more IDs to run in parallel]
disable-model-invocation: true
---

You coordinate. You don't write the task's code yourself. Task IDs: `$ARGUMENTS`

## 1. Check it can start

- `git fetch origin` and look up each ID in `BUILD-PLAN.md`. Unknown ID → stop and say so.
- Check the dependency map: everything the task needs must already be merged into `origin/main` (`gh pr list --state merged`). If not, stop and tell the maintainer what's missing.
- Check nothing is already open for it: `gh pr list --state open`. If a PR exists, go to step 3 with it instead of building again.
- Look up each task's tier in the "Model tiers" table in `BUILD-PLAN.md` (not listed = Standard, unless it touches secrets, auth, crypto, `net_guard` or an `AGENTS.md` contract: then Critical). Builder and reviewer models: Critical = opus / opus, Standard = sonnet / sonnet, Light = haiku / sonnet. Say each task's tier when you start it.

## 2. Build

Spawn the `builder` agent, one per ID, with the `model` parameter set to the task's builder model from its tier (this overrides the agent file's default). With more than one ID, use `isolation: "worktree"` for each and launch them in the same message so they run in parallel. One ID → no worktree needed.

Prompt (fill in the ID and paste the task's row from BUILD-PLAN.md, plus any "Carried over from reviews" notes for that task under its milestone):

```
Do task <ID> from BUILD-PLAN.md only.
Acceptance criteria: <paste the row>
Also required (carried over from earlier reviews): <paste the notes, or "none">
Follow AGENTS.md. Open the PR when scripts/check.py passes and report back.
```

When a review leaves non-blocking notes that belong to a later task, add them to that milestone's "Carried over from reviews" list in BUILD-PLAN.md in a small PR, so they reach every machine.

If the builder stops with a contract question, relay it to the maintainer word for word and stop.

## 3. Review

Wait for CI on the PR (`gh pr checks <N> --watch`). Then spawn a **fresh** `reviewer` agent (never the builder), with `model` set to the task's reviewer model from its tier (never weaker than the builder's):

```
Review PR #<N> for task <ID> in BUILD-PLAN.md.
Acceptance criteria: <paste the row>
```

## 4. One fix round

If the verdict is "request changes", send the blocking findings to the same builder with SendMessage, wait for its push and green CI, then spawn a new reviewer once more (same tier model). The fix round keeps the same builder, so it keeps the same model. After that, stop either way: anything still blocking goes to the maintainer.

## 5. Report to the maintainer

Keep it short:

```
<ID>: PR #<N> <link>: ready to merge | needs you
CI: green/red
Review: <verdict>, <blocking count> blocking, <n> non-blocking
Needs your decision: <contract questions, TODO(maintainer), disagreements> (or "nothing")
```

Never merge. Never approve on GitHub. The maintainer merges.

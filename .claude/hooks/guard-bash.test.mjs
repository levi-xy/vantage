import assert from "node:assert/strict";
import { test } from "node:test";

import { check } from "./guard-bash.mjs";

test("blocks rule-breaking commands", () => {
  for (const cmd of [
    "git commit --no-verify -m x",
    "git commit -n -m x",
    "git commit -anm x",
    "git -c commit.gpgsign=false commit -m x",
    "git config commit.gpgsign false",
    "git push --force origin m2/x",
    "git push -f origin m2/x",
    "git push --force-with-lease",
    "git push origin +m2/x",
    "git push origin main",
    'git push origin "main"',
    "git push origin 'main'",
    "git push origin HEAD:main",
    "git push origin HEAD:refs/heads/main",
    "git push origin m2/x && git push origin main",
    "cat deploy/secrets/master.key",
    "cat deploy//secrets/x",
    "cd deploy && cat secrets/master.key",
    "Get-Content deploy\\secrets\\prometheus_token",
    "cat .env",
    "cat deploy/.env",
    "cat .env.local",
    "cat deploy/.env.production",
    "cat <.env",
    "source ./.env && run",
  ]) {
    assert.notEqual(check(cmd), null, cmd);
  }
});

test("blocks a bare push while main is checked out", () => {
  assert.notEqual(check("git push", "main"), null);
  assert.notEqual(check("git push -u origin", "main"), null);
  assert.equal(check("git push", "m2/agent-tooling"), null);
});

test("allows normal work", () => {
  for (const cmd of [
    "git push -u origin m2/agent-tooling",
    "git push origin m2/main-menu",
    "git commit -m 'Fix main loop'",
    "git checkout main",
    "git log --oneline main",
    "cat deploy/.env.example",
    "cp deploy/.env.example /tmp/example",
    "cat deploy/secrets-notes.md",
    "ls .envrc",
    "python scripts/check.py --fast",
    "gh pr create --base main --title x --body y",
  ]) {
    assert.equal(check(cmd), null, cmd);
  }
});

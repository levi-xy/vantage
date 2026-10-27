import assert from "node:assert/strict";
import { test } from "node:test";

import { check } from "./guard-bash.mjs";

test("blocks rule-breaking commands", () => {
  for (const cmd of [
    "git commit --no-verify -m x",
    "git -c commit.gpgsign=false commit -m x",
    "git push --force origin m2/x",
    "git push -f origin m2/x",
    "git push --force-with-lease",
    "git push origin +m2/x",
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin m2/x && git push origin main",
    "cat deploy/secrets/master.key",
    "Get-Content deploy\\secrets\\prometheus_token",
    "cat .env",
    "cat deploy/.env",
    "source ./.env && run",
  ]) {
    assert.notEqual(check(cmd), null, cmd);
  }
});

test("allows normal work", () => {
  for (const cmd of [
    "git push -u origin m2/agent-tooling",
    "git push origin m2/main-menu",
    "git commit -m 'Fix main loop'",
    "git checkout main",
    "git log --oneline main",
    "cat deploy/.env.example",
    "python scripts/check.py --fast",
    "gh pr create --base main --title x --body y",
    "ls .envrc-notes",
  ]) {
    assert.equal(check(cmd), null, cmd);
  }
});

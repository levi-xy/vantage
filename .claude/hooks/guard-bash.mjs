// PreToolUse hook for Bash/PowerShell: blocks commands that break AGENTS.md rules.
// Exit code 2 blocks the call and shows the reason to the agent.
//
// This is a speed bump against mistakes, not a security boundary: a determined
// command (globs, `cd` first, `python -c`) can get around regexes. The real
// controls are the deny rules in .claude/settings.json, branch protection on main,
// and review.

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const GIT_PUSH = /\bgit\b[^;&|\n]*\bpush\b/;

const RULES = [
  {
    test: /--no-verify\b|\bgit\b[^;&|\n]*\bcommit\b[^;&|\n]*\s-[a-zA-Z]*n[a-zA-Z]*\b/,
    reason: "Don't skip git hooks (--no-verify / -n). Fix what the hook reports instead.",
  },
  {
    test: /--no-gpg-sign\b|commit\.gpgsign(\s*=\s*|\s+)false/,
    reason: "Don't bypass commit signing.",
  },
  {
    test: /\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*(\s--force(-with-lease)?\b|\s-[a-zA-Z]*f\b|\s['"]?\+\S)/,
    reason: "No force-push. Add a new commit instead.",
  },
  {
    test: /\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*(\s|:)['"]?(refs\/heads\/)?main['"]?(\s|$|[;&|])/,
    reason: "Never push to main. Push your m<N>/<task> branch and open a PR.",
  },
  {
    test: /deploy[\\/]+secrets(?![\w-])|\bsecrets[\\/]+(master\.key|prometheus_token)\b/,
    reason: "deploy/secrets/ holds real secrets. Agents never read or write it.",
  },
  {
    // Any .env file except .env.example: .env, .env.local, deploy/.env.production, <.env
    test: /(^|[\s'"=\\/<(])\.env(?!\.example\b)(?![\w-])/,
    reason: ".env files may hold real values. Use deploy/.env.example instead.",
  },
];

/**
 * Returns the reason a command is blocked, or null if it's allowed.
 * `branch` is the checked-out branch, so a bare `git push` on main is caught too.
 */
export function check(command, branch = null) {
  for (const rule of RULES) {
    if (rule.test.test(command)) return rule.reason;
  }
  if (branch === "main" && GIT_PUSH.test(command)) {
    return "You're on main. Create an m<N>/<task> branch, push that, and open a PR.";
  }
  return null;
}

function currentBranch(cwd) {
  try {
    return execFileSync("git", ["rev-parse", "--abbrev-ref", "HEAD"], {
      cwd,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return null;
  }
}

function main() {
  let input;
  try {
    input = JSON.parse(readFileSync(0, "utf8"));
  } catch {
    return 0; // not our input shape; don't block
  }
  const command = input?.tool_input?.command;
  if (typeof command !== "string") return 0;
  const branch = GIT_PUSH.test(command) ? currentBranch(input.cwd || process.cwd()) : null;
  const reason = check(command, branch);
  if (reason === null) return 0;
  process.stderr.write(`Blocked by .claude/hooks/guard-bash.mjs: ${reason}\n`);
  return 2;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  process.exit(main());
}

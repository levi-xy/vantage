// PreToolUse hook for Bash/PowerShell: blocks commands that break AGENTS.md rules.
// Exit code 2 blocks the call and shows the reason to the agent.

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const RULES = [
  {
    test: /--no-verify\b/,
    reason: "Don't skip git hooks (--no-verify). Fix what the hook reports instead.",
  },
  {
    test: /--no-gpg-sign\b|commit\.gpgsign\s*=\s*false/,
    reason: "Don't bypass commit signing.",
  },
  {
    test: /\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*(\s--force(-with-lease)?\b|\s-[a-zA-Z]*f\b|\s\+\S)/,
    reason: "No force-push. Add a new commit instead.",
  },
  {
    test: /\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*(\s|:)(refs\/heads\/)?main(\s|$|[;&|])/,
    reason: "Never push to main. Push your m<N>/<task> branch and open a PR.",
  },
  {
    test: /deploy[\\/]secrets\b/,
    reason: "deploy/secrets/ holds real secrets. Agents never read or write it.",
  },
  {
    test: /(^|[\s'"=\\/])\.env(?![\w.-])/,
    reason: ".env may hold real values. Use deploy/.env.example instead.",
  },
];

/** Returns the reason a command is blocked, or null if it's allowed. */
export function check(command) {
  for (const rule of RULES) {
    if (rule.test.test(command)) return rule.reason;
  }
  return null;
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
  const reason = check(command);
  if (reason === null) return 0;
  process.stderr.write(`Blocked by .claude/hooks/guard-bash.mjs: ${reason}\n`);
  return 2;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  process.exit(main());
}

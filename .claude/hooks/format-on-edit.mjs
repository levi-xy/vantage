// PostToolUse hook for Edit/Write: formats the edited file with the repo's own
// formatter (ruff for backend/, Prettier for frontend/). Never blocks: if a tool
// isn't installed yet, it does nothing.

import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";

const PRETTIER_EXT = new Set([".ts", ".tsx", ".js", ".mjs", ".json", ".css", ".html", ".md", ".yml", ".yaml"]);

function main() {
  let input;
  try {
    input = JSON.parse(readFileSync(0, "utf8"));
  } catch {
    return;
  }
  const file = input?.tool_input?.file_path;
  if (typeof file !== "string" || !existsSync(file)) return;

  const root = process.env.CLAUDE_PROJECT_DIR || input.cwd || process.cwd();
  const rel = path.relative(root, file).split(path.sep).join("/");
  if (rel.startsWith("..")) return;

  if (rel.startsWith("backend/") && rel.endsWith(".py")) {
    const backend = path.join(root, "backend");
    const ruff = ["Scripts/ruff.exe", "bin/ruff"]
      .map((p) => path.join(backend, ".venv", p))
      .find(existsSync);
    if (ruff) spawnSync(ruff, ["format", "--quiet", file], { cwd: backend });
  } else if (rel.startsWith("frontend/") && PRETTIER_EXT.has(path.extname(rel))) {
    const frontend = path.join(root, "frontend");
    const prettier = path.join(frontend, "node_modules", "prettier", "bin", "prettier.cjs");
    if (existsSync(prettier)) {
      spawnSync(process.execPath, [prettier, "--write", "--log-level=silent", file], { cwd: frontend });
    }
  }
}

main();

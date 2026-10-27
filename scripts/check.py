"""Run the same checks CI runs, on macOS, Linux or Windows.

    python scripts/check.py              # everything
    python scripts/check.py backend      # one side only
    python scripts/check.py --fast       # skip the slow audits (pip-audit, npm audit)

Any Python 3.8+ can run this file; backend steps use backend/.venv
(create it with scripts/dev_setup.py). Exits non-zero if any step fails.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"


def venv_python() -> Path | None:
    for sub in ("Scripts/python.exe", "bin/python"):
        candidate = BACKEND / ".venv" / sub
        if candidate.exists():
            return candidate
    return None


def backend_steps(fast: bool) -> list[tuple[str, list[str]]] | str:
    py = venv_python()
    if py is None:
        return "backend/.venv missing: run scripts/dev_setup.py with Python 3.12+"
    p = str(py)
    steps = [
        ("ruff check", [p, "-m", "ruff", "check", "."]),
        ("ruff format", [p, "-m", "ruff", "format", "--check", "."]),
        ("mypy", [p, "-m", "mypy"]),
        ("pytest", [p, "-m", "pytest", "-q"]),
        ("bandit", [p, "-m", "bandit", "-q", "-c", "pyproject.toml", "-r", "app"]),
    ]
    if not fast:
        steps.append(("pip-audit", [p, "-m", "pip_audit", "--skip-editable"]))
    return steps


def frontend_steps(fast: bool) -> list[tuple[str, list[str]]] | str:
    npm = shutil.which("npm")
    if npm is None:
        return "npm not found: install Node 22.12+"
    if not (FRONTEND / "node_modules").exists():
        return "frontend/node_modules missing: run scripts/dev_setup.py (or npm ci)"
    steps = [
        ("eslint", [npm, "run", "--silent", "lint"]),
        ("prettier", [npm, "run", "--silent", "format:check"]),
        ("tsc", [npm, "run", "--silent", "typecheck"]),
        ("vitest", [npm, "test", "--silent"]),
        ("hook tests", ["node", "--test", "../.claude/hooks/*.test.mjs"]),
    ]
    if not fast:
        steps.append(("npm audit", [npm, "audit", "--audit-level=high"]))
    return steps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("side", nargs="?", choices=["backend", "frontend"], default=None)
    parser.add_argument("--fast", action="store_true", help="skip pip-audit and npm audit")
    args = parser.parse_args()

    plan: list[tuple[str, Path, list[tuple[str, list[str]]] | str]] = []
    if args.side in (None, "backend"):
        plan.append(("backend", BACKEND, backend_steps(args.fast)))
    if args.side in (None, "frontend"):
        plan.append(("frontend", FRONTEND, frontend_steps(args.fast)))

    failed: list[str] = []
    env = {**os.environ, "PYTHONUTF8": "1"}
    for side, cwd, steps in plan:
        if isinstance(steps, str):
            print(f"[{side}] SKIPPED: {steps}")
            failed.append(f"{side} (not set up)")
            continue
        for name, cmd in steps:
            print(f"[{side}] {name} ...", flush=True)
            result = subprocess.run(cmd, cwd=cwd, env=env, check=False)  # noqa: S603 - fixed argv
            if result.returncode != 0:
                failed.append(f"{side}: {name}")

    if shutil.which("gitleaks"):
        print("[repo] gitleaks ...", flush=True)
        cmd = ["gitleaks", "git", "--redact", "--no-banner", "."]
        if subprocess.run(cmd, cwd=ROOT, check=False).returncode != 0:  # noqa: S603
            failed.append("repo: gitleaks")
    else:
        print("[repo] gitleaks not installed locally; CI still runs it.")

    print()
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

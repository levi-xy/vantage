"""Set up a dev environment on macOS, Linux or Windows.

Run it with any Python 3.12+ interpreter:

    python3.12 scripts/dev_setup.py      # macOS / Linux
    py -3.12 scripts/dev_setup.py        # Windows (any 3.12+ works, e.g. py -3.13)

Creates backend/.venv, installs the backend with dev extras, runs `npm ci` in
frontend/, and installs the pre-commit hooks if pre-commit is available.
Safe to re-run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / "backend" / ".venv"


def venv_python() -> Path:
    sub = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    return VENV / sub


def run(cmd: list[str], cwd: Path) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)  # noqa: S603 - fixed argv, no shell


def main() -> int:
    if sys.version_info < (3, 12):  # noqa: UP036 - runs before the venv exists
        print(f"Python 3.12+ required, this is {sys.version.split()[0]}.", file=sys.stderr)
        print("Windows: py -3.12 scripts/dev_setup.py   macOS: python3.12 ...", file=sys.stderr)
        return 1

    if not venv_python().exists():
        print(f"Creating {VENV.relative_to(ROOT)}")
        venv.create(VENV, with_pip=True)
    py = str(venv_python())
    probe = [py, "-c", "import sys; print(sys.version_info >= (3, 12))"]
    if subprocess.run(probe, capture_output=True, text=True, check=False).stdout.strip() != "True":  # noqa: S603
        print(f"{VENV.relative_to(ROOT)} was made with Python < 3.12.", file=sys.stderr)
        print("Delete it and re-run this script with Python 3.12+.", file=sys.stderr)
        return 1
    run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], ROOT / "backend")
    run([py, "-m", "pip", "install", "-q", "-e", ".[dev]"], ROOT / "backend")

    npm = shutil.which("npm")
    if npm is None:
        print("npm not found: install Node 22.12+ for the frontend.", file=sys.stderr)
    else:
        run([npm, "ci", "--no-fund", "--no-audit"], ROOT / "frontend")

    pre_commit = shutil.which("pre-commit")
    if pre_commit is None:
        print("pre-commit not found: hooks not installed (pipx install pre-commit).")
    else:
        run([pre_commit, "install"], ROOT)

    print("\nDone. Run all CI checks with: python scripts/check.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

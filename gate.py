#!/usr/bin/env python3
"""Quality gate. Nothing is "done", deployed or handed over without `GATE: PASS`.

Steps: compile (3.11 syntax), lint (ruff), types (mypy --strict), tests
(unittest, offline), profiles (every profile in profiles/ loads and its
examples pass). ruff and mypy run via `uvx` when not installed.

    python3 gate.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def tool(name: str) -> list[str] | None:
    if shutil.which(name):
        return [name]
    if shutil.which("uvx"):
        return ["uvx", name]
    try:
        __import__(name)
        return [sys.executable, "-m", name]
    except ImportError:
        return None


def run(label: str, cmd: list[str] | None) -> bool:
    print(f"\n=== {label}")
    if cmd is None:
        print("missing tool — install uv (https://docs.astral.sh/uv/) or the tool itself")
        return False
    proc = subprocess.run(cmd, cwd=ROOT)
    print(f"--- {label}: {'ok' if proc.returncode == 0 else 'FAILED'}")
    return proc.returncode == 0


def main() -> int:
    py = [sys.executable]
    ruff, mypy = tool("ruff"), tool("mypy")
    check_profiles = ("import sys, pathlib; from grizly.cli import main; "
                      "sys.exit(max(main(['profile', '--profile', str(p)]) "
                      "for p in sorted(pathlib.Path('profiles').glob('*.toml'))))")
    steps = [
        ("compile", [*py, "-m", "compileall", "-q", "grizly", "grizly.py", "gate.py", "tests"]),
        ("lint", [*ruff, "check", "."] if ruff else None),
        ("types", mypy),
        ("tests", [*py, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"]),
        ("profiles", [*py, "-c", check_profiles]),
    ]
    results = [run(label, cmd) for label, cmd in steps]
    passed = all(results)
    print("\n" + ("GATE: PASS" if passed else "GATE: FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

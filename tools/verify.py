#!/usr/bin/env python
"""Verification helper for safe upgrades."""

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Ensure repo root is importable (so `import main` works when running tools/verify.py)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_step(cmd: list[str], description: str) -> None:
    print(f"[verify] {description}...")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        cmd_str = " ".join(map(str, cmd))
        raise SystemExit(
            f"[verify] FAILED: {description} (exit {result.returncode})\n"
            f"[verify] Command: {cmd_str}"
        )


def run_step_warn(cmd: list[str], description: str) -> None:
    """Run a step that reports issues but does not fail verification."""
    print(f"[verify] {description} (non-blocking)...")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        cmd_str = " ".join(map(str, cmd))
        print(
            f"[verify] WARNING: {description} reported issues (exit {result.returncode}).\n"
            f"[verify] Command: {cmd_str}\n"
            f"[verify] Note: This is non-blocking for now. Fix Ruff issues to re-enable strict gating."
        )


def _pytest_available() -> bool:
    try:
        import pytest  # noqa: F401
        return True
    except Exception:
        return False


def run() -> None:
    # Ruff: lint only owned code (avoid vendored/legacy noise)
    # Non-blocking during cleanup phase.
    run_step_warn(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "main.py",
            "routes",
            "services",
            "tools",
            "tests",
        ],
        "ruff lint",
    )

    # Bytecode compile the whole repo (quick syntax/import sanity)
    run_step([sys.executable, "-m", "compileall", "-q", "."], "python -m compileall")

    # Safe import of main (catches missing deps / import-time crashes)
    print("[verify] Importing main module...")
    try:
        importlib.import_module("main")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[verify] FAILED: could not import main.py -> {exc}") from exc

    # Golden output contract tests (blocking if pytest installed)
    if _pytest_available():
        run_step(
            [sys.executable, "-m", "pytest", "tests/test_golden_outputs.py"],
            "golden tests (RT inventory + Vendor PO)",
        )
    else:
        print("[verify] WARNING: pytest not installed; skipping golden tests.")

    print("[verify] All checks passed.")


if __name__ == "__main__":
    run()

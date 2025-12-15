#!/usr/bin/env python
"""Verification helper for safe upgrades."""
import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_step(cmd, description):
    print(f"[verify] {description}...")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"[verify] FAILED: {description} (exit {result.returncode})")


def run():
    run_step(["ruff", "check", "."], "ruff lint")
    run_step([sys.executable, "-m", "compileall", "-q", "."], "python -m compileall")
    print("[verify] Importing main module...")
    try:
        importlib.import_module("main")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[verify] FAILED: could not import main.py -> {exc}")
    print("[verify] All checks passed.")


if __name__ == "__main__":
    run()

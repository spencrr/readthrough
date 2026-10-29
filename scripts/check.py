"""Run every quality gate the way a reviewer would: lint, format, types, then tests with the coverage floor.

    uv run python scripts/check.py          # everything
    uv run python scripts/check.py --fix    # let ruff fix and format first
    uv run python scripts/check.py --quick  # skip the browser tests (what the pre-commit hook runs)

All gates run even when an early one fails, so one pass shows everything to fix. Exits non-zero if any failed.
The coverage floor lives in pyproject.toml ([tool.coverage.report] fail_under).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def gates(*, fix: bool, quick: bool) -> list[tuple[str, list[str]]]:
    py = [sys.executable, "-m"]
    tests = [*py, "pytest", "-q", "--no-header", "--cov", "--cov-report=term-missing:skip-covered"]
    if quick:
        tests += ["-m", "not browser"]
    return [
        ("lint", [*py, "ruff", "check", ".", *(["--fix"] if fix else [])]),
        ("format", [*py, "ruff", "format", *([] if fix else ["--check"]), "."]),
        ("types", [*py, "ty", "check"]),
        ("tests", tests),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fix", action="store_true", help="apply ruff fixes and formatting before checking")
    parser.add_argument("--quick", action="store_true", help="skip tests marked 'browser'")
    args = parser.parse_args()

    results: list[tuple[str, bool, float]] = []
    for name, cmd in gates(fix=args.fix, quick=args.quick):
        print(f"\n== {name}: {' '.join(cmd[2:])}", flush=True)
        started = time.monotonic()
        ok = subprocess.call(cmd, cwd=ROOT) == 0
        results.append((name, ok, time.monotonic() - started))

    print("\n== summary")
    for name, ok, took in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name:7} {took:5.1f}s")
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"\nFailed: {', '.join(failed)}. Fix them (or run with --fix for lint/format) and re-run.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

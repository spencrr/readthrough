"""CI status per PR: which checks fail, whether they're required, and whether main fails them too.

A rollup state alone ("FAILURE") is too blunt: a PR can fail an optional check, fail a check that's
broken on main for everyone, or not have run CI at all (fork PRs from first-time contributors wait for
a maintainer to approve workflows). Each of those calls for a different response. And when the forge
couldn't say what the checks are, the answer is "unknown", never "passing" or "no CI".
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from typing import Any

from readthrough.model import Check

FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
PENDING = {"QUEUED", "IN_PROGRESS", "PENDING", "EXPECTED", "WAITING", "REQUESTED"}

# Sort order: most in need of attention first.
STATES = ("failing", "not-run", "failing-optional", "failing-main", "unknown", "pending", "passing", "none")
STATE_RANK = {s: i for i, s in enumerate(STATES)}
STATE_LABELS = {
    "failing": "failing",
    "not-run": "not run",
    "failing-optional": "optional check failing",
    "failing-main": "failing on main too",
    "unknown": "couldn't be read",
    "pending": "pending",
    "passing": "passing",
    "none": "no CI",
}


def latest_checks(checks: list[Check]) -> dict[str, dict[str, Any]]:
    """Collapse re-runs: keep the most recent result for each check name."""
    latest: dict[str, dict[str, Any]] = {}
    for check in checks:
        when = check.started or check.completed
        current = latest.get(check.name)
        if current is None or (when or "") >= (current["when"] or ""):
            outcome = (check.conclusion or check.status or "").upper()
            latest[check.name] = {"name": check.name, "outcome": outcome, "url": check.url, "when": when}
    return latest


def failing_names(checks: list[Check]) -> set[str]:
    return {c["name"] for c in latest_checks(checks).values() if c["outcome"] in FAILED}


def unknown(reason: str) -> dict[str, Any]:
    """The summary for a PR whose checks the forge couldn't supply."""
    return {
        "state": "unknown", "reason": reason, "failing": [], "pending": 0, "cancelled": 0, "passed": 0, "total": 0,
        "required_missing": [], "required_known": False,
    }  # fmt: skip


def classify(
    contexts: list[Check], *, required: AbstractSet[str] | None, main_failing: AbstractSet[str]
) -> dict[str, Any]:
    """Summarize one PR's checks into a state plus the details the dashboard shows."""
    checks = latest_checks(contexts)
    required = required or set()
    failing = [c for c in checks.values() if c["outcome"] in FAILED]
    pending = [c for c in checks.values() if c["outcome"] in PENDING]
    cancelled = [c for c in checks.values() if c["outcome"] == "CANCELLED"]
    missing = sorted(required - set(checks))

    failed_rows = sorted(
        (
            {
                "name": c["name"],
                "url": c["url"],
                "required": c["name"] in required,
                "on_main": c["name"] in main_failing,
            }
            for c in failing
        ),
        key=lambda r: (not r["required"], r["on_main"], r["name"]),
    )
    own = [r for r in failed_rows if not r["on_main"]]

    if any(r["required"] for r in own) or (own and not required):
        state = "failing"
    elif own:
        state = "failing-optional"
    elif failed_rows:
        state = "failing-main"
    elif required and len(missing) == len(required) and not pending:
        state = "not-run"
    elif pending or missing or cancelled:
        state = "pending"
    elif checks:
        state = "passing"
    else:
        state = "none"

    return {
        "state": state,
        "failing": failed_rows,
        "pending": len(pending),
        "cancelled": len(cancelled),
        "passed": sum(1 for c in checks.values() if c["outcome"] in ("SUCCESS", "NEUTRAL", "SKIPPED")),
        "total": len(checks),
        "required_missing": missing[:20],
        "required_known": bool(required),
    }

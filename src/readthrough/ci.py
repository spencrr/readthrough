"""CI status per PR: which checks fail, whether they're required, and whether main fails them too.

A rollup state alone ("FAILURE") is too blunt: a PR can fail an optional check, fail a check that's
broken on main for everyone, or not have run CI at all (fork PRs from first-time contributors wait for
a maintainer to approve workflows). Each of those calls for a different response. And when the forge
couldn't say what the checks are, the answer is "unknown", never "passing" or "no CI".
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from enum import StrEnum
from typing import Any

from readthrough.contract import CiChecks, FailingCheck
from readthrough.model import Check

FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
PENDING = {"QUEUED", "IN_PROGRESS", "PENDING", "EXPECTED", "WAITING", "REQUESTED"}


class State(StrEnum):
    """One PR's CI, as the dashboard files it (the page groups and orders these itself)."""

    FAILING = "failing"
    NOT_RUN = "not-run"
    FAILING_OPTIONAL = "failing-optional"
    FAILING_MAIN = "failing-main"
    UNKNOWN = "unknown"
    PENDING = "pending"
    PASSING = "passing"
    NONE = "none"


LABELS = {
    State.FAILING: "failing",
    State.NOT_RUN: "not run",
    State.FAILING_OPTIONAL: "optional check failing",
    State.FAILING_MAIN: "failing on main too",
    State.UNKNOWN: "couldn't be read",
    State.PENDING: "pending",
    State.PASSING: "passing",
    State.NONE: "no CI",
}


def label(state: object) -> str:
    """The words for a state, as the brief prints it; one read back from a file is a plain string, and anything
    unrecognised reads as unreadable, never as passing."""
    try:
        return LABELS[State(state)]
    except ValueError:
        return LABELS[State.UNKNOWN]


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


def unknown(reason: str) -> CiChecks:
    """The summary for a PR whose checks the forge couldn't supply."""
    return {
        "state": State.UNKNOWN, "reason": reason, "failing": [], "pending": 0, "cancelled": 0, "passed": 0, "total": 0,
        "required_missing": [], "required_known": False,
    }  # fmt: skip


def classify(contexts: list[Check], *, required: AbstractSet[str] | None, main_failing: AbstractSet[str]) -> CiChecks:
    """Summarize one PR's checks into a state plus the details the dashboard shows."""
    checks = latest_checks(contexts)
    required = required or set()
    failing = [c for c in checks.values() if c["outcome"] in FAILED]
    pending = [c for c in checks.values() if c["outcome"] in PENDING]
    cancelled = [c for c in checks.values() if c["outcome"] == "CANCELLED"]
    missing = sorted(required - set(checks))

    failed_rows: list[FailingCheck] = sorted(
        (
            FailingCheck(
                name=c["name"], url=c["url"], required=c["name"] in required, on_main=c["name"] in main_failing
            )
            for c in failing
        ),
        key=lambda r: (not r["required"], r["on_main"], r["name"]),
    )
    own = [r for r in failed_rows if not r["on_main"]]

    if any(r["required"] for r in own) or (own and not required):
        state = State.FAILING
    elif own:
        state = State.FAILING_OPTIONAL
    elif failed_rows:
        state = State.FAILING_MAIN
    elif required and len(missing) == len(required) and not pending:
        state = State.NOT_RUN
    elif pending or missing or cancelled:
        state = State.PENDING
    elif checks:
        state = State.PASSING
    else:
        state = State.NONE

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

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from readthrough.profile import load_profile

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)
FIXTURES = Path(__file__).parent / "fixtures"
PROFILE_PATH = FIXTURES / "courier.toml"
PROFILE = load_profile(PROFILE_PATH)
REPO = PROFILE.repo


@pytest.fixture(autouse=True)
def no_real_programs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `gh` and `git` at a program that doesn't exist, so a test that forgets to fake them fails the same way
    everywhere instead of passing on a machine that happens to be signed in."""
    monkeypatch.setenv("READTHROUGH_GH", "readthrough-tests-must-not-run-gh")
    monkeypatch.setenv("READTHROUGH_GIT", "readthrough-tests-must-not-run-git")


def _base_pr() -> dict[str, Any]:
    return {
        "number": 100,
        "title": "FIX: something",
        "url": f"https://github.com/{REPO}/pull/100",
        "body": "Fixes it.",
        "isDraft": False,
        "createdAt": "2026-09-30T12:00:00Z",
        "updatedAt": "2026-10-01T12:00:00Z",
        "additions": 40,
        "deletions": 5,
        "changedFiles": 2,
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "reviewDecision": "REVIEW_REQUIRED",
        "mergeable": "MERGEABLE",
        "author": {"login": "someone"},
        "labels": {"nodes": []},
        "files": {
            "nodes": [
                {"path": "courier/transports/smtp.py", "additions": 30, "deletions": 5, "changeType": "MODIFIED"},
                {
                    "path": "tests/unit/transports/test_smtp.py",
                    "additions": 10,
                    "deletions": 0,
                    "changeType": "MODIFIED",
                },
            ]
        },
        "reviewRequests": {"nodes": []},
        "latestReviews": {"nodes": []},
        "reviewThreads": {"nodes": []},
        "comments": {"totalCount": 0},
        "commits": {
            "nodes": [{"commit": {"committedDate": "2026-10-01T12:00:00Z", "statusCheckRollup": {"state": "SUCCESS"}}}]
        },
        "closingIssuesReferences": {"nodes": []},
    }


@pytest.fixture
def make_pr():
    def factory(**overrides: Any) -> dict[str, Any]:
        pr = copy.deepcopy(_base_pr())
        pr.update(overrides)
        return pr

    return factory

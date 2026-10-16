from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeForge

from readthrough.forges.github import normalize
from readthrough.model import PullRequest
from readthrough.profile import load_profile

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)
FIXTURES = Path(__file__).parent / "fixtures"
PROFILE_PATH = FIXTURES / "courier.toml"
PROFILE = load_profile(PROFILE_PATH)
REPO = PROFILE.repo
SLUG = f"github/{REPO}"


@pytest.fixture(autouse=True)
def no_real_programs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point `gh`, `git`, and the other CLIs at programs that don't exist, so a test that forgets to fake them fails
    the same way everywhere instead of passing on a machine that happens to be signed in.

    The review agents count as installed (their executables are named, if nonexistent) and their session stores
    are empty temporary directories, so the tests see the same machine wherever they run.
    """
    for program in ("gh", "git", "glab", "az", "copilot", "claude"):
        monkeypatch.setenv(f"READTHROUGH_{program.upper()}", f"readthrough-tests-must-not-run-{program}")
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


@pytest.fixture
def fake_forge(monkeypatch: pytest.MonkeyPatch) -> type[FakeForge]:
    """Every ``forges.make(cfg)`` returns a FakeForge for that repo; the class holds the data and records calls."""
    FakeForge.reset()
    monkeypatch.setattr(
        "readthrough.forges.make",
        lambda cfg: FakeForge(cfg.repo, None, cfg.tuning.forge, account=getattr(cfg, "account", None)),
    )
    return FakeForge


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
    """A PR as GitHub's GraphQL returns it (what the fake forge and the normalizer consume)."""

    def factory(**overrides: Any) -> dict[str, Any]:
        pr = copy.deepcopy(_base_pr())
        pr.update(overrides)
        return pr

    return factory


@pytest.fixture
def make_pull(make_pr):
    """The same PR as the neutral record the analysis consumes."""

    def factory(**overrides: Any) -> PullRequest:
        return normalize(make_pr(**overrides))

    return factory

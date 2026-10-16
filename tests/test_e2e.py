"""End to end through the real entry point: ``python -m readthrough`` as a subprocess, with ``gh`` faked.

Onboard a repo, build its dashboard, read what came out, then build again and check the diff cache was used.
Nothing here touches the network, Copilot, git, or the OS integrations. The fake stands in at the transport,
the ``gh`` process boundary, through ``READTHROUGH_GH``; everything above it is the real thing.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import fake_gh
import pytest

pytestmark = pytest.mark.e2e


class Cli:
    """Runs ``readthrough`` with an isolated home, the fake gh, and a log of every gh call."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.log = tmp_path / "gh-calls"
        self.env = {
            **os.environ,
            "READTHROUGH_GH": fake_gh.command(),
            "READTHROUGH_HOME": str(self.home),
            "READTHROUGH_FAKE_GH_LOG": str(self.log),
            "COPILOT_HOME": str(tmp_path / "copilot"),
            "PYTHONUTF8": "1",
        }
        self.env.pop("READTHROUGH_CONFIG", None)

    def __call__(self, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, "-m", "readthrough", "--home", str(self.home), *args],
            capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120, check=False,
        )  # fmt: skip
        assert proc.returncode == expect, (
            f"exit {proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}"
        )
        return proc

    def gh_calls(self) -> list[list[str]]:
        return fake_gh.calls(self.log)

    def reset_calls(self) -> None:
        shutil.rmtree(self.log, ignore_errors=True)


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    return Cli(tmp_path)


def test_version_and_help_need_nothing(cli):
    assert cli("--version").stdout.strip().startswith("readthrough ")
    assert "onboard" in cli("--help").stdout


def test_onboard_build_study_and_cache_hit(cli):
    repo = fake_gh.REPO
    home: Path = cli.home

    # Onboarding drafts a profile from the fake repo's tree and merged PRs, and the draft must cover enough churn.
    out = cli("onboard", repo, "--clone", str(home / "nowhere"), "--accept")
    assert f"{repo} is onboarded" in out.stdout
    assert "Coverage:" in out.stdout
    profile = home / "repos" / "github" / "octo" / "courier" / "profile.toml"
    assert profile.exists() and "repo = 'octo/courier'" in profile.read_text(encoding="utf-8").replace('"', "'")

    cli.reset_calls()
    out = cli("build")
    assert f"{repo}: 2 open PRs" in out.stderr and "Dashboard:" in out.stderr
    data = json.loads((home / "repos" / "github" / "octo" / "courier" / "data.json").read_text(encoding="utf-8"))
    assert data["schema"] == 2 and data["repo"] == repo and data["viewer"] == fake_gh.VIEWER
    assert data["slug"] == f"github/{repo}" and data["forge"]["label"] == "GitHub"
    by_number = {p["number"]: p for p in data["prs"]}
    assert set(by_number) == {41, 42}
    assert by_number[42]["lane"] == "mine" and by_number[41]["lane"] == "ready"
    assert any(f["id"] == "requested" for f in by_number[41]["flags"]), "the viewer's review was requested"
    assert data["stats"]["diffs_downloaded"] == 2 and data["stats"]["rate_limit"] == {"rest": 4990, "graphql": 4980}
    assert by_number[41]["smells"] == [], "a drafted profile carries no line heuristics yet"
    html = (home / "repos" / "github" / "octo" / "courier" / "index.html").read_text(encoding="utf-8")
    assert "<title>" in html and "Content-Security-Policy" in html and "LUCIDE" in html
    brief = (home / "repos" / "github" / "octo" / "courier" / "briefs" / "pr-41.md").read_text(encoding="utf-8")
    assert brief.startswith("# #41 ") and "## 0. Predict before you read" in brief
    diff_calls = [c for c in cli.gh_calls() if c[:2] == ["pr", "diff"]]
    assert sorted(c[2] for c in diff_calls) == ["41", "42"]

    # Studying from the terminal appends to the log and the brief, with no assistant involved.
    cli("study", "41", "--prediction", "Adds retries to the SMTP transport", "--finding", "major: swallows errors",
        "--at", "courier/transports/smtp.py:15")  # fmt: skip
    out = cli("study", "41")
    assert "Adds retries" in out.stdout and "swallows errors" in out.stdout
    brief = (home / "repos" / "github" / "octo" / "courier" / "briefs" / "pr-41.md").read_text(encoding="utf-8")
    assert "Study log (evidence of understanding)" in brief and "swallows errors" in brief

    # A second build reuses the cached diffs: the heads didn't change, so gh isn't asked for them again.
    cli.reset_calls()
    cli("build")
    assert [c for c in cli.gh_calls() if c[:2] == ["pr", "diff"]] == []
    assert data["prs"][0]["number"] in by_number

    out = cli("doctor", expect=1)  # the clone is missing and nothing is installed, so doctor reports problems
    assert "assistant" in out.stdout and "terminal" in out.stdout and "profile" in out.stdout
    assert cli("profile", "sources").stdout.startswith("-> local")
    assert "valid (1 repos)" in cli("config", "--check").stdout


def test_unknown_config_key_fails_with_a_suggestion(cli):
    home: Path = cli.home
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text('terminal = "wtt"\n', encoding="utf-8")
    out = cli("config", "--check", expect=1)
    assert "did you mean 'wt'" in out.stderr and "Traceback" not in out.stderr

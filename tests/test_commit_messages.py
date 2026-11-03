"""The commit-subject check CI runs on every pull request, and the grammar it enforces."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import commit_messages

CONFORMING = (
    "feat: show the dashboard beside the session",
    "feat(canvas): show the dashboard beside the session",
    "fix(serve)!: refuse a focus for a PR that isn't open",
    "docs: say what the rulesets guarantee",
    "build(deps): bump the python group with 3 updates",
    "chore(deps-dev): bump ruff from 0.17.2 to 0.17.3",
    "ci: pin the toolchain that builds a release",
    "refactor(forges/ado): read the PR list through Rest.call",
    "fix: handle HTTP 429 from GitLab",
    "build(deps): Bump the python group across 1 directory with 3 updates",  # Dependabot capitalizes; the spec allows it
    'Revert "feat: show the dashboard beside the session"',
    "Merge branch 'main' into dev/spencrr/canvas",
)


@pytest.mark.parametrize("subject", CONFORMING)
def test_conforming_subjects_pass(subject):
    assert commit_messages.problems(subject) == []


@pytest.mark.parametrize(
    ("subject", "complaint"),
    [
        ("Show the dashboard beside the session", "not `type(scope): description`"),
        ("feat show the dashboard", "not `type(scope): description`"),
        ("feat:show the dashboard", "not `type(scope): description`"),
        ("style: reformat", "unknown type `style`"),
        ("Feat: show the dashboard", "not `type(scope): description`"),  # the type is lowercase
        ("feat(Canvas): show the dashboard", "not `type(scope): description`"),  # so is the scope
    ],
)
def test_nonconforming_subjects_are_told_what_is_wrong(subject, complaint):
    found = commit_messages.problems(subject)
    assert any(complaint in item for item in found), found


def test_the_types_are_what_the_contributing_guide_lists():
    text = (Path(__file__).resolve().parents[1] / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for kind in commit_messages.TYPES:
        assert f"`{kind}`" in text, f"CONTRIBUTING.md doesn't list `{kind}`"


def test_the_command_line_reports_each_failure_and_exits_nonzero(capsys):
    assert commit_messages.main(["--title", "feat: add a thing"]) == 0
    assert "1 subject(s) follow" in capsys.readouterr().out
    assert commit_messages.main(["--title", "Add a thing."]) == 1
    out = capsys.readouterr().out
    assert "the pull request title: 'Add a thing.'" in out and "see CONTRIBUTING.md" in out


def test_a_range_names_each_offending_commit(capsys):
    """The range mode reads `git log` in the shape `git_log` asks for; here the log is handed in, since the tests
    never run git. Only the offending commits are reported, by short sha."""
    asked: list[str] = []

    def log(rev_range: str) -> str:
        asked.append(rev_range)
        return "a1b2c3d\0feat: begin\nd4e5f6a\0Fix things.\n0badf00\0style: reformat\n"

    assert commit_messages.main(["--range", "origin/main..HEAD"], log=log) == 1
    out = capsys.readouterr().out
    assert asked == ["origin/main..HEAD"]
    assert "commit d4e5f6a: 'Fix things.'" in out and "commit 0badf00: 'style: reformat'" in out
    assert "a1b2c3d" not in out and "2 subject(s) don't follow" in out
    assert commit_messages.main(["--range", "x..y"], log=lambda r: "") == 0, "an empty range has nothing to fail"

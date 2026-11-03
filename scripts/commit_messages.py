"""Check that commit subjects follow Conventional Commits, as CI does for every pull request.

    uv run python scripts/commit_messages.py --title "feat(canvas): show the dashboard beside the session"
    uv run python scripts/commit_messages.py --range origin/main..HEAD      # every commit about to be proposed

A subject is ``type(scope)!: description``: one of the types below, an optional lowercase scope, an optional
``!`` for a breaking change, then a description that doesn't end in a period (its case is the writer's; Dependabot
capitalizes, and the specification doesn't mind). The body is free prose (what changed and why, as a person would
write it); only the subject is checked. The pull request's
title is checked because a squash makes it the commit; each commit is checked because a rebase lands them as
they are. Merges and reverts GitHub writes itself (``Merge ...``, ``Revert "..."``) are accepted as they come.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable

TYPES = ("feat", "fix", "docs", "refactor", "test", "build", "ci", "chore", "perf", "revert")
"""What a change is. No ``style``: ruff owns formatting, so a formatting-only change is a ``chore``."""

MAX_SUBJECT = 100

SUBJECT_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9][a-z0-9/._-]*)\))?(?P<breaking>!)?: (?P<description>\S.*)$"
)
GITHUB_OWN_RE = re.compile(r'^(Merge (branch|pull request|remote-tracking branch) |Revert ".*"$)')


def problems(subject: str) -> list[str]:
    """Everything wrong with one subject line, in the order a reader would notice; empty when it conforms."""
    if GITHUB_OWN_RE.match(subject):
        return []
    found: list[str] = []
    if len(subject) > MAX_SUBJECT:
        found.append(f"longer than {MAX_SUBJECT} characters ({len(subject)})")
    m = SUBJECT_RE.match(subject)
    if not m:
        return [
            *found,
            "not `type(scope): description`; types are " + ", ".join(TYPES) + ", the scope is optional and lowercase",
        ]
    if m["type"] not in TYPES:
        found.append(f"unknown type `{m['type']}`; one of " + ", ".join(TYPES))
    if m["description"].endswith("."):
        found.append("the description ends with a period")
    return found


def git_log(rev_range: str) -> str:
    """``git log`` of a range as ``<short sha>NUL<subject>`` lines, oldest first."""
    return subprocess.run(
        ["git", "log", "--reverse", "--format=%h%x00%s", rev_range],
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout


def subjects_in(log: str) -> list[tuple[str, str]]:
    """``(short sha, subject)`` for every line of ``git_log``'s output."""
    pairs = (line.split("\0", 1) for line in log.splitlines() if line)
    return [(sha, subject) for sha, subject in pairs]


def main(argv: list[str] | None = None, *, log: Callable[[str], str] = git_log) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--title", help="a pull request title to check")
    parser.add_argument("--range", dest="rev_range", help="a git revision range whose commit subjects to check")
    args = parser.parse_args(argv)
    if not args.title and not args.rev_range:
        parser.error("give --title, --range, or both")
    failures = 0
    checks = [("the pull request title", args.title)] if args.title else []
    if args.rev_range:
        checks += [(f"commit {sha}", subject) for sha, subject in subjects_in(log(args.rev_range))]
    for what, subject in checks:
        found = problems(subject)
        if found:
            failures += 1
            print(f"{what}: {subject!r}")
            for item in found:
                print(f"    {item}")
    if failures:
        print(f"\n{failures} subject(s) don't follow Conventional Commits; see CONTRIBUTING.md.")
        return 1
    print(f"{len(checks)} subject(s) follow Conventional Commits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

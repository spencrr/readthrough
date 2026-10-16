"""The pull request as readthrough sees it, whichever forge it came from.

Every forge adapter maps its own API into these records, and the analysis reads only these. The fields are
the concepts the analysis consumes, not a union of every forge's schema. Where forges disagree on what a
value means, the record keeps the distinction (an approval is not the same fact as "mergeable").

A value a forge may not be able to supply is a ``Capability``: ``known`` (including a known empty value),
``unsupported`` (this forge has no such concept, or no API for it), or ``unavailable`` (it has one, but the
call failed this time). The build turns the last two into a warning and a per-PR signal, so a missing
answer is never read as "none".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Capability(Generic[T]):
    """A value the forge may or may not have been able to provide."""

    kind: str  # known | unsupported | unavailable
    value: T | None = None
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.kind == "known"

    def get(self, default: T) -> T:
        """The value when known, otherwise ``default``. Callers must have reported the gap first."""
        return self.value if self.kind == "known" and self.value is not None else default


def known(value: T) -> Capability[T]:
    return Capability("known", value)


def unsupported(reason: str) -> Capability[T]:
    return Capability("unsupported", reason=reason)


def unavailable(reason: str) -> Capability[T]:
    return Capability("unavailable", reason=reason)


@dataclass(frozen=True)
class Author:
    login: str
    bot: bool = False


@dataclass(frozen=True)
class ChangedFile:
    path: str
    additions: int
    deletions: int
    change: str = "MODIFIED"  # ADDED | MODIFIED | DELETED | RENAMED | COPIED | CHANGED


@dataclass(frozen=True)
class Review:
    author: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
    submitted: str | None = None  # ISO 8601


@dataclass(frozen=True)
class Thread:
    """One review discussion, with its first comment."""

    author: str
    body: str
    body_html: str = ""
    path: str | None = None
    line: int | None = None
    resolved: bool = False
    outdated: bool = False


@dataclass(frozen=True)
class Check:
    """One CI check or commit status on the head commit."""

    name: str
    status: str  # COMPLETED | IN_PROGRESS | QUEUED | PENDING
    conclusion: str | None = (
        None  # SUCCESS | FAILURE | NEUTRAL | CANCELLED | SKIPPED | TIMED_OUT | ACTION_REQUIRED | ERROR
    )
    url: str | None = None
    started: str | None = None
    completed: str | None = None


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    url: str


# Author associations, in GitHub's vocabulary, which the profile's [authors] rules are written against.
# Adapters for forges with other vocabularies map into it (a GitLab Maintainer is a MEMBER, say).
ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE")

# The review decision: the forge's own verdict on whether the PR has the approvals it needs.
DECISIONS = ("APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", "NONE")


@dataclass
class PullRequest:
    number: int
    title: str
    url: str
    body: str
    draft: bool
    created: str
    updated: str
    additions: int
    deletions: int
    changed_files: int
    head: str
    base: str
    author: Author
    files: list[ChangedFile]
    body_html: str = ""
    labels: list[str] = field(default_factory=list)
    requested: list[str] = field(default_factory=list)  # logins or team names asked to review
    reviews: list[Review] = field(default_factory=list)  # the latest review per reviewer
    comments: int = 0
    last_commit: str | None = None  # when the head commit was committed
    issues: list[Issue] = field(default_factory=list)  # issues this PR closes
    association: Capability[str] = field(default_factory=lambda: unsupported("author association"))
    decision: Capability[str] = field(default_factory=lambda: unsupported("review decision"))
    mergeable: Capability[str] = field(
        default_factory=lambda: unsupported("merge state")
    )  # MERGEABLE | CONFLICTING | UNKNOWN
    threads: Capability[list[Thread]] = field(default_factory=lambda: unsupported("review threads"))
    checks: Capability[list[Check]] = field(default_factory=lambda: unsupported("CI checks"))
    # Whether the per-file additions and deletions mean anything. A forge without line counts (Azure DevOps)
    # marks this unsupported, and the analysis weighs files by count instead of by lines.
    line_counts: Capability[bool] = field(default_factory=lambda: known(True))
    truncated: dict[str, tuple[int, int]] = field(default_factory=dict)  # field -> (have, of) when a list was cut short

    def gaps(self) -> list[tuple[str, Capability]]:
        """The capabilities this record couldn't fill, for warnings and signals."""
        out = []
        for name in GAP_FIELDS:
            cap: Capability = getattr(self, name)
            if not cap.known:
                out.append((name, cap))
        return out


GAP_FIELDS = ("association", "decision", "mergeable", "threads", "checks", "line_counts")


@dataclass(frozen=True)
class MergedPullRequest:
    """What onboarding needs from a recently merged PR: who, and which files."""

    number: int
    author: Author
    association: Capability[str]
    files: list[ChangedFile]

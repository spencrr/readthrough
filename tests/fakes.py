"""A forge adapter for tests: the real ``Forge`` base (retries, diff cache) over in-memory data.

Install it with the ``fake_forge`` fixture in conftest, which points ``forges.make`` at it, so every
code path that would reach GitHub or another forge reaches this instead. The class-level knobs are reset
by that fixture before each test.
"""

from __future__ import annotations

import copy
from typing import Any, ClassVar

from readthrough.faults import NotFound
from readthrough.forges import CiContext, Forge
from readthrough.forges.github import normalize
from readthrough.model import Author as PrAuthor
from readthrough.model import Capability, ChangedFile, MergedPullRequest, PullRequest, known


class FakeForge(Forge):
    name = "github"
    label = "GitHub"
    config_dir = ".github"
    pr_noun = "pull request"
    capabilities = frozenset({"diffs", "required_checks", "default_branch_ci", "rate_limit", "memberships", "accounts"})

    # What the fake knows, set by tests. ``prs`` holds GraphQL-shaped dicts (see conftest.make_pr) so tests
    # can keep describing PRs the way GitHub returns them; ``fetch_prs`` normalizes them.
    prs: ClassVar[list[dict[str, Any]]] = []
    total: ClassVar[int | None] = None
    viewer: ClassVar[str] = "me"
    fail: ClassVar[Exception | None] = None  # raised by fetch_prs
    unreadable: ClassVar[Exception | None] = None  # raised by repo_info
    files: ClassVar[dict[str, str]] = {}  # path -> text at the default branch
    tree_paths: ClassVar[list[str]] = []
    merged: ClassVar[list[MergedPullRequest]] = []
    members: ClassVar[set[tuple[str, str]]] = set()  # (group, login) pairs that are members
    membership_error: ClassVar[Exception | None] = None
    diff_texts: ClassVar[dict[int, str]] = {}
    diff_error: ClassVar[Exception | None] = None
    required: ClassVar[dict[str, Capability[set[str]]]] = {}
    main_failing: ClassVar[Capability[set[str]]] = known(set())
    ci_error: ClassVar[Exception | None] = None
    default_branch: ClassVar[str] = "main"
    rate: ClassVar[dict[str, int] | None] = {"graphql": 4900, "rest": 4800}
    calls: ClassVar[list[str]] = []

    @classmethod
    def reset(cls) -> None:
        cls.prs, cls.total, cls.viewer, cls.fail, cls.unreadable = [], None, "me", None, None
        cls.files, cls.tree_paths, cls.merged = {}, [], []
        cls.members, cls.membership_error = set(), None
        cls.diff_texts, cls.diff_error = {}, None
        cls.required, cls.main_failing, cls.ci_error = {}, known(set()), None
        cls.default_branch, cls.rate, cls.calls = "main", {"graphql": 4900, "rest": 4800}, []

    def __init__(self, repo: str, options: Any = None, tuning: Any = None, *, account: str | None = None) -> None:
        super().__init__(repo, options, tuning, account=account)
        self.owner, self.repo_name = repo.split("/")

    @classmethod
    def validate_repo(cls, repo: str) -> str | None:
        return None if repo.count("/") == 1 and all(repo.split("/")) else f"{repo!r} isn't owner/name"

    def web_url(self) -> str:
        return f"https://github.com/{self.repo}"

    def pr_url(self, number: int) -> str:
        return f"{self.web_url()}/pull/{number}"

    def pr_ref(self, number: int) -> str:
        return f"pull/{number}/head"

    def file_url(self, path: str, branch: str) -> str:
        return f"{self.web_url()}/blob/{branch}/{path}"

    def new_file_url(self, path: str, branch: str) -> str | None:
        return f"{self.web_url()}/new/{branch}?filename={path}"

    def describe_auth(self) -> str:
        return f"gh as {self.account}" if self.account else "gh (active account)"

    def repo_info(self) -> dict[str, Any]:
        self.calls.append("repo_info")
        if self.unreadable:
            raise self.unreadable
        return {
            "full_name": self.repo,
            "default_branch": self.default_branch,
            "private": False,
            "description": "A repo",
        }

    def rate_limit(self) -> dict[str, int] | None:
        return self.rate

    def fetch_prs(self, limit: int) -> tuple[str, list[PullRequest], int]:
        self.calls.append("fetch_prs")
        if self.fail:
            raise self.fail
        prs = [normalize(copy.deepcopy(p)) for p in self.prs[:limit]]
        return self.viewer, prs, self.total if self.total is not None else len(self.prs)

    def merged_prs(self, count: int) -> tuple[str, list[MergedPullRequest]]:
        self.calls.append("merged_prs")
        return self.default_branch, list(self.merged[:count])

    def tree(self, branch: str) -> tuple[list[str], bool]:
        self.calls.append(f"tree {branch}")
        return list(self.tree_paths) or sorted(self.files), True

    def read_file(self, path: str) -> str | None:
        self.calls.append(f"read {path}")
        return self.files.get(path)

    def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted(p[len(prefix) :] for p in self.files if p.startswith(prefix) and "/" not in p[len(prefix) :])

    def ci_context(self, bases: set[str]) -> CiContext:
        self.calls.append("ci_context")
        if self.ci_error:
            raise self.ci_error
        required = {b: self.required.get(b, known(set())) for b in bases}
        return CiContext(required, self.main_failing, self.default_branch)

    def is_member(self, group: str, login: str) -> bool:
        self.calls.append(f"member {group} {login}")
        if self.membership_error:
            raise self.membership_error
        return (group, login) in self.members

    def fetch_diff(self, number: int) -> str:
        self.calls.append(f"diff {number}")
        if self.diff_error:
            raise self.diff_error
        if number not in self.diff_texts:
            raise NotFound(f"pr diff #{number}: no such PR", stderr="HTTP 404")
        return self.diff_texts[number]


def merged_pr(number: int, login: str, association: str = "NONE", *paths: str, bot: bool = False) -> MergedPullRequest:
    """A recently merged PR for onboarding tests: ten changed lines per path."""
    return MergedPullRequest(
        number=number,
        author=PrAuthor(login, bot=bot),
        association=known(association),
        files=[ChangedFile(p, 8, 2) for p in paths],
    )

"""Forgejo and Gitea (Codeberg is Forgejo), through their REST API, which the two share.

The token comes from ``FORGEJO_TOKEN`` if set, else calls are anonymous, which is enough for public repos.
There is no first-party CLI with a stable token command to lean on (``tea`` keeps its own logins), so the
environment variable is the way in for private repos.

Forgejo has reviews with states like GitHub's and required status checks in branch protection, so most
of the record maps directly. It has no "author association"; that's derived from whether the author is a
collaborator or the owner, which needs read access to collaborators and is otherwise reported as
unavailable rather than guessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from readthrough.ci import failing_names
from readthrough.faults import AuthError, ForgeError, NotFound
from readthrough.forges import FORGES, CiContext, Forge, Rest, credential, link_next
from readthrough.logs import pool_map
from readthrough.model import (
    Author,
    Capability,
    ChangedFile,
    Check,
    Issue,
    MergedPullRequest,
    PullRequest,
    Review,
    Thread,
    known,
    unavailable,
)

log = logging.getLogger(__name__)

REVIEW_STATES = {"APPROVED": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED",
                 "PENDING": "PENDING", "REQUEST_REVIEW": "PENDING"}  # fmt: skip
STATUS = {"success": "SUCCESS", "failure": "FAILURE", "error": "ERROR", "pending": None, "warning": "NEUTRAL"}


@dataclass(frozen=True)
class ForgejoOptions:
    host: str = "codeberg.org"
    credential_env: str = "FORGEJO_TOKEN"


@FORGES.add("forgejo", "Forgejo and Gitea (Codeberg), through their REST API", options=ForgejoOptions)
class Forgejo(Forge):
    name = "forgejo"
    label = "Forgejo"
    config_dir = ".forgejo"
    capabilities = frozenset({"diffs", "required_checks", "default_branch_ci"})

    def __init__(self, repo: str, options: Any = None, tuning: Any = None, *, account: str | None = None) -> None:
        super().__init__(repo, options or ForgejoOptions(), tuning, account=account)
        self.host: str = self.options.host
        env = self.options.credential_env
        self.rest = Rest(
            f"https://{self.host}/api/v1/",
            lambda: credential(env=env, cli=None, cli_name="", scheme="token"),
            self.tuning,
        )
        self.owner, self.repo_name = repo.split("/")
        self._default_branch: str | None = None
        self._collaborators: Capability[set[str]] | None = None

    @classmethod
    def validate_repo(cls, repo: str) -> str | None:
        parts = repo.split("/")
        return None if len(parts) == 2 and all(parts) else f"{repo!r} isn't owner/name"

    def web_url(self) -> str:
        return f"https://{self.host}/{self.repo}"

    def pr_url(self, number: int) -> str:
        return f"{self.web_url()}/pulls/{number}"

    def pr_ref(self, number: int) -> str:
        return f"pull/{number}/head"

    def file_url(self, path: str, branch: str) -> str:
        return f"{self.web_url()}/src/branch/{branch}/{path}"

    def new_file_url(self, path: str, branch: str) -> str | None:
        return f"{self.web_url()}/_new/{quote(branch, safe='')}/?filename={quote(path)}"

    def describe_auth(self) -> str:
        return self.rest.cred.source

    # ---- REST helpers

    def _get(self, path: str, **params: Any) -> Any:
        resp = self.retrying(f"GET {path}", lambda: self.rest.call("GET", path, params=params))
        return resp.json() if resp else None

    def _get_or_none(self, path: str, **params: Any) -> Any:
        resp = self.retrying(f"GET {path}", lambda: self.rest.call("GET", path, params=params, absent=(NotFound,)))
        return resp.json() if resp else None

    def _pages(self, path: str, *, limit: int, **params: Any) -> list[Any]:
        items: list[Any] = []
        url: str | None = self.rest.url(path, {**params, "limit": min(50, limit)})
        while url and len(items) < limit:
            resp = self.retrying(f"GET {path}", lambda u=url: self.rest.call("GET", u))
            if resp is None:
                break
            page = resp.json() or []
            items.extend(page)
            url = link_next(resp.headers) if page else None
        return items[:limit]

    def _branch(self) -> str:
        if self._default_branch is None:
            self._default_branch = self.repo_info()["default_branch"] or "main"
        return self._default_branch

    @property
    def _r(self) -> str:
        return f"repos/{self.owner}/{self.repo_name}"

    # ---- the repo

    def repo_info(self) -> dict[str, Any]:
        info = self._get(self._r)
        return {"full_name": info.get("full_name"), "default_branch": info.get("default_branch"),
                "private": bool(info.get("private")), "description": info.get("description")}  # fmt: skip

    def tree(self, branch: str) -> tuple[list[str], bool]:
        paths: list[str] = []
        page, limit = 1, self.tuning.max_tree_files
        while True:
            data = self._get(
                f"{self._r}/git/trees/{quote(branch, safe='')}", recursive="true", page=page, per_page=1000
            )
            paths += [e["path"] for e in data.get("tree") or [] if e.get("type") == "blob"]
            more = bool(data.get("truncated")) and data.get("total_count", 0) > page * 1000
            if more and len(paths) < limit:
                page += 1
                continue
            return paths[:limit], not more and len(paths) <= limit

    def read_file(self, path: str) -> str | None:
        resp = self.retrying(f"GET raw {path}", lambda: self.rest.call(
            "GET", f"{self._r}/raw/{quote(path)}", params={"ref": self._branch()}, headers={"Accept": "*/*"}, absent=(NotFound,),
        ))  # fmt: skip
        return resp.text if resp else None

    def list_dir(self, path: str) -> list[str]:
        items = self._get_or_none(f"{self._r}/contents/{quote(path)}", ref=self._branch()) or []
        return [e["name"] for e in items if isinstance(e, dict) and e.get("type") == "file"]

    def _association(self, login: str) -> Capability[str]:
        if login.lower() == self.owner.lower():
            return known("OWNER")
        if self._collaborators is None:
            try:
                names = {c["login"].lower() for c in self._pages(f"{self._r}/collaborators", limit=500)}
                self._collaborators = known(names)
            except (AuthError, NotFound):
                self._collaborators = unavailable("collaborators aren't visible without a token")
            except ForgeError as exc:
                self._collaborators = unavailable(exc.reason)
        if not self._collaborators.known:
            return unavailable(self._collaborators.reason)
        return known("COLLABORATOR" if login.lower() in (self._collaborators.value or set()) else "CONTRIBUTOR")

    # ---- pull requests

    def fetch_prs(self, limit: int) -> tuple[str, list[PullRequest], int]:
        raw = self._pages(f"{self._r}/pulls", limit=limit, state="open", sort="recentupdate")
        resp = self.rest.call("GET", f"{self._r}/pulls", params={"state": "open", "limit": 1}, absent=(ForgeError,))
        total = int(resp.headers.get("x-total-count", len(raw))) if resp else len(raw)
        me = self._get_or_none("user") if self.rest.cred.token else None
        viewer = (me or {}).get("login") or ""
        return viewer, pool_map(self._complete, raw, self.tuning.concurrency), total

    def _complete(self, pr: dict[str, Any]) -> PullRequest:
        n = pr["number"]
        files_raw = self._pages(f"{self._r}/pulls/{n}/files", limit=self.tuning.max_files_per_pr)
        files = [ChangedFile(f["filename"], f.get("additions", 0), f.get("deletions", 0),
                             {"added": "ADDED", "deleted": "DELETED", "renamed": "RENAMED"}.get(f.get("status", ""), "MODIFIED"))
                 for f in files_raw]  # fmt: skip
        reviews_raw = self._pages(f"{self._r}/pulls/{n}/reviews", limit=100)
        latest: dict[str, Review] = {}
        for r in reviews_raw:
            state = REVIEW_STATES.get(r.get("state") or "", "COMMENTED")
            if r.get("user") and state != "PENDING" and not r.get("dismissed"):
                latest[r["user"]["login"]] = Review(r["user"]["login"], state, r.get("submitted_at"))
        reviews = list(latest.values())
        threads = self._threads(n, reviews_raw)
        head_sha = (pr.get("head") or {}).get("sha") or ""
        checks: Capability[list[Check]]
        try:
            checks = known(self._checks(head_sha) if head_sha else [])
        except ForgeError as exc:
            checks = unavailable(exc.reason)
        last_commit = None
        if head_sha:
            commit = self._get_or_none(f"{self._r}/git/commits/{head_sha}") or {}
            last_commit = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        states = {r.state for r in reviews}
        decision = ("CHANGES_REQUESTED" if "CHANGES_REQUESTED" in states else "APPROVED" if "APPROVED" in states
                    else "REVIEW_REQUIRED")  # fmt: skip
        mergeable = "UNKNOWN" if pr.get("mergeable") is None else ("MERGEABLE" if pr["mergeable"] else "CONFLICTING")
        user = pr.get("user") or {}
        return PullRequest(
            number=n, title=pr["title"], url=pr["html_url"], body=pr.get("body") or "", draft=bool(pr.get("draft")),
            created=pr["created_at"], updated=pr["updated_at"],
            additions=int(pr["additions"]) if pr.get("additions") is not None else sum(f.additions for f in files),
            deletions=int(pr["deletions"]) if pr.get("deletions") is not None else sum(f.deletions for f in files),
            changed_files=pr.get("changed_files") or len(files), head=head_sha, base=(pr.get("base") or {}).get("ref") or "main",
            author=Author(user.get("login") or "ghost", bot=bool(user.get("is_bot") or user.get("login", "").endswith("[bot]"))),
            files=files, labels=[label["name"] for label in pr.get("labels") or []],
            requested=[u["login"] for u in pr.get("requested_reviewers") or [] if u],
            reviews=reviews, comments=int(pr.get("comments") or 0), last_commit=last_commit,
            issues=[Issue(0, "", "")][:0], association=self._association(user.get("login") or ""),
            decision=known(decision), mergeable=known(mergeable), threads=threads, checks=checks,
            truncated={"files": (len(files), pr["changed_files"])} if pr.get("changed_files", 0) > len(files) else {},
        )  # fmt: skip

    def _threads(self, number: int, reviews_raw: list[dict[str, Any]]) -> Capability[list[Thread]]:
        """Review comments, which Forgejo serves per review; a comment with a ``resolver`` is resolved."""
        cap = self.tuning.max_review_threads_per_pr
        threads: list[Thread] = []
        try:
            for review in reviews_raw:
                if not review.get("comments_count") or len(threads) >= cap:
                    continue
                comments = self._get_or_none(f"{self._r}/pulls/{number}/reviews/{review['id']}/comments") or []
                threads += [
                    Thread(c["user"]["login"], c.get("body") or "", c.get("body_html") or "", c.get("path"),
                           c.get("position"), bool(c.get("resolver")), False)
                    for c in comments if c.get("user")
                ]  # fmt: skip
        except ForgeError as exc:
            return unavailable(exc.reason)
        return known(threads[:cap])

    def _checks(self, ref: str) -> list[Check]:
        """Commit statuses on ``ref``: a history, with every rerun; ``ci.latest_checks`` keeps the newest per name."""
        statuses = self._pages(f"{self._r}/commits/{quote(ref, safe='')}/statuses", limit=200)
        checks = []
        for s in statuses:
            url = s.get("target_url") or None
            if url and url.startswith("/"):
                url = f"https://{self.host}{url}"
            checks.append(Check(s["context"], "COMPLETED" if s.get("status") != "pending" else "PENDING",
                                STATUS.get(s.get("status") or ""), url, s.get("created_at")))  # fmt: skip
        return checks

    def merged_prs(self, count: int) -> tuple[str, list[MergedPullRequest]]:
        raw = self._pages(f"{self._r}/pulls", limit=count, state="closed", sort="recentupdate")
        merged = []
        for pr in raw:
            if not pr.get("merged"):
                continue
            files_raw = self._pages(f"{self._r}/pulls/{pr['number']}/files", limit=100)
            user = pr.get("user") or {}
            merged.append(MergedPullRequest(
                pr["number"], Author(user.get("login") or "ghost", bool(user.get("is_bot"))),
                self._association(user.get("login") or ""),
                [ChangedFile(f["filename"], f.get("additions", 0), f.get("deletions", 0)) for f in files_raw],
            ))  # fmt: skip
        return self._branch(), merged

    def fetch_diff(self, number: int) -> str:
        resp = self.retrying(f"pr diff #{number}", lambda: self.rest.call(
            "GET", f"{self._r}/pulls/{number}.diff", headers={"Accept": "text/plain"},
        ))  # fmt: skip
        return resp.text if resp else ""

    # ---- CI

    def ci_context(self, bases: set[str]) -> CiContext:
        branch = self._branch()
        required: dict[str, Capability[set[str]]] = {}
        try:
            protections = self._get_or_none(f"{self._r}/branch_protections") or []
            for base in bases:
                names = {ctx for p in protections if p.get("branch_name") == base or p.get("rule_name") == base
                         for ctx in p.get("status_check_contexts") or []}  # fmt: skip
                required[base] = known(names)
        except AuthError:
            required = {b: unavailable("branch protection isn't visible without a token") for b in bases}
        except ForgeError as exc:
            required = {b: unavailable(exc.reason) for b in bases}
        failing: Capability[set[str]]
        try:
            failing = known(failing_names(self._checks(branch)))
        except ForgeError as exc:
            failing = unavailable(exc.reason)
        return CiContext(required, failing, branch)

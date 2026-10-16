"""GitLab (gitlab.com or self-hosted), through the REST API.

The token comes from ``GITLAB_TOKEN`` if set, else from a signed-in ``glab`` CLI, else calls are anonymous,
which is enough for public projects. Projects nest (``group/subgroup/project``); the whole path is the repo.

What GitLab has and GitHub doesn't, and the other way round: an MR's "approved" is a count of approvals
against rules, so the review decision is derived (approved by rules, changes requested through a blocking
discussion, or review required). Author association comes from the author's access level in the project.
Required checks don't exist as names; the project setting "pipelines must succeed" is the nearest thing,
so required checks are reported as unsupported and the pipeline status drives the CI state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from readthrough.faults import ForgeError, NotFound, TooLarge
from readthrough.forges import FORGES, CiContext, Forge, Rest, cached_cli_token, credential, link_next
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
    unsupported,
)
from readthrough.process import quiet_output

log = logging.getLogger(__name__)

# GitLab access levels -> the association vocabulary the profile's [authors] rules use.
ACCESS = {50: "OWNER", 40: "MEMBER", 30: "MEMBER", 20: "COLLABORATOR", 10: "COLLABORATOR", 5: "COLLABORATOR"}
PIPELINE = {
    "success": ("COMPLETED", "SUCCESS"), "failed": ("COMPLETED", "FAILURE"), "canceled": ("COMPLETED", "CANCELLED"),
    "skipped": ("COMPLETED", "SKIPPED"), "manual": ("COMPLETED", "ACTION_REQUIRED"), "running": ("IN_PROGRESS", None),
    "pending": ("QUEUED", None), "created": ("QUEUED", None), "waiting_for_resource": ("QUEUED", None),
    "preparing": ("QUEUED", None), "scheduled": ("QUEUED", None),
}  # fmt: skip


@dataclass(frozen=True)
class GitLabOptions:
    host: str = "gitlab.com"
    credential_env: str = (
        "GITLAB_TOKEN"  # read if set; otherwise `glab auth status --show-token` when glab is signed in
    )


def _glab_token(host: str) -> str | None:
    """The token glab holds for ``host``. glab prints its status report to stderr, one ``Token: ...`` line per host."""

    def ask() -> str | None:
        out = quiet_output(["glab", "auth", "status", "--hostname", host, "--show-token"], with_stderr=True)
        return parse_glab_token(out or "")

    return cached_cli_token(f"glab {host}", ask)


def parse_glab_token(report: str) -> str | None:
    for line in report.splitlines():
        if "Token:" in line:
            token = line.split("Token:", 1)[1].strip()
            return token if token and not token.startswith("*") else None  # stars mean it wasn't asked to show it
    return None


@FORGES.add("gitlab", "GitLab, gitlab.com or self-hosted, through its REST API", options=GitLabOptions)
class GitLab(Forge):
    name = "gitlab"
    label = "GitLab"
    config_dir = ".gitlab"
    pr_noun = "merge request"
    shape = "group/project (or group/subgroup/project)"
    capabilities = frozenset({"diffs", "default_branch_ci", "rate_limit"})

    def __init__(self, repo: str, options: Any = None, tuning: Any = None, *, account: str | None = None) -> None:
        super().__init__(repo, options or GitLabOptions(), tuning, account=account)
        self.host: str = self.options.host
        env = self.options.credential_env
        self.rest = Rest(
            f"https://{self.host}/api/v4/",
            lambda: credential(env=env, cli=lambda: _glab_token(self.host), cli_name="glab"),
            self.tuning,
        )
        self.project = quote(repo, safe="")
        self._default_branch: str | None = None
        self._members: dict[str, int] | None = None

    @classmethod
    def validate_repo(cls, repo: str) -> str | None:
        parts = repo.split("/")
        if len(parts) < 2 or not all(p and "/" not in p and p not in (".", "..") for p in parts):
            return f"{repo!r} isn't group/project (groups may nest: group/subgroup/project)"
        return None

    def web_url(self) -> str:
        return f"https://{self.host}/{self.repo}"

    def pr_url(self, number: int) -> str:
        return f"{self.web_url()}/-/merge_requests/{number}"

    def pr_ref(self, number: int) -> str:
        return f"merge-requests/{number}/head"

    def file_url(self, path: str, branch: str) -> str:
        return f"{self.web_url()}/-/blob/{branch}/{path}"

    def new_file_url(self, path: str, branch: str) -> str | None:
        return f"{self.web_url()}/-/new/{quote(branch, safe='')}?file_name={quote(path)}"

    def describe_auth(self) -> str:
        return self.rest.cred.source

    # ---- REST helpers

    def _get(self, path: str, **params: Any) -> Any:
        resp = self.retrying(f"GET {path}", lambda: self.rest.call("GET", path, params=params))
        return resp.json() if resp else None

    def _get_or_none(self, path: str, **params: Any) -> Any:
        resp = self.retrying(f"GET {path}", lambda: self.rest.call("GET", path, params=params, absent=(NotFound,)))
        return resp.json() if resp else None

    def _pages(self, endpoint: str, *, limit: int, **params: Any) -> list[Any]:
        items: list[Any] = []
        url: str | None = self.rest.url(endpoint, {**params, "per_page": min(100, limit)})
        while url and len(items) < limit:
            resp = self.retrying(f"GET {endpoint}", lambda u=url: self.rest.call("GET", u))
            if resp is None:
                break
            items.extend(resp.json() or [])
            url = link_next(resp.headers)
        return items[:limit]

    def _branch(self) -> str:
        if self._default_branch is None:
            self._default_branch = self.repo_info()["default_branch"] or "main"
        return self._default_branch

    # ---- the repo

    def repo_info(self) -> dict[str, Any]:
        info = self._get(f"projects/{self.project}")
        return {
            "full_name": info.get("path_with_namespace"), "default_branch": info.get("default_branch"),
            "private": info.get("visibility") != "public", "description": info.get("description"),
        }  # fmt: skip

    def rate_limit(self) -> dict[str, int] | None:
        resp = self.rest.call("GET", f"projects/{self.project}", absent=(ForgeError,))
        if resp is None or "ratelimit-remaining" not in resp.headers:
            return None
        return {"rest": int(resp.headers["ratelimit-remaining"])}

    def tree(self, branch: str) -> tuple[list[str], bool]:
        limit = self.tuning.max_tree_files
        items = self._pages(f"projects/{self.project}/repository/tree", limit=limit + 1, ref=branch, recursive="true")
        paths = [e["path"] for e in items if e.get("type") == "blob"]
        return paths[:limit], len(items) <= limit

    def read_file(self, path: str) -> str | None:
        resp = self.retrying(f"GET file {path}", lambda: self.rest.call(
            "GET", f"projects/{self.project}/repository/files/{quote(path, safe='')}/raw",
            params={"ref": self._branch()}, headers={"Accept": "*/*"}, absent=(NotFound,),
        ))  # fmt: skip
        return resp.text if resp else None

    def list_dir(self, path: str) -> list[str]:
        items = self._pages(f"projects/{self.project}/repository/tree", limit=1000, path=path, ref=self._branch())
        return [e["name"] for e in items if e.get("type") == "blob"]

    # ---- members, for author association

    def _access(self, login: str) -> Capability[str]:
        if self._members is None:
            try:
                members = self._pages(f"projects/{self.project}/members/all", limit=2000)
                self._members = {m["username"].lower(): m["access_level"] for m in members}
            except ForgeError as exc:
                self._members = {}
                self._members_error = exc.reason
        if not self._members and getattr(self, "_members_error", ""):
            return unavailable(f"members: {self._members_error}")
        level = self._members.get(login.lower())
        return known(ACCESS.get(level or 0, "CONTRIBUTOR"))

    # ---- merge requests

    def fetch_prs(self, limit: int) -> tuple[str, list[PullRequest], int]:
        raw = self._pages(f"projects/{self.project}/merge_requests", limit=limit, state="opened", order_by="updated_at",
                          sort="desc", with_labels_details="false")  # fmt: skip
        total = len(raw)
        resp = self.rest.call("GET", f"projects/{self.project}/merge_requests", params={"state": "opened", "per_page": 1},
                              absent=(ForgeError,))  # fmt: skip
        if resp is not None and resp.headers.get("x-total"):
            total = int(resp.headers["x-total"])
        viewer = ""
        me = self._get_or_none("user") if self.rest.cred.token else None
        if me:
            viewer = me.get("username") or ""
        from readthrough.logs import pool_map

        prs = pool_map(self._complete, raw, self.tuning.concurrency)
        return viewer, prs, total

    def _complete(self, mr: dict[str, Any]) -> PullRequest:
        iid = mr["iid"]
        base = f"projects/{self.project}/merge_requests/{iid}"
        detail = self._get(base)  # has diff_stats-ish counts, pipeline, detailed_merge_status
        changes = self._pages(f"{base}/diffs", limit=self.tuning.max_files_per_pr)
        files = [
            ChangedFile(
                c.get("new_path") or c.get("old_path"),
                *_count_lines(c.get("diff") or ""),
                "ADDED" if c.get("new_file") else "DELETED" if c.get("deleted_file") else "RENAMED" if c.get("renamed_file") else "MODIFIED",
            )
            for c in changes
        ]  # fmt: skip
        approvals = self._get_or_none(f"{base}/approvals") or {}
        reviews = [Review(a["user"]["username"], "APPROVED", None) for a in approvals.get("approved_by") or []]
        reviews += [Review(r["username"], "COMMENTED", None) for r in mr.get("reviewers") or []
                    if r["username"] not in {x.author for x in reviews}]  # fmt: skip
        threads: list[Thread] = []
        try:
            for d in self._pages(f"{base}/discussions", limit=self.tuning.max_review_threads_per_pr):
                notes = [n for n in d.get("notes") or [] if not n.get("system")]
                if not notes or not notes[0].get("resolvable"):
                    continue
                first = notes[0]
                pos = first.get("position") or {}
                threads.append(Thread(first["author"]["username"], first.get("body") or "", "", pos.get("new_path"),
                                      pos.get("new_line"), bool(first.get("resolved")), False))  # fmt: skip
            threads_cap: Capability[list[Thread]] = known(threads)
        except ForgeError as exc:
            threads_cap = unavailable(exc.reason)
        pipeline = detail.get("head_pipeline") or mr.get("head_pipeline")
        checks: Capability[list[Check]]
        if pipeline:
            status, conclusion = PIPELINE.get(pipeline.get("status") or "", ("QUEUED", None))
            checks = known([Check("pipeline", status, conclusion, pipeline.get("web_url"), pipeline.get("created_at"),
                                  pipeline.get("updated_at"))])  # fmt: skip
        else:
            checks = known([])
        # The decision rests on the discussions: without them it can't be known, and isn't guessed.
        decision: Capability[str]
        if not threads_cap.known:
            decision = unavailable(f"review decision needs the discussions: {threads_cap.reason}")
        elif any(not t.resolved for t in threads):
            decision = known("CHANGES_REQUESTED")
        else:
            decision = known("APPROVED" if approvals.get("approved") else "REVIEW_REQUIRED")
        status = detail.get("detailed_merge_status") or detail.get("merge_status") or ""
        mergeable = (
            "CONFLICTING"
            if status in ("conflict", "cannot_be_merged")
            else "MERGEABLE"
            if status in ("mergeable", "can_be_merged")
            else "UNKNOWN"
        )
        author = mr.get("author") or {}
        return PullRequest(
            number=iid, title=mr["title"], url=mr["web_url"], body=mr.get("description") or "", draft=bool(mr.get("draft")),
            created=mr["created_at"], updated=mr["updated_at"],
            additions=sum(f.additions for f in files), deletions=sum(f.deletions for f in files),
            changed_files=(detail.get("changes_count") and int(str(detail["changes_count"]).rstrip("+"))) or len(files),
            head=mr.get("sha") or "", base=mr.get("target_branch") or "main",
            author=Author(author.get("username") or "ghost", bot=bool(author.get("bot"))),
            files=files, labels=list(mr.get("labels") or []),
            requested=[r["username"] for r in mr.get("reviewers") or []], reviews=reviews,
            comments=int(mr.get("user_notes_count") or 0), last_commit=(pipeline or {}).get("created_at"),
            issues=[Issue(i["iid"], i["title"], i["web_url"]) for i in self._get_or_none(f"{base}/closes_issues") or []],
            association=self._access(author.get("username") or ""), decision=decision, mergeable=known(mergeable),
            threads=threads_cap, checks=checks,
            truncated={"files": (len(files), int(str(detail.get("changes_count", "0")).rstrip("+") or 0))}
            if str(detail.get("changes_count", "")).endswith("+") else {},
        )  # fmt: skip

    def merged_prs(self, count: int) -> tuple[str, list[MergedPullRequest]]:
        raw = self._pages(
            f"projects/{self.project}/merge_requests", limit=count, state="merged", order_by="updated_at", sort="desc"
        )
        merged = []
        for mr in raw:
            changes = self._pages(f"projects/{self.project}/merge_requests/{mr['iid']}/diffs", limit=100)
            author = mr.get("author") or {}
            merged.append(MergedPullRequest(
                mr["iid"], Author(author.get("username") or "ghost", bool(author.get("bot"))),
                self._access(author.get("username") or ""),
                [ChangedFile(c.get("new_path") or c.get("old_path"), *_count_lines(c.get("diff") or "")) for c in changes],
            ))  # fmt: skip
        return self._branch(), merged

    def fetch_diff(self, number: int) -> str:
        resp = self.retrying(f"MR diff !{number}", lambda: self.rest.call(
            "GET", f"projects/{self.project}/merge_requests/{number}/raw_diffs", headers={"Accept": "text/plain"},
        ))  # fmt: skip
        if resp is None:
            raise TooLarge(f"MR diff !{number}: not served", what=f"MR diff !{number}")
        return resp.text

    # ---- CI

    def ci_context(self, bases: set[str]) -> CiContext:
        branch = self._branch()
        pipelines = (
            self._get_or_none(
                f"projects/{self.project}/pipelines", ref=branch, per_page=1, order_by="updated_at", sort="desc"
            )
            or []
        )
        failing: set[str] = {"pipeline"} if pipelines and pipelines[0].get("status") == "failed" else set()
        required = {b: unsupported("GitLab has no named required checks; the pipeline must pass") for b in bases}
        return CiContext(required, known(failing), branch)


def _count_lines(diff: str) -> tuple[int, int]:
    add = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    dele = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return add, dele

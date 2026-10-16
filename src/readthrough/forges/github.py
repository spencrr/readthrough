"""GitHub, through the ``gh`` CLI so your existing sign-in, SSO, and proxy settings are reused.

Each repo can name which signed-in ``gh`` account to use (an enterprise-managed account for internal repos,
a personal one for public repos). The token is passed per call, so your active ``gh`` account never
changes. PRs come from one GraphQL query per page, with follow-up pages for PRs that have more files,
review threads, or checks than fit, up to the caps in ``[tuning.forge]``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from readthrough.ci import failing_names
from readthrough.faults import AuthError, ForgeError, NotFound
from readthrough.forges import FORGES, CiContext, Forge
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
from readthrough.process import Secret, quiet_output, remember, run

log = logging.getLogger(__name__)

REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

CHECK_FIELDS = (
    "__typename ... on CheckRun{name conclusion status detailsUrl startedAt completedAt}"
    " ... on StatusContext{context state targetUrl createdAt}"
)
FILE_FIELDS = "path additions deletions changeType"
THREAD_FIELDS = "isResolved isOutdated path line comments(first:2){nodes{author{login} body bodyHTML}}"
PAGE = "pageInfo{hasNextPage endCursor} totalCount"

PR_QUERY = """
query PullRequests($owner:String!,$name:String!,$cursor:String,$size:Int!){
  viewer{login}
  repository(owner:$owner,name:$name){
    pullRequests(states:OPEN,first:$size,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}){
      pageInfo{hasNextPage endCursor} totalCount
      nodes{
        number title url body bodyHTML isDraft createdAt updatedAt additions deletions changedFiles
        headRefOid baseRefName reviewDecision mergeable authorAssociation
        author{__typename login}
        labels(first:10){nodes{name}}
        files(first:100){__PAGE__ nodes{__FILES__}}
        reviewRequests(first:10){nodes{requestedReviewer{... on User{login} ... on Team{name}}}}
        latestReviews(first:20){nodes{author{login} state submittedAt}}
        reviewThreads(first:60){__PAGE__ nodes{__THREADS__}}
        comments{totalCount}
        commits(last:1){nodes{commit{committedDate statusCheckRollup{state contexts(first:100){__PAGE__ nodes{__CHECKS__}}}}}}
        closingIssuesReferences(first:5){nodes{number title url}}
      }
    }
  }
}
"""

# Follow-up pages for the PRs that have more than the first page of files, review threads, or checks.
FILES_QUERY = """
query PullRequestFiles($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){pullRequest(number:$number){files(first:100,after:$cursor){__PAGE__ nodes{__FILES__}}}}
}
"""
THREADS_QUERY = """
query PullRequestThreads($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){__PAGE__ nodes{__THREADS__}}}}
}
"""
CHECKS_QUERY = """
query CommitChecks($owner:String!,$name:String!,$oid:GitObjectID!,$cursor:String){
  repository(owner:$owner,name:$name){object(oid:$oid){... on Commit{statusCheckRollup{contexts(first:100,after:$cursor){__PAGE__ nodes{__CHECKS__}}}}}}
}
"""
MAIN_QUERY = """
query DefaultBranch($owner:String!,$name:String!){
  repository(owner:$owner,name:$name){
    defaultBranchRef{name target{... on Commit{oid statusCheckRollup{state contexts(first:100){__PAGE__ nodes{__CHECKS__}}}}}}
  }
}
"""
MERGED_QUERY = """
query MergedPullRequests($owner:String!,$name:String!,$count:Int!){
  repository(owner:$owner,name:$name){
    defaultBranchRef{name}
    pullRequests(states:MERGED,last:$count,orderBy:{field:UPDATED_AT,direction:ASC}){
      nodes{number author{login __typename} authorAssociation files(first:100){nodes{path additions deletions}}}
    }
  }
}
"""


def _fill(query: str) -> str:
    return (query.replace("__PAGE__", PAGE).replace("__FILES__", FILE_FIELDS)
            .replace("__THREADS__", THREAD_FIELDS).replace("__CHECKS__", CHECK_FIELDS))  # fmt: skip


PR_QUERY, FILES_QUERY, THREADS_QUERY, CHECKS_QUERY, MAIN_QUERY = map(
    _fill, (PR_QUERY, FILES_QUERY, THREADS_QUERY, CHECKS_QUERY, MAIN_QUERY)
)


# ---------------------------------------------------------------- accounts

_tokens: dict[str, Secret] = {}
_tokens_lock = threading.Lock()


def token_for(account: str) -> Secret:
    """The stored token for a signed-in ``gh`` account (``gh auth token --user``). Cached per process."""
    with _tokens_lock:
        if account not in _tokens:
            out = quiet_output(["gh", "auth", "token", "--user", account])
            if not out or not out.strip():
                raise ForgeError(f"gh isn't signed in as {account}. Run: gh auth login (then pick that account)")
            _tokens[account] = remember(Secret(out.strip()))
        return _tokens[account]


def signed_in_accounts() -> list[str]:
    out = quiet_output(["gh", "auth", "status"])
    found: list[str] = []
    for line in (out or "").splitlines():
        if "Logged in to github.com account" in line:
            name = line.split("account", 1)[1].split()[0]
            if name not in found:
                found.append(name)
    return found


# ---------------------------------------------------------------- the adapter


@dataclass(frozen=True)
class GitHubOptions:
    host: str = "github.com"  # a GitHub Enterprise Server host goes here; gh must be signed in to it


@FORGES.add("github", "GitHub and GitHub Enterprise, through the gh CLI", options=GitHubOptions)
class GitHub(Forge):
    name = "github"
    label = "GitHub"
    config_dir = ".github"
    capabilities = frozenset({"diffs", "required_checks", "default_branch_ci", "rate_limit", "memberships", "accounts"})

    def __init__(self, repo: str, options: Any = None, tuning: Any = None, *, account: str | None = None) -> None:
        super().__init__(repo, options or GitHubOptions(), tuning, account=account)
        self.owner, self.repo_name = repo.split("/")
        self.host: str = getattr(self.options, "host", "github.com")

    @classmethod
    def validate_repo(cls, repo: str) -> str | None:
        return None if REPO_RE.match(repo) else f"{repo!r} isn't owner/name"

    def web_url(self) -> str:
        return f"https://{self.host}/{self.repo}"

    def pr_url(self, number: int) -> str:
        return f"{self.web_url()}/pull/{number}"

    def pr_ref(self, number: int) -> str:
        return f"pull/{number}/head"

    def file_url(self, path: str, branch: str) -> str:
        return f"{self.web_url()}/blob/{branch}/{path}"

    def new_file_url(self, path: str, branch: str) -> str | None:
        from urllib.parse import quote

        return f"{self.web_url()}/new/{quote(branch, safe='')}?filename={quote(path)}"

    def describe_auth(self) -> str:
        return f"gh as {self.account}" if self.account else "gh (active account)"

    # ---- gh calls

    def run(self, cmd: list[str], *, check: bool = True) -> str:
        """Run ``gh`` as this repo's account. Temporary failures are retried with backoff."""
        env = {"GH_TOKEN": token_for(self.account).reveal()} if self.account and cmd[:1] == ["gh"] else None
        if self.host != "github.com" and cmd[:1] == ["gh"]:
            env = {**(env or {}), "GH_HOST": self.host}
        return self.retrying(
            " ".join(cmd[:3]), lambda: run(cmd, check=check, env=env, timeout=self.tuning.timeout_seconds)
        )

    def api(self, path: str) -> str:
        return self.run(["gh", "api", path])

    def api_or_none(self, path: str, *, absent: tuple[type[ForgeError], ...] = (NotFound,)) -> str | None:
        """``None`` only when GitHub says the thing isn't there. Network and other failures still raise."""
        try:
            return self.api(path)
        except absent:
            return None

    def raw(self, path: str) -> str:
        return self.run(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{self.repo}/contents/{path}"])

    def graphql(self, query: str, **variables: str | int) -> dict[str, Any]:
        cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            # -F makes gh guess the type ("123" becomes a number), so only real integers use it.
            cmd += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        data = json.loads(self.run(cmd))
        if data.get("errors") and not data.get("data"):
            raise ForgeError(f"graphql: {data['errors'][0].get('message', data['errors'])}", what="graphql")
        return data["data"]

    # ---- the repo

    def repo_info(self) -> dict[str, Any]:
        info = json.loads(self.api(f"repos/{self.repo}"))
        return {k: info.get(k) for k in ("full_name", "default_branch", "private", "description")}

    def rate_limit(self) -> dict[str, int] | None:
        """Remaining REST and GraphQL calls (checking doesn't count against the limit)."""
        try:
            res = json.loads(self.api("rate_limit"))["resources"]
        except (ForgeError, ValueError, KeyError):
            return None
        return {"rest": res["core"]["remaining"], "graphql": res["graphql"]["remaining"]}

    def tree(self, branch: str) -> tuple[list[str], bool]:
        data = json.loads(self.api(f"repos/{self.repo}/git/trees/{branch}?recursive=1"))
        paths = [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]
        return paths[: self.tuning.max_tree_files], not data.get("truncated") and len(
            paths
        ) <= self.tuning.max_tree_files

    def read_file(self, path: str) -> str | None:
        try:
            return self.raw(path)
        except NotFound:
            return None

    def read_files(self, paths: list[str]) -> dict[str, str]:
        """Several files in one GraphQL call; a null object is a real "doesn't exist", an error still raises."""
        if not paths:
            return {}
        fields = " ".join(f'f{i}: object(expression:"HEAD:{p}"){{... on Blob{{text}}}}' for i, p in enumerate(paths))
        query = f"query Files($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{{fields}}}}}"
        repo = self.graphql(query, owner=self.owner, name=self.repo_name)["repository"]
        return {
            p: repo[f"f{i}"]["text"]
            for i, p in enumerate(paths)
            if repo.get(f"f{i}") and repo[f"f{i}"].get("text") is not None
        }

    def list_dir(self, path: str) -> list[str]:
        out = self.api_or_none(f"repos/{self.repo}/contents/{path}")
        listing = json.loads(out) if out and out.strip().startswith("[") else []
        return [e["name"] for e in listing if e.get("type") == "file"]

    def is_member(self, group: str, login: str) -> bool:
        """Is ``login`` in the org (``"org"``) or team (``"org/team-slug"``)? Only a 404 means no; other failures raise."""
        org, _, team = group.partition("/")
        try:
            if not team:
                self.api(f"orgs/{org}/members/{login}")  # 204 if a member visible to you, else 404
                return True
            out = self.api(f"orgs/{org}/teams/{team}/memberships/{login}")
            return json.loads(out or "{}").get("state") == "active"
        except NotFound:
            return False

    # ---- PRs

    def fetch_prs(self, limit: int) -> tuple[str, list[PullRequest], int]:
        prs: list[dict[str, Any]] = []
        cursor: str | None = None
        viewer, total = "", 0
        while len(prs) < limit:
            extra: dict[str, str | int] = {"cursor": cursor} if cursor else {}
            size = min(self.tuning.pr_page_size, limit - len(prs))
            data = self.graphql(PR_QUERY, owner=self.owner, name=self.repo_name, size=size, **extra)
            viewer = data["viewer"]["login"]
            page = data["repository"]["pullRequests"]
            total = page["totalCount"]
            prs.extend(page["nodes"])
            log.info("fetched %d of %d open PRs", len(prs), total)
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        prs = prs[:limit]
        pool_map(self.complete_pr, [p for p in prs if _needs_more(p)], self.tuning.concurrency)
        return viewer, [normalize(p) for p in prs], total

    def complete_pr(self, pr: dict[str, Any]) -> None:
        """Fetch the rest of a PR's files, review threads, and checks, up to the configured caps.

        Whatever is still cut off is recorded under ``pr["truncated"]`` so the dashboard can say so.
        """
        n = pr["number"]
        caps = {"files": self.tuning.max_files_per_pr, "reviewThreads": self.tuning.max_review_threads_per_pr}
        for field, query in (("files", FILES_QUERY), ("reviewThreads", THREADS_QUERY)):
            conn = pr[field]
            self._follow(conn, caps[field], lambda c, q=query, f=field: self.graphql(
                q, owner=self.owner, name=self.repo_name, number=n, cursor=c)["repository"]["pullRequest"][f])  # fmt: skip
        commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
        rollup = commit.get("statusCheckRollup") or {}
        if rollup.get("contexts"):
            self._follow(rollup["contexts"], self.tuning.max_checks_per_pr, lambda c: self.graphql(
                CHECKS_QUERY, owner=self.owner, name=self.repo_name, oid=pr["headRefOid"], cursor=c,
            )["repository"]["object"]["statusCheckRollup"]["contexts"])  # fmt: skip
        truncated = {
            name: [len(conn["nodes"]), conn["totalCount"]]
            for name, conn in (("files", pr["files"]), ("review threads", pr["reviewThreads"]), ("checks", rollup.get("contexts")))
            if conn and conn.get("totalCount", 0) > len(conn["nodes"])
        }  # fmt: skip
        if truncated:
            pr["truncated"] = truncated

    @staticmethod
    def _follow(conn: dict[str, Any], cap: int, fetch: Any) -> None:
        while conn["pageInfo"]["hasNextPage"] and len(conn["nodes"]) < cap:
            page = fetch(conn["pageInfo"]["endCursor"])
            conn["nodes"].extend(page["nodes"])
            conn["pageInfo"] = page["pageInfo"]
        del conn["nodes"][cap:]

    def merged_prs(self, count: int) -> tuple[str, list[MergedPullRequest]]:
        data = self.graphql(MERGED_QUERY, owner=self.owner, name=self.repo_name, count=count)["repository"]
        branch = (data.get("defaultBranchRef") or {}).get("name") or "main"
        merged = [
            MergedPullRequest(
                number=pr["number"],
                author=_author(pr.get("author")),
                association=known(pr.get("authorAssociation") or "NONE"),
                files=[ChangedFile(f["path"], f["additions"], f["deletions"]) for f in pr["files"]["nodes"]],
            )
            for pr in data["pullRequests"]["nodes"]
        ]
        return branch, merged

    def fetch_diff(self, number: int) -> str:
        return self.run(["gh", "pr", "diff", str(number), "-R", self.repo])

    # ---- CI

    def required_checks(self, branch: str) -> Capability[set[str]]:
        """Required status checks for a branch, from classic protection and rulesets."""
        names: set[str] = set()
        seen_any = False
        try:
            out = self.api_or_none(f"repos/{self.repo}/branches/{branch}", absent=(NotFound, AuthError))
            if out:
                seen_any = True
                protection = (json.loads(out).get("protection") or {}).get("required_status_checks") or {}
                names.update(protection.get("contexts") or [])
                names.update(c["context"] for c in protection.get("checks") or [] if c.get("context"))
            out = self.api_or_none(f"repos/{self.repo}/rules/branches/{branch}", absent=(NotFound, AuthError))
            if out:
                seen_any = True
                for rule in json.loads(out):
                    if rule.get("type") == "required_status_checks":
                        params = rule.get("parameters") or {}
                        names.update(
                            c["context"] for c in params.get("required_status_checks") or [] if c.get("context")
                        )
        except ForgeError as exc:
            return unavailable(exc.reason)
        return known(names) if seen_any else unavailable("branch protection isn't visible to this account")

    def ci_context(self, bases: set[str]) -> CiContext:
        data = self.graphql(MAIN_QUERY, owner=self.owner, name=self.repo_name)
        ref = data["repository"]["defaultBranchRef"] or {}
        target = ref.get("target") or {}
        rollup = target.get("statusCheckRollup") or {}
        if rollup.get("contexts") and target.get("oid"):
            self._follow(rollup["contexts"], self.tuning.max_checks_per_pr, lambda c: self.graphql(
                CHECKS_QUERY, owner=self.owner, name=self.repo_name, oid=target["oid"], cursor=c,
            )["repository"]["object"]["statusCheckRollup"]["contexts"])  # fmt: skip

        main_failing = failing_names(_checks((rollup.get("contexts") or {}).get("nodes") or []))
        ordered = sorted(bases)
        required = dict(zip(ordered, pool_map(self.required_checks, ordered, self.tuning.concurrency), strict=True))
        return CiContext(required, known(main_failing), ref.get("name") or "main")


# ---------------------------------------------------------------- GraphQL -> model


def _needs_more(pr: dict[str, Any]) -> bool:
    commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
    contexts = (commit.get("statusCheckRollup") or {}).get("contexts") or {}
    return any(
        (conn or {}).get("pageInfo", {}).get("hasNextPage") for conn in (pr["files"], pr["reviewThreads"], contexts)
    )


def _author(raw: dict[str, Any] | None) -> Author:
    raw = raw or {}
    return Author(raw.get("login") or "ghost", bot=raw.get("__typename") == "Bot")


def _checks(nodes: list[dict[str, Any]]) -> list[Check]:
    out = []
    for n in nodes:
        if n.get("__typename") == "StatusContext":
            state = n.get("state") or "PENDING"
            done = state in ("SUCCESS", "FAILURE", "ERROR")
            out.append(Check(n.get("context") or "?", "COMPLETED" if done else "PENDING", state if done else None,
                             n.get("targetUrl"), n.get("createdAt")))  # fmt: skip
        else:
            out.append(Check(n.get("name") or "?", n.get("status") or "QUEUED", n.get("conclusion"), n.get("detailsUrl"),
                             n.get("startedAt"), n.get("completedAt")))  # fmt: skip
    return out


def normalize(pr: dict[str, Any]) -> PullRequest:
    """One PR from the GraphQL query (after any follow-up pages) as the neutral record."""
    commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
    rollup = commit.get("statusCheckRollup")
    requested = [
        (r.get("requestedReviewer") or {}).get("login") or (r.get("requestedReviewer") or {}).get("name")
        for r in pr["reviewRequests"]["nodes"]
    ]
    threads = [
        Thread(
            author=((t["comments"]["nodes"] or [{}])[0].get("author") or {}).get("login") or "ghost",
            body=(t["comments"]["nodes"] or [{}])[0].get("body") or "",
            body_html=(t["comments"]["nodes"] or [{}])[0].get("bodyHTML") or "",
            path=t.get("path"), line=t.get("line"), resolved=bool(t.get("isResolved")), outdated=bool(t.get("isOutdated")),
        )
        for t in pr["reviewThreads"]["nodes"]
    ]  # fmt: skip
    return PullRequest(
        number=pr["number"],
        title=pr["title"],
        url=pr["url"],
        body=pr.get("body") or "",
        body_html=pr.get("bodyHTML") or "",
        draft=bool(pr["isDraft"]),
        created=pr["createdAt"],
        updated=pr["updatedAt"],
        additions=pr["additions"],
        deletions=pr["deletions"],
        changed_files=pr["changedFiles"],
        head=pr["headRefOid"],
        base=pr["baseRefName"],
        author=_author(pr.get("author")),
        files=[ChangedFile(f["path"], f["additions"], f["deletions"], f.get("changeType") or "MODIFIED") for f in pr["files"]["nodes"]],
        labels=[label["name"] for label in pr["labels"]["nodes"]],
        requested=[r for r in requested if r],
        reviews=[Review(r["author"]["login"], r["state"], r.get("submittedAt")) for r in pr["latestReviews"]["nodes"] if r.get("author")],
        comments=pr["comments"]["totalCount"],
        last_commit=commit.get("committedDate"),
        issues=[Issue(i["number"], i["title"], i["url"]) for i in pr["closingIssuesReferences"]["nodes"]],
        association=known(pr.get("authorAssociation") or "NONE"),
        decision=known(pr.get("reviewDecision") or "NONE"),
        mergeable=known(pr.get("mergeable") or "UNKNOWN"),
        threads=known(threads),
        # A commit with no rollup has no checks at all: that's a known empty, which `ci.classify` calls "not run".
        checks=known(_checks(((rollup or {}).get("contexts") or {}).get("nodes") or [])),
        truncated={k: (v[0], v[1]) for k, v in (pr.get("truncated") or {}).items()},
    )  # fmt: skip

"""Azure DevOps Repos, through the REST API. A repo is ``organization/project/repository``.

The token comes from ``AZURE_DEVOPS_EXT_PAT`` if set (the variable the ``az devops`` extension reads too),
else from a signed-in ``az`` CLI (``az account get-access-token`` for the Azure DevOps resource), else calls
are anonymous, which works for public projects.

Azure DevOps serves no unified diff and reports no line counts, so a PR's diff is assembled here from both
versions of each changed file (two blob requests per file, capped by ``max_assembled_diff_files``), and the
build counts lines from it. It has no author association either, so one is derived: everyone who opens a PR
holds contribute rights (GitHub's COLLABORATOR), and the completed-PR history tells a returning author from
a first-timer; the organization's users and a project team's members (through nested groups, via the
identities API on the ``vssps`` host) back the ``memberships`` capability. There is no "closes issues" link
readthrough can read. Reviewer votes map to review states (10/5 approve, -5 wait, -10 reject), branch
policies supply required build names, and the head commit's statuses are the checks.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

from readthrough.faults import AuthError, ForgeError, NotFound, TooLarge
from readthrough.forges import (
    FORGES,
    CiContext,
    Credential,
    FilePair,
    Forge,
    Rest,
    assemble_diff,
    cached_cli_token,
    credential,
)
from readthrough.logs import pool_map
from readthrough.model import (
    PATH_SEGMENT,
    Author,
    Capability,
    ChangedFile,
    Check,
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

API = "7.1"
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"  # the Azure DevOps application id, for az access tokens
VOTES = {10: "APPROVED", 5: "APPROVED", 0: "COMMENTED", -5: "COMMENTED", -10: "CHANGES_REQUESTED"}
STATUS = {
    "succeeded": "SUCCESS",
    "failed": "FAILURE",
    "error": "ERROR",
    "pending": None,
    "notSet": None,
    "notApplicable": "SKIPPED",
}


@dataclass(frozen=True)
class AzureDevOpsOptions:
    host: str = "dev.azure.com"
    credential_env: str = "AZURE_DEVOPS_EXT_PAT"


def _az_token() -> str | None:
    def ask() -> str | None:
        out = quiet_output(
            ["az", "account", "get-access-token", "--resource", ADO_RESOURCE, "--query", "accessToken", "-o", "tsv"]
        )
        return out.strip() if out else None

    return cached_cli_token("az", ask)


@FORGES.add("ado", "Azure DevOps Repos, through its REST API", options=AzureDevOpsOptions)
class AzureDevOps(Forge):
    name = "ado"
    label = "Azure DevOps"
    config_dir = ".azuredevops"
    shape = "organization/project/repository"
    capabilities = frozenset({"diffs", "required_checks", "default_branch_ci", "memberships"})  # diffs: from blobs

    def __init__(self, repo: str, options: Any = None, tuning: Any = None, *, account: str | None = None) -> None:
        super().__init__(repo, options or AzureDevOpsOptions(), tuning, account=account)
        self.host: str = self.options.host
        self.org, self.project, self.repo_name = repo.split("/")
        env = self.options.credential_env

        def find() -> Credential:
            # A PAT is sent as Basic auth; an az access token as Bearer.
            cred = credential(env=env, cli=None, cli_name="", scheme="Basic")
            return cred if cred.token else credential(env="", cli=_az_token, cli_name="az")

        self.rest = Rest(f"https://{self.host}/{quote(self.org)}/", find, self.tuning)
        # Identities and the graph live on another host, which gets the same credential (a Rest sends its
        # Authorization header only to its own host) and follows when the main one steps aside to anonymous.
        self.vssps = Rest(f"https://vssps.{self.host}/{quote(self.org)}/", lambda: self.rest.cred, self.tuning)
        self._default_branch: str | None = None
        self._repo_id: str | None = None
        self._merge_commits_of: dict[int, tuple[str, str]] = {}  # from fetch_prs, so fetch_diff needn't ask again
        self._association_of: dict[str, Capability[str]] = {}  # per author id, from their completed-PR history
        self._members_lock = threading.Lock()
        self._org_logins: set[str] | None = None
        self._team_descriptors: dict[str, str] = {}
        self._id_of: dict[str, str] = {}  # login -> identity id, from the PRs' authors

    def retrying(self, what: str, call: Any) -> Any:
        """Like the base, but an ``az`` identity the organization refuses steps aside for anonymous access.

        The az token is opportunistic (nobody configured it), and a public project answers anonymous calls
        that it refuses to a signed-in outsider. A token someone set on purpose is never second-guessed.
        """
        try:
            return super().retrying(what, call)
        except AuthError:
            if self.rest.cred.source != "az":
                raise
            log.warning("%s refuses the az identity; continuing anonymously (fine for a public project)", self.org)
            self.rest = Rest(self.rest.base, Credential("anonymous"), self.tuning)
            return super().retrying(what, call)

    @classmethod
    def validate_repo(cls, repo: str) -> str | None:
        # Project and repository names may contain spaces ("Delivery Team"); organizations may not.
        parts = repo.split("/")
        if len(parts) != 3 or not all(re.fullmatch(PATH_SEGMENT, p) for p in parts) or " " in parts[0]:
            return f"{repo!r} isn't organization/project/repository"
        return None

    @classmethod
    def remote_matches(cls, url: str, repo: str) -> bool:
        """``https://<org>@dev.azure.com/<org>/<project>/_git/<repo>`` or ``git@ssh.dev.azure.com:v3/<org>/<project>/<repo>``,
        with the project percent-encoded in the https form ("Delivery%20Team")."""
        org, project, name = (p.lower() for p in repo.split("/"))
        path = unquote(url).lower().rstrip("/")
        return path.endswith((f"/{org}/{project}/_git/{name}", f"v3/{org}/{project}/{name}"))

    def web_url(self) -> str:
        return f"https://{self.host}/{quote(self.org)}/{quote(self.project)}/_git/{quote(self.repo_name)}"

    def pr_url(self, number: int) -> str:
        return f"{self.web_url()}/pullrequest/{number}"

    def pr_ref(self, number: int) -> str:
        return f"pull/{number}/merge"

    def file_url(self, path: str, branch: str) -> str:
        return f"{self.web_url()}?path={quote('/' + path)}&version=GB{quote(branch)}"

    def describe_auth(self) -> str:
        return self.rest.cred.source

    # ---- REST helpers

    @property
    def _p(self) -> str:
        return f"{quote(self.project)}/_apis/git/repositories/{quote(self.repo_name)}"

    def _get(self, path: str, **params: Any) -> Any:
        params.setdefault("api-version", API)
        resp = self.retrying(f"GET {path}", lambda: self.rest.call("GET", path, params=params))
        return resp.json() if resp else None

    def _get_or_none(self, path: str, **params: Any) -> Any:
        params.setdefault("api-version", API)
        resp = self.retrying(f"GET {path}", lambda: self.rest.call("GET", path, params=params, absent=(NotFound,)))
        return resp.json() if resp else None

    def _branch(self) -> str:
        if self._default_branch is None:
            self._default_branch = self.repo_info()["default_branch"] or "main"
        return self._default_branch

    # ---- the repo

    def repo_info(self) -> dict[str, Any]:
        info = self._get(self._p)
        self._repo_id = info.get("id")
        branch = (info.get("defaultBranch") or "refs/heads/main").removeprefix("refs/heads/")
        return {"full_name": f"{self.org}/{self.project}/{info.get('name')}", "default_branch": branch,
                "private": (info.get("project") or {}).get("visibility") != "public", "description": None}  # fmt: skip

    def tree(self, branch: str) -> tuple[list[str], bool]:
        data = self._get(f"{self._p}/items", scopePath="/", recursionLevel="Full",
                         **{"versionDescriptor.version": branch, "versionDescriptor.versionType": "branch"})  # fmt: skip
        paths = [e["path"].lstrip("/") for e in data.get("value") or [] if not e.get("isFolder")]
        return paths[: self.tuning.max_tree_files], len(paths) <= self.tuning.max_tree_files

    def read_file(self, path: str) -> str | None:
        resp = self.retrying(f"GET item {path}", lambda: self.rest.call(
            "GET", f"{self._p}/items",
            params={"path": "/" + path, "api-version": API, "versionDescriptor.version": self._branch(),
                    "versionDescriptor.versionType": "branch", "includeContent": "true", "$format": "text"},
            headers={"Accept": "text/plain"}, absent=(NotFound,),
        ))  # fmt: skip
        return resp.text if resp else None

    def list_dir(self, path: str) -> list[str]:
        data = self._get_or_none(f"{self._p}/items", scopePath="/" + path, recursionLevel="OneLevel",
                                 **{"versionDescriptor.version": self._branch()}) or {}  # fmt: skip
        return [
            e["path"].rsplit("/", 1)[-1]
            for e in data.get("value") or []
            if not e.get("isFolder") and e["path"].strip("/") != path
        ]

    # ---- pull requests

    def fetch_prs(self, limit: int) -> tuple[str, list[PullRequest], int]:
        raw = (
            self._get(f"{self._p}/pullrequests", **{"searchCriteria.status": "active", "$top": limit}).get("value")
            or []
        )
        total = len(raw)
        if len(raw) == limit:  # ADO gives no total; count by paging through ids only when the page filled up
            skip, more = limit, True
            while more:
                page = (
                    self._get(
                        f"{self._p}/pullrequests", **{"searchCriteria.status": "active", "$top": 500, "$skip": skip}
                    ).get("value")
                    or []
                )
                total += len(page)
                skip += len(page)
                more = len(page) == 500
        raw.sort(key=lambda p: p.get("creationDate") or "", reverse=True)
        self._look_up_history({a for p in raw if (a := (p.get("createdBy") or {}).get("id"))})
        viewer = ""
        if self.rest.cred.token:
            me = self.rest.call("GET", "_apis/connectionData", absent=(ForgeError,))  # organization-scoped
            if me:
                user = (me.json() or {}).get("authenticatedUser") or {}
                viewer = (
                    (user.get("properties") or {}).get("Account", {}).get("$value")
                    or user.get("providerDisplayName")
                    or ""
                )
        return viewer, pool_map(self._complete, raw, self.tuning.concurrency), total

    # ---- authors

    def _look_up_history(self, author_ids: set[str]) -> None:
        """Whether each author has had a PR completed in this repo before, one query per author.

        Azure DevOps has no author association. What it can say is this: everyone who opens a PR holds
        Contribute rights on the repo (GitHub's COLLABORATOR), and the completed-PR history tells a returning
        author from one on their first PR here. Teams and the organization are checked through ``is_member``.
        """

        def landed(author_id: str) -> tuple[str, Capability[str]]:
            try:
                page = self._get(f"{self._p}/pullrequests", **{"searchCriteria.creatorId": author_id,
                                                               "searchCriteria.status": "completed", "$top": 1})  # fmt: skip
            except ForgeError as exc:
                return author_id, unavailable(f"couldn't read the author's PR history: {exc.reason}")
            return author_id, known("COLLABORATOR" if page.get("value") else "FIRST_TIME_CONTRIBUTOR")

        wanted = [a for a in author_ids if a not in self._association_of]
        self._association_of.update(pool_map(landed, wanted, self.tuning.concurrency))

    def _association(self, pr: dict[str, Any]) -> Capability[str]:
        author_id = (pr.get("createdBy") or {}).get("id")
        if author_id is None:
            return unavailable("the PR names no author")
        if author_id not in self._association_of:
            self._look_up_history({author_id})
        return self._association_of[author_id]

    def is_member(self, group: str, login: str) -> bool:
        """``<organization>``: the author is one of the organization's users. ``<organization or project>/<team>``:
        a member of that team in this repo's project, through any nesting of groups. Only a definite answer
        is returned; a login or team that can't be resolved raises, so the check counts as failed, not as "no"."""
        want = login.lower()
        if group.lower() == self.org.lower():
            return want in self._org_users()
        scope, _, team = group.partition("/")
        if not team or scope.lower() not in (self.org.lower(), self.project.lower()):
            raise ForgeError(
                f"{group!r} isn't this organization or one of its project's teams", what=f"members {group}"
            )
        # The identities API expands a user's containers recursively ("ExpandedUp"), so one call says whether
        # the team is among them, however the team is composed (users, groups, groups of groups).
        ident = self._identities(identityIds=self._identity_id(want), queryMembership="ExpandedUp")
        return self._team_descriptor(team) in ((ident[0].get("memberOf") or []) if ident else [])

    def _identities(self, **params: Any) -> list[dict[str, Any]]:
        params.setdefault("api-version", API)
        resp = self.retrying("GET identities", lambda: self.vssps.call("GET", "_apis/identities", params=params))
        return ((resp.json() if resp else None) or {}).get("value") or []

    def _identity_id(self, login: str) -> str:
        """The identity id behind a login: remembered from the PRs fetched this build, else looked up."""
        if login in self._id_of:
            return self._id_of[login]
        for ident in self._identities(searchFilter="General", filterValue=login):
            names = {str(v).lower() for v in (
                ((ident.get("properties") or {}).get("Account") or {}).get("$value"),
                ((ident.get("properties") or {}).get("Mail") or {}).get("$value"),
            ) if v}  # fmt: skip
            if login in names and ident.get("id"):
                self._id_of[login] = str(ident["id"])
                return self._id_of[login]
        raise ForgeError(f"no identity found for {login}", what=f"identity {login}")

    def _team_descriptor(self, team: str) -> str:
        with self._members_lock:
            if team.lower() not in self._team_descriptors:
                teams = self._get(f"_apis/projects/{quote(self.project)}/teams", **{"$top": 500}).get("value") or []
                match = next((t for t in teams if (t.get("name") or "").lower() == team.lower()), None)
                if match is None:
                    raise ForgeError(f"no team named {team!r} in {self.project}", what=f"members {team}")
                ident = self._identities(identityIds=match["id"])
                if not ident or not ident[0].get("descriptor"):
                    raise ForgeError(f"team {team!r} has no identity descriptor", what=f"members {team}")
                self._team_descriptors[team.lower()] = str(ident[0]["descriptor"])
            return self._team_descriptors[team.lower()]

    def _org_users(self) -> set[str]:
        with self._members_lock:
            if self._org_logins is None:
                logins: set[str] = set()
                token: str | None = None
                while True:
                    params: dict[str, Any] = {"api-version": "7.1-preview.1", "subjectTypes": "aad,msa"}
                    if token:
                        params["continuationToken"] = token
                    resp = self.retrying(
                        "GET graph/users", lambda p=params: self.vssps.call("GET", "_apis/graph/users", params=p)
                    )
                    for user in ((resp.json() if resp else None) or {}).get("value") or []:
                        logins |= {str(v).lower() for v in (user.get("principalName"), user.get("mailAddress")) if v}
                    token = resp.headers.get("x-ms-continuationtoken") if resp else None
                    if not token:
                        break
                self._org_logins = logins
            return self._org_logins

    @staticmethod
    def _merge_commits(pr: dict[str, Any]) -> tuple[str, str] | None:
        """The commits a PR's changes are the difference between: its last merge base and its head."""
        base = (pr.get("lastMergeTargetCommit") or {}).get("commitId")
        head = (pr.get("lastMergeSourceCommit") or {}).get("commitId")
        return (base, head) if base and head else None

    def _changes(self, base: str, head: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        """The blob changes between two commits, as Azure DevOps lists them (path, change type, object ids).

        This is the one changes listing a public project serves without sign-in (iterations need it).
        """
        blobs: list[dict[str, Any]] = []
        skip, complete = 0, True
        while True:
            data = self._get(f"{self._p}/diffs/commits", baseVersion=base, baseVersionType="commit", targetVersion=head,
                             targetVersionType="commit", **{"$top": min(1000, limit), "$skip": skip})  # fmt: skip
            changes = data.get("changes") or []
            blobs += [c for c in changes if not (c.get("item") or {}).get("isFolder")
                      and (c.get("item") or {}).get("gitObjectType", "blob") == "blob"]  # fmt: skip
            if data.get("allChangesIncluded", True) or not changes:
                break
            skip += len(changes)
            if len(blobs) >= limit:
                complete = False
                break
        return blobs[:limit], complete

    @staticmethod
    def _paths(change: dict[str, Any]) -> tuple[str | None, str | None]:
        """(old path, new path) of a change; a deletion has no new path and an addition no old one."""
        kind = str(change.get("changeType", "")).split(",")[0].strip()
        new = ((change.get("item") or {}).get("path") or "").lstrip("/") or None
        old = (change.get("originalPath") or "").lstrip("/") or None
        if kind == "delete":
            return old or new, None
        if kind == "add":
            return None, new
        return old or new, new

    def _changed_files(self, pr: dict[str, Any], limit: int) -> tuple[list[ChangedFile], bool]:
        """The paths a PR changes. Azure DevOps reports no line counts anywhere, so additions and deletions stay 0
        here and the record says so; the build counts them from the assembled diff when it has one."""
        commits = self._merge_commits(pr)
        if commits is None:
            return [], True
        changes, complete = self._changes(*commits, limit)
        files = []
        for c in changes:
            kind = str(c.get("changeType", "")).split(",")[0].strip()
            old, new = self._paths(c)
            files.append(ChangedFile(new or old or "", 0, 0, {"add": "ADDED", "delete": "DELETED", "rename": "RENAMED"}.get(kind, "MODIFIED")))  # fmt: skip
        return files, complete

    def _complete(self, pr: dict[str, Any]) -> PullRequest:
        n = pr["pullRequestId"]
        base = f"{self._p}/pullrequests/{n}"
        if commits := self._merge_commits(pr):
            self._merge_commits_of[n] = commits
        files, complete = self._changed_files(pr, self.tuning.max_files_per_pr)
        truncated = {} if complete else {"files": (len(files), len(files) + 1)}
        reviews = [Review(r.get("uniqueName") or r.get("displayName") or "?", VOTES.get(int(r.get("vote") or 0), "COMMENTED"), None)
                   for r in pr.get("reviewers") or [] if not r.get("isContainer")]  # fmt: skip
        threads: Capability[list[Thread]]
        last_activity: list[str] = []
        try:
            raw_threads = self._get(f"{base}/threads").get("value") or []
            last_activity += [t["lastUpdatedDate"] for t in raw_threads if t.get("lastUpdatedDate")]
            threads = known([
                Thread((t["comments"][0].get("author") or {}).get("uniqueName") or "?", t["comments"][0].get("content") or "", "",
                       ((t.get("threadContext") or {}).get("filePath") or "/").lstrip("/") or None,
                       ((t.get("threadContext") or {}).get("rightFileStart") or {}).get("line"),
                       t.get("status") in ("fixed", "closed", "wontFix", "byDesign"), False)
                for t in raw_threads if t.get("comments") and t.get("threadContext") and not t.get("isDeleted")
                and t["comments"][0].get("commentType") == "text"
            ])  # fmt: skip
        except ForgeError as exc:
            threads = unavailable(exc.reason)
        head = (pr.get("lastMergeSourceCommit") or {}).get("commitId") or ""
        last_commit = None
        if head:
            commits = (self._get_or_none(f"{base}/commits", **{"$top": 1}) or {}).get("value") or []
            last_commit = (commits[0].get("committer") or {}).get("date") if commits else None
            last_activity += [last_commit] if last_commit else []
        checks: Capability[list[Check]]
        try:
            statuses = self._get_or_none(f"{self._p}/commits/{head}/statuses") if head else None
            checks = known([Check(f"{(s.get('context') or {}).get('genre') or ''}/{(s.get('context') or {}).get('name') or '?'}".strip("/"),
                                  "COMPLETED" if s.get("state") not in ("pending", "notSet") else "PENDING",
                                  STATUS.get(s.get("state") or ""), s.get("targetUrl"), s.get("creationDate"))
                            for s in (statuses or {}).get("value") or []])  # fmt: skip
        except ForgeError as exc:
            checks = unavailable(exc.reason)
        states = {r.state for r in reviews}
        decision = ("CHANGES_REQUESTED" if "CHANGES_REQUESTED" in states else "APPROVED" if "APPROVED" in states
                    else "REVIEW_REQUIRED")  # fmt: skip
        merge_status = pr.get("mergeStatus") or "notSet"
        mergeable = (
            "CONFLICTING" if merge_status == "conflicts" else "MERGEABLE" if merge_status == "succeeded" else "UNKNOWN"
        )
        login = self._login(pr)
        return PullRequest(
            number=n, title=pr["title"], url=self.pr_url(n), body=pr.get("description") or "", draft=bool(pr.get("isDraft")),
            created=pr["creationDate"], updated=max([pr["creationDate"], *last_activity]),
            additions=0, deletions=0, changed_files=len(files), head=head,
            base=(pr.get("targetRefName") or "refs/heads/main").removeprefix("refs/heads/"),
            author=Author(login, bot=False), files=files, labels=[label["name"] for label in pr.get("labels") or []],
            requested=[r.get("uniqueName") or r.get("displayName") or "?" for r in pr.get("reviewers") or [] if r.get("isRequired")],
            reviews=reviews, comments=len(threads.value or []) if threads.known else 0,
            last_commit=last_commit, issues=[],
            association=self._association(pr),
            decision=known(decision), mergeable=known(mergeable), threads=threads, checks=checks,
            line_counts=unsupported("Azure DevOps reports no per-file line counts; they are counted from the diff when one is fetched"),
            truncated=truncated,
        )  # fmt: skip

    def merged_prs(self, count: int) -> tuple[str, list[MergedPullRequest]]:
        raw = (
            self._get(f"{self._p}/pullrequests", **{"searchCriteria.status": "completed", "$top": count}).get("value")
            or []
        )
        self._look_up_history({a for p in raw if (a := (p.get("createdBy") or {}).get("id"))})
        merged = []
        for pr in raw:
            files, _ = self._changed_files(pr, 100)
            merged.append(MergedPullRequest(pr["pullRequestId"], Author(self._login(pr)), self._association(pr), files))
        return self._branch(), merged

    def _login(self, pr: dict[str, Any]) -> str:
        """The author's login (their unique name, an email on Azure DevOps), remembering which identity it is."""
        author = pr.get("createdBy") or {}
        login = author.get("uniqueName") or author.get("displayName") or "ghost"
        if author.get("id"):
            self._id_of[login.lower()] = str(author["id"])
        return login

    def fetch_diff(self, number: int) -> str:
        """A unified diff assembled from both versions of each changed file; Azure DevOps serves none itself.

        Two blob requests per file, so a PR changing more than ``max_assembled_diff_files`` files is treated as
        too large, the way a diff GitHub refuses is: its files still place it on the map, only the line
        heuristics are skipped for it.
        """
        what = f"pr diff #{number}"
        commits = self._merge_commits_of.get(number) or self._merge_commits(
            self._get(f"{self._p}/pullrequests/{number}")
        )
        if commits is None:
            return ""  # no merge base yet: nothing to diff against
        cap = self.tuning.max_assembled_diff_files
        changes, complete = self._changes(*commits, cap + 1)
        if not complete or len(changes) > cap:
            raise TooLarge(f"{what}: changes more than {cap} files, the most assembled from blobs", what=what)

        def blob(object_id: str | None) -> bytes | None:
            if not object_id:
                return None
            resp = self.retrying(f"GET blob {object_id[:12]}", lambda: self.rest.call(
                "GET", f"{self._p}/blobs/{object_id}", params={"api-version": API, "$format": "octetStream"},
                headers={"Accept": "application/octet-stream"},
            ))  # fmt: skip
            return resp.content if resp else b""

        def pair(change: dict[str, Any]) -> FilePair:
            old_path, new_path = self._paths(change)
            item = change.get("item") or {}
            return FilePair(old_path, new_path, blob(item.get("originalObjectId")) if old_path else None,
                            blob(item.get("objectId")) if new_path else None)  # fmt: skip

        return assemble_diff(pool_map(pair, changes, self.tuning.concurrency))

    # ---- CI

    def ci_context(self, bases: set[str]) -> CiContext:
        branch = self._branch()
        required: dict[str, Capability[set[str]]] = {}
        try:
            if self._repo_id is None:
                self.repo_info()
            data = self._get(f"{quote(self.project)}/_apis/policy/configurations", **{"$top": 500})
            for base in bases:
                names: set[str] = set()
                for cfg in data.get("value") or []:
                    if not cfg.get("isEnabled") or (cfg.get("type") or {}).get("displayName") != "Build":
                        continue
                    for scope in (cfg.get("settings") or {}).get("scope") or []:
                        if (
                            scope.get("repositoryId") in (None, self._repo_id)
                            and (scope.get("refName") or "").removeprefix("refs/heads/") == base
                        ):
                            names.add(
                                (cfg.get("settings") or {}).get("displayName")
                                or f"build {(cfg.get('settings') or {}).get('buildDefinitionId')}"
                            )
                required[base] = known(names)
        except ForgeError as exc:
            required = {b: unavailable(exc.reason) for b in bases}
        failing: Capability[set[str]]
        try:
            statuses = (
                self._get_or_none(f"{self._p}/commits", **{"searchCriteria.itemVersion.version": branch, "$top": 1})
                or {}
            )
            head = ((statuses.get("value") or [{}])[0]).get("commitId")
            checks = self._get_or_none(f"{self._p}/commits/{head}/statuses") if head else None
            failing = known({f"{(s.get('context') or {}).get('genre') or ''}/{(s.get('context') or {}).get('name') or ''}".strip("/")
                             for s in (checks or {}).get("value") or [] if s.get("state") in ("failed", "error")})  # fmt: skip
        except ForgeError as exc:
            failing = unavailable(exc.reason)
        return CiContext(required, failing, branch)

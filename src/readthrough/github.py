"""Everything that talks to GitHub, via the ``gh`` CLI so we reuse your existing sign-in.

Each repo can name which signed-in ``gh`` account to use (an enterprise-managed account for internal
repos, a personal one for public repos). The token is passed per call, so your active ``gh`` account
never changes.

Failures are classified (network, rate limit, auth, not found, too large, timeout). Temporary ones are
retried with backoff; "not found" is the only failure that means "absent", so an outage is never mistaken
for an answer. All calls share one concurrency limit across repos, and every call has a timeout.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from readthrough.ci import failing_names
from readthrough.logs import pool_map
from readthrough.profile import Profile
from readthrough.rules import Contract, Instruction, contracts_for, parse_instruction
from readthrough.tuning import GitHubTuning, default_tuning

log = logging.getLogger(__name__)

# Keep child consoles from flashing when we run windowless (scheduled task, URL handler).
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------- errors


class GhError(RuntimeError):
    """A failed ``gh`` or ``git`` command. Subclasses say why, and whether trying again could help."""

    retryable = False
    kind = "error"

    def __init__(self, message: str, *, what: str = "", stderr: str = "", returncode: int | None = None) -> None:
        super().__init__(message)
        self.what, self.stderr, self.returncode = what, stderr, returncode

    @property
    def reason(self) -> str:
        """The first meaningful line of the tool's error output, for one-line logs."""
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        return (lines[0] if lines else str(self))[:300]


class GhNetworkError(GhError):
    retryable, kind = True, "network"


class GhTimeout(GhNetworkError):
    kind = "timeout"


class GhRateLimited(GhError):
    retryable, kind = True, "rate-limit"


class GhAuthError(GhError):
    kind = "auth"


class GhNotFound(GhError):
    kind = "not-found"


class GhTooLarge(GhError):
    kind = "too-large"


_PATTERNS: list[tuple[type[GhError], re.Pattern[str]]] = [
    (GhRateLimited, re.compile(r"HTTP 429|rate limit|abuse detection", re.IGNORECASE)),
    (GhTooLarge, re.compile(r"HTTP 406|exceeded the maximum number of|diff is taking too long|too large to", re.IGNORECASE)),
    (GhNotFound, re.compile(r"HTTP 404|Not Found|Could not resolve to a|no pull requests? found", re.IGNORECASE)),
    (GhAuthError, re.compile(r"HTTP 401|HTTP 403|Bad credentials|gh auth login|SAML|single sign-on|must authenticate", re.IGNORECASE)),
    (GhNetworkError, re.compile(
        r"error connecting to|connection (?:reset|refused)|forcibly closed|actively refused|proxyconnect|dial tcp|"
        r"connectex|network is unreachable|no route to host|wsarecv|wsasend|broken pipe|i/o timeout|"
        r"timeout awaiting|TLS handshake|unexpected EOF|\bEOF\b|no such host|HTTP 50[0234]|Bad Gateway|"
        r"Service Unavailable|Gateway Time-?out|Something went wrong while executing|stream error", re.IGNORECASE)),
]  # fmt: skip


def classify(stderr: str) -> type[GhError]:
    for cls, pattern in _PATTERNS:
        if pattern.search(stderr):
            return cls
    return GhError


def describe(cmd: list[str]) -> str:
    """A short, readable name for a command: `graphql PullRequests`, `GET repos/o/r/branches/main`, `pr diff #12`."""
    if cmd[:3] == ["gh", "api", "graphql"]:
        query = next((a[6:] for a in cmd if a.startswith("query=")), "")
        name = re.search(r"query\s+(\w+)", query)
        number = next((a.split("=", 1)[1] for a in cmd if a.startswith("number=")), "")
        return f"graphql {name.group(1) if name else 'query'}{' #' + number if number else ''}"
    if cmd[:2] == ["gh", "api"]:
        method = cmd[cmd.index("-X") + 1] if "-X" in cmd else "GET"
        path = next((a for a in reversed(cmd[2:]) if not a.startswith("-") and ":" not in a), "?")
        return f"{method} {path}"
    if cmd[:3] == ["gh", "pr", "diff"]:
        return f"pr diff #{cmd[3]}"
    return " ".join(cmd[:4])


# ---------------------------------------------------------------- running commands


def which(program: str) -> list[str]:
    """How to invoke ``program``: its resolved path, or the command ``READTHROUGH_<PROGRAM>`` names.

    ``subprocess`` without a shell only finds ``.exe`` files by bare name on Windows, so a ``gh.cmd`` shim
    or a ``git`` wrapper script would be missed; resolving through ``shutil.which`` (PATH and PATHEXT) keeps
    the rest of the code naming programs plainly. ``READTHROUGH_GH`` (or ``READTHROUGH_GIT``) substitutes
    the program: a path, or a JSON list for a command with arguments (``["python", "fake_gh.py"]``), which is
    how the tests stand in for ``gh`` on every platform. Unknown programs are returned as given, so the
    failure is reported where they're run.
    """
    override = os.environ.get(f"READTHROUGH_{program.upper()}")
    if override:
        return json.loads(override) if override.startswith("[") else [override]
    return [shutil.which(program) or program]


def run(
    cmd: list[str], *, check: bool = True, cwd: Path | None = None, env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> str:  # fmt: skip
    """Run a command and return its output. Failures raise a classified ``GhError`` (when ``check``)."""
    what = describe(cmd)
    if timeout is None:
        timeout = (default_tuning().github if cmd[:1] == ["gh"] else default_tuning().git).timeout_seconds
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [*which(cmd[0]), *cmd[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
            creationflags=_NO_WINDOW, env={**os.environ, **env} if env else None, timeout=timeout, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        log.debug("%s: timed out after %.0fs", what, timeout)
        raise GhTimeout(
            f"{what} timed out after {timeout:.0f}s", what=what, stderr=f"timed out after {timeout:.0f}s"
        ) from exc
    except FileNotFoundError as exc:
        raise GhError(f"{cmd[0]} isn't installed or isn't on PATH", what=what) from exc
    took = time.monotonic() - started
    log.debug("%s: exit %d in %.2fs", what, proc.returncode, took)
    if check and proc.returncode != 0:
        stderr = redact(proc.stderr.strip())
        cls = classify(stderr) if cmd[:1] == ["gh"] else GhError
        first = next((line.strip() for line in stderr.splitlines() if line.strip()), f"exit code {proc.returncode}")
        raise cls(f"{what} failed: {first[:300]}", what=what, stderr=stderr[:4000], returncode=proc.returncode)
    return proc.stdout


class _Slots:
    """One limit on concurrent ``gh`` calls for the whole process, sized by the first caller."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._semaphore: threading.BoundedSemaphore | None = None

    def get(self, concurrency: int) -> threading.BoundedSemaphore:
        with self._lock:
            if self._semaphore is None:
                self._semaphore = threading.BoundedSemaphore(concurrency)
            return self._semaphore


_slots = _Slots()


def _gh_slots(concurrency: int) -> threading.BoundedSemaphore:
    return _slots.get(concurrency)


# Jitter keeps retries from different repos from lining up. Its own generator, so seeding it (tests) or
# anything else using `random` can't change the other's sequence.
JITTER_MAX = 0.25
jitter = random.Random()  # noqa: S311 - timing jitter, not security


def backoff(attempt: int, tuning: GitHubTuning, exc: GhError, rng: random.Random | None = None) -> float:
    """Seconds to wait before retry ``attempt``: exponential with a cap, plus up to ``JITTER_MAX`` of itself."""
    if isinstance(exc, GhRateLimited):
        return tuning.rate_limit_wait_seconds
    base = min(tuning.retry_max_backoff_seconds, tuning.retry_backoff_seconds * 2 ** (attempt - 1))
    return base * (1 + (rng or jitter).uniform(0, JITTER_MAX))


class Secret:
    """A value that must never appear in logs, errors, or bundles. Only ``reveal()`` gives it back."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and secrets.compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        return hash(self._value)


_tokens: dict[str, Secret] = {}
_tokens_lock = threading.Lock()


def token_for(account: str) -> Secret:
    """The stored token for a signed-in ``gh`` account (``gh auth token --user``). Cached per process."""
    with _tokens_lock:
        if account not in _tokens:
            proc = subprocess.run(
                [*which("gh"), "auth", "token", "--user", account], capture_output=True, text=True, encoding="utf-8",
                errors="replace", creationflags=_NO_WINDOW, check=False,
            )  # fmt: skip
            if proc.returncode != 0 or not proc.stdout.strip():
                raise GhError(f"gh isn't signed in as {account}. Run: gh auth login (then pick that account)")
            _tokens[account] = Secret(proc.stdout.strip())
        return _tokens[account]


def redact(text: str) -> str:
    """Blank out any token this process has seen, in case ``gh`` echoed one into an error."""
    for token in list(_tokens.values()):
        value = token.reveal()
        if value:
            text = text.replace(value, "***")
    return text


def signed_in_accounts() -> list[str]:
    try:
        proc = subprocess.run(
            [*which("gh"), "auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW, check=False,
        )  # fmt: skip
    except FileNotFoundError:
        return []
    found = []
    for line in (proc.stdout + proc.stderr).splitlines():
        if "Logged in to github.com account" in line:
            name = line.split("account", 1)[1].split()[0]
            if name not in found:
                found.append(name)
    return found


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


def _fill(query: str) -> str:
    return (query.replace("__PAGE__", PAGE).replace("__FILES__", FILE_FIELDS)
            .replace("__THREADS__", THREAD_FIELDS).replace("__CHECKS__", CHECK_FIELDS))  # fmt: skip


PR_QUERY, FILES_QUERY, THREADS_QUERY, CHECKS_QUERY, MAIN_QUERY = map(
    _fill, (PR_QUERY, FILES_QUERY, THREADS_QUERY, CHECKS_QUERY, MAIN_QUERY)
)


class FileReader(Protocol):
    """Reads one file from the repo; ``None`` when it doesn't exist. The smallest slice of ``GitHub``."""

    def raw_or_none(self, path: str) -> str | None: ...


class Reader(FileReader, Protocol):
    """The read-only slice of ``GitHub`` that onboarding and the setup page need. Tests fake this."""

    repo: str
    owner: str
    name: str
    tuning: GitHubTuning

    def run(self, cmd: list[str], *, check: bool = True) -> str: ...
    def api(self, path: str) -> str: ...
    def graphql(self, query: str, **variables: str | int) -> dict[str, Any]: ...


class Fetcher(Reader, Protocol):
    """What a build needs on top of a ``Reader``: the PR list and everything that goes with it."""

    def fetch_prs(self, limit: int) -> tuple[str, list[dict[str, Any]], int]: ...
    def ci_context(self, bases: set[str]) -> tuple[dict[str, set[str] | None], set[str], str]: ...
    def rules(
        self, profile: Profile, *, branch: str = "main"
    ) -> tuple[dict[str, Contract], list[Instruction], list[str]]: ...
    def diffs(self, prs: list[dict[str, Any]], cache: Path) -> tuple[dict[int, str], dict[int, GhError], int]: ...
    def rate_limit(self) -> dict[str, int] | None: ...


class GitHub:
    """One repo, seen through one ``gh`` account (or your active one)."""

    def __init__(self, repo: str, account: str | None = None, tuning: GitHubTuning | None = None) -> None:
        self.repo = repo
        self.account = account
        self.owner, self.name = repo.split("/")
        self.tuning = tuning or default_tuning().github
        self.sleep = time.sleep  # replaced in tests

    def run(self, cmd: list[str], *, check: bool = True) -> str:
        """Run ``gh`` as this repo's account. Temporary failures are retried with backoff."""
        env = {"GH_TOKEN": token_for(self.account).reveal()} if self.account and cmd[:1] == ["gh"] else None
        slots = _gh_slots(self.tuning.concurrency)
        attempts = self.tuning.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                with slots:
                    return run(cmd, check=check, env=env, timeout=self.tuning.timeout_seconds)
            except GhError as exc:
                if not exc.retryable or attempt == attempts:
                    if exc.retryable:
                        exc.args = (f"{exc.args[0]} (gave up after {attempts} attempts)",)
                    raise
                wait = backoff(attempt, self.tuning, exc)
                log.warning("%s: %s (attempt %d of %d, retrying in %.1fs)", exc.what, exc.reason, attempt, attempts, wait,
                            extra={"attempt": attempt})  # fmt: skip
                self.sleep(wait)
        raise AssertionError("unreachable")

    def api(self, path: str) -> str:
        return self.run(["gh", "api", path])

    def api_or_none(self, path: str, *, absent: tuple[type[GhError], ...] = (GhNotFound,)) -> str | None:
        """``None`` only when GitHub says the thing isn't there. Network and other failures still raise."""
        try:
            return self.api(path)
        except absent:
            return None

    def raw(self, path: str) -> str:
        return self.run(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{self.repo}/contents/{path}"])

    def raw_or_none(self, path: str) -> str | None:
        """A file's contents, or ``None`` if it doesn't exist. Network and other failures still raise."""
        try:
            return self.raw(path)
        except GhNotFound:
            return None

    def graphql(self, query: str, **variables: str | int) -> dict[str, Any]:
        cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            # -F makes gh guess the type ("123" becomes a number), so only real integers use it.
            cmd += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        data = json.loads(self.run(cmd))
        if data.get("errors") and not data.get("data"):
            raise GhError(f"graphql: {data['errors'][0].get('message', data['errors'])}", what=describe(cmd))
        return data["data"]

    def rate_limit(self) -> dict[str, int] | None:
        """Remaining REST and GraphQL calls (checking doesn't count against the limit)."""
        try:
            res = json.loads(self.api("rate_limit"))["resources"]
        except (GhError, ValueError, KeyError):
            return None
        return {"rest": res["core"]["remaining"], "graphql": res["graphql"]["remaining"]}

    def viewer(self) -> str:
        return json.loads(self.api("user"))["login"]

    # ---- PRs

    def fetch_prs(self, limit: int) -> tuple[str, list[dict[str, Any]], int]:
        """Open PRs, newest activity first, up to ``limit``. Also returns how many are open in total."""
        prs: list[dict[str, Any]] = []
        cursor: str | None = None
        viewer, total = "", 0
        while len(prs) < limit:
            extra: dict[str, str | int] = {"cursor": cursor} if cursor else {}
            size = min(self.tuning.pr_page_size, limit - len(prs))
            data = self.graphql(PR_QUERY, owner=self.owner, name=self.name, size=size, **extra)
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
        return viewer, prs, total

    def complete_pr(self, pr: dict[str, Any]) -> None:
        """Fetch the rest of a PR's files, review threads, and checks, up to the configured caps.

        Whatever is still cut off is recorded under ``pr["truncated"]`` so the dashboard can say so.
        """
        n = pr["number"]
        caps = {"files": self.tuning.max_files_per_pr, "reviewThreads": self.tuning.max_review_threads_per_pr}
        for field, query in (("files", FILES_QUERY), ("reviewThreads", THREADS_QUERY)):
            conn = pr[field]
            self._follow(conn, caps[field], lambda c, q=query, f=field: self.graphql(
                q, owner=self.owner, name=self.name, number=n, cursor=c)["repository"]["pullRequest"][f])  # fmt: skip
        commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
        rollup = commit.get("statusCheckRollup") or {}
        if rollup.get("contexts"):
            self._follow(rollup["contexts"], self.tuning.max_checks_per_pr, lambda c: self.graphql(
                CHECKS_QUERY, owner=self.owner, name=self.name, oid=pr["headRefOid"], cursor=c,
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

    def diff(self, number: int, head: str, cache: Path) -> tuple[str, bool]:
        """A PR's diff, cached by head SHA so a refresh only downloads PRs that changed. Returns (text, fetched).

        Failures are never cached. GitHub refuses diffs over 300 files; that refusal is remembered (as a
        ``.toolarge`` marker for this head) and raised as ``GhTooLarge``.
        """
        path = cache / f"pr-{number}-{head[:12]}.diff"
        marker = path.with_suffix(".toolarge")
        empty = path.with_suffix(".empty")  # GitHub served no diff text (binary-only or docs it can't render)
        if path.exists() and path.stat().st_size:
            return path.read_text(encoding="utf-8"), False
        if empty.exists():
            return "", False
        if marker.exists():
            raise GhTooLarge(
                f"pr diff #{number}: too large for GitHub to serve (remembered for this commit)",
                what=f"pr diff #{number}",
            )
        try:
            text = self.run(["gh", "pr", "diff", str(number), "-R", self.repo])
        except GhTooLarge:
            self._forget_diffs(number, cache)
            marker.write_text("", encoding="utf-8")
            raise
        self._forget_diffs(number, cache)
        (path if text else empty).write_text(text, encoding="utf-8")
        return text, True

    _CACHE_SUFFIXES = (".diff", ".toolarge", ".empty")

    def _prune_cache(self, cache: Path, current: set[int]) -> None:
        """Drop empty .diff files (failures cached by older versions) and old entries of PRs no longer listed."""
        cutoff = time.time() - self.tuning.diff_cache_days * 86400
        for path in self._cache_entries(cache, "*"):
            number = path.name.split("-")[1]
            stale = number.isdigit() and int(number) not in current and path.stat().st_mtime < cutoff
            if stale or (path.suffix == ".diff" and not path.stat().st_size):
                path.unlink(missing_ok=True)

    @classmethod
    def _forget_diffs(cls, number: int, cache: Path) -> None:
        for old in cls._cache_entries(cache, str(number)):
            old.unlink(missing_ok=True)

    @classmethod
    def _cache_entries(cls, cache: Path, number: str) -> list[Path]:
        return [p for suffix in cls._CACHE_SUFFIXES for p in cache.glob(f"pr-{number}-*{suffix}")]

    def diffs(self, prs: list[dict[str, Any]], cache: Path) -> tuple[dict[int, str], dict[int, GhError], int]:
        """Diffs for every PR: (texts, failures by PR, how many were downloaded rather than cached)."""
        cache.mkdir(parents=True, exist_ok=True)

        def one(pr: dict[str, Any]) -> tuple[int, str | None, GhError | None, bool]:
            try:
                text, fetched = self.diff(pr["number"], pr["headRefOid"], cache)
                return pr["number"], text, None, fetched
            except GhError as exc:
                return pr["number"], None, exc, False

        results = pool_map(one, prs, self.tuning.concurrency)
        self._prune_cache(cache, {p["number"] for p in prs})
        texts = {n: t for n, t, _, _ in results if t is not None}
        failures = {n: e for n, _, e, _ in results if e is not None}
        return texts, failures, sum(1 for *_, fetched in results if fetched)

    # ---- CI

    def required_checks(self, branch: str) -> set[str] | None:
        """Required status checks for a branch, from classic protection and rulesets. None if not visible to you."""
        names: set[str] = set()
        seen_any = False
        out = self.api_or_none(f"repos/{self.repo}/branches/{branch}", absent=(GhNotFound, GhAuthError))
        if out:
            seen_any = True
            protection = (json.loads(out).get("protection") or {}).get("required_status_checks") or {}
            names.update(protection.get("contexts") or [])
            names.update(c["context"] for c in protection.get("checks") or [] if c.get("context"))
        out = self.api_or_none(f"repos/{self.repo}/rules/branches/{branch}", absent=(GhNotFound, GhAuthError))
        if out:
            seen_any = True
            for rule in json.loads(out):
                if rule.get("type") == "required_status_checks":
                    params = rule.get("parameters") or {}
                    names.update(c["context"] for c in params.get("required_status_checks") or [] if c.get("context"))
        return names if seen_any else None

    def ci_context(self, bases: set[str]) -> tuple[dict[str, set[str] | None], set[str], str]:
        """Required checks per base branch, the checks failing on the default branch, and its name."""
        data = self.graphql(MAIN_QUERY, owner=self.owner, name=self.name)
        ref = data["repository"]["defaultBranchRef"] or {}
        target = ref.get("target") or {}
        rollup = target.get("statusCheckRollup") or {}
        if rollup.get("contexts") and target.get("oid"):
            self._follow(rollup["contexts"], self.tuning.max_checks_per_pr, lambda c: self.graphql(
                CHECKS_QUERY, owner=self.owner, name=self.name, oid=target["oid"], cursor=c,
            )["repository"]["object"]["statusCheckRollup"]["contexts"])  # fmt: skip
        main_failing = failing_names(((rollup.get("contexts") or {}).get("nodes")) or [])
        ordered = sorted(bases)
        required = dict(zip(ordered, pool_map(self.required_checks, ordered, self.tuning.concurrency), strict=True))
        return required, main_failing, ref.get("name") or "main"

    # ---- rules

    def rules(
        self, profile: Profile, *, branch: str = "main"
    ) -> tuple[dict[str, Contract], list[Instruction], list[str]]:
        """The profile's architecture contracts and the repo's instruction files, from the default branch.

        Also returns what couldn't be fetched, so the build can carry on with the rest and say what's missing.
        """
        problems: list[str] = []
        doc = None
        if profile.contracts_source:
            try:
                doc = self.raw_or_none(profile.contracts_source)
            except GhError as exc:
                problems.append(f"{profile.contracts_source}: {exc.reason}")
        contracts = contracts_for(profile, doc)
        if not profile.instructions_dir:
            return contracts, [], problems
        try:
            out = self.api_or_none(f"repos/{self.repo}/contents/{profile.instructions_dir}")
        except GhError as exc:
            problems.append(f"{profile.instructions_dir}/: {exc.reason}")
            return contracts, [], problems
        listing = json.loads(out) if out and out.strip().startswith("[") else []
        names = [e["name"] for e in listing if e["name"].endswith(".md")]

        def fetch(name: str) -> str | None:
            try:
                return self.raw(f"{profile.instructions_dir}/{name}")
            except GhError as exc:
                problems.append(f"{name}: {exc.reason}")
                return None

        texts = pool_map(fetch, names, self.tuning.concurrency)
        instructions = [
            parse_instruction(n, t, f"https://github.com/{self.repo}/blob/{branch}/{profile.instructions_dir}/{n}")
            for n, t in zip(names, texts, strict=True)
            if t is not None
        ]
        return contracts, [i for i in instructions if i.patterns], problems


def _needs_more(pr: dict[str, Any]) -> bool:
    commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
    contexts = (commit.get("statusCheckRollup") or {}).get("contexts") or {}
    return any(
        (conn or {}).get("pageInfo", {}).get("hasNextPage") for conn in (pr["files"], pr["reviewThreads"], contexts)
    )

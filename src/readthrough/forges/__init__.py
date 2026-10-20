"""Forges: where pull requests come from. One adapter per platform, chosen per repo with ``forge = "..."``.

Every adapter produces the records in ``model.py`` and nothing else reads a platform's API. What an
adapter can't supply (a forge without review decisions, a diff endpoint, or required checks) it marks as
unsupported, and the build says so rather than reading silence as "none".

Two transports. GitHub keeps the ``gh`` CLI, which carries your sign-in, SSO, and proxies. The others
speak REST through ``urllib`` with a token from the platform's own CLI when it's installed and signed in
(``glab``, ``az``), else from an environment variable, else anonymously, which is enough for public repos.
Credentials never live in ``config.toml``; the ``[forges.<name>]`` tables hold hosts and variable names.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar

from readthrough import __version__
from readthrough.faults import AuthError, ForgeError, NetworkError, RateLimited, Timeout, TooLarge, classify_http
from readthrough.logs import pool_map
from readthrough.model import Capability, MergedPullRequest, PullRequest
from readthrough.process import Secret, redact, remember
from readthrough.registry import Registry
from readthrough.tuning import ForgeTuning, default_tuning

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- retries, shared by every transport


class _Slots:
    """One limit on concurrent forge calls for the whole process, sized by the first caller."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._semaphore: threading.BoundedSemaphore | None = None

    def get(self, concurrency: int) -> threading.BoundedSemaphore:
        with self._lock:
            if self._semaphore is None:
                self._semaphore = threading.BoundedSemaphore(concurrency)
            return self._semaphore


_slots = _Slots()

# Jitter keeps retries from different repos from lining up. Its own generator, so seeding it (tests) or
# anything else using `random` can't change the other's sequence.
JITTER_MAX = 0.25
jitter = random.Random()  # noqa: S311 - timing jitter, not security


def backoff(attempt: int, tuning: ForgeTuning, exc: ForgeError, rng: random.Random | None = None) -> float:
    """Seconds to wait before retry ``attempt``: exponential with a cap, plus up to ``JITTER_MAX`` of itself.

    A rate limit waits the configured time, or what the forge asked for in ``Retry-After`` when that's
    shorter (GitLab says exactly when the window resets); either way, never more than the configured wait.
    """
    if isinstance(exc, RateLimited):
        asked = exc.retry_after
        return min(tuning.rate_limit_wait_seconds, asked) if asked else tuning.rate_limit_wait_seconds
    base = min(tuning.retry_max_backoff_seconds, tuning.retry_backoff_seconds * 2 ** (attempt - 1))
    return base * (1 + (rng or jitter).uniform(0, JITTER_MAX))


# ---------------------------------------------------------------- credentials


@dataclass(frozen=True)
class Credential:
    """How a forge is authenticated: where the token came from, and the token. ``source`` is safe to log."""

    source: str  # "env GITLAB_TOKEN" | "glab" | "az" | "anonymous"
    token: Secret | None = None
    scheme: str = "Bearer"  # the Authorization scheme: Bearer, token, or Basic (ADO PATs)

    def header(self) -> str | None:
        if self.token is None:
            return None
        value = self.token.reveal()
        if self.scheme == "Basic":
            import base64

            value = base64.b64encode(f":{value}".encode()).decode()
        return f"{self.scheme} {value}"


def credential(*, env: str, cli: Callable[[], str | None] | None, cli_name: str, scheme: str = "Bearer") -> Credential:
    """An explicit variable wins, then the platform's CLI if it's signed in, else anonymous."""
    value = os.environ.get(env, "").strip() if env else ""
    if value:
        return Credential(f"env {env}", remember(Secret(value)), scheme)
    if cli is not None:
        found = (cli() or "").strip()
        if found:
            return Credential(cli_name, remember(Secret(found)), scheme)
    return Credential("anonymous", None, scheme)


_cli_tokens: dict[str, str | None] = {}
_cli_tokens_lock = threading.Lock()


def cached_cli_token(key: str, lookup: Callable[[], str | None]) -> str | None:
    """Ask a CLI for a token once per process. Spawning ``az`` or ``glab`` costs seconds; the answer doesn't change."""
    with _cli_tokens_lock:
        if key not in _cli_tokens:
            _cli_tokens[key] = lookup()
        return _cli_tokens[key]


# ---------------------------------------------------------------- REST over urllib


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]  # lower-cased names
    text: str

    def json(self) -> Any:
        return json.loads(self.text) if self.text.strip() else None


class Rest:
    """HTTP for one API base: a timeout on every call, UTF-8, errors classified by status.

    ``cred`` may be a callable that finds the credential; it's called on the first request (or when something
    asks ``describe_auth``), so building an adapter for a URL or a session id never spawns a CLI.
    """

    def __init__(
        self, base: str, cred: Credential | Callable[[], Credential], tuning: ForgeTuning, *,
        headers: dict[str, str] | None = None,
    ) -> None:  # fmt: skip
        self.base = base.rstrip("/") + "/"
        self._cred: Credential | None = cred if isinstance(cred, Credential) else None
        self._find_cred = None if isinstance(cred, Credential) else cred
        self._cred_lock = threading.Lock()
        self.tuning = tuning
        self.headers = {"User-Agent": f"readthrough/{__version__}", "Accept": "application/json", **(headers or {})}

    @property
    def cred(self) -> Credential:
        with self._cred_lock:
            if self._cred is None:
                self._cred = self._find_cred() if self._find_cred else Credential("anonymous")
            return self._cred

    def url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = path if path.startswith("http") else self.base + path.lstrip("/")
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url += ("&" if "?" in url else "?") + query
        return url

    def call(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None,
        headers: dict[str, str] | None = None, absent: tuple[type[ForgeError], ...] = (),
    ) -> Response | None:  # fmt: skip
        """One request. Raises a classified error for any status >= 400, except those in ``absent`` (-> None)."""
        url = self.url(path, params)
        what = f"{method} {urllib.parse.urlsplit(url).path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={**self.headers, **(headers or {})})  # noqa: S310 - https API URLs only
        if data is not None:
            req.add_header("Content-Type", "application/json")
        auth = self.cred.header()
        if auth and urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(self.base).netloc:
            # Only to our own host, and unredirected: urllib copies ordinary headers onto a redirect, and a
            # forge that bounces raw content to object storage must not get the token with it.
            req.add_unredirected_header("Authorization", auth)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.tuning.timeout_seconds) as resp:  # noqa: S310 - https API URLs
                text = resp.read().decode("utf-8", errors="replace")
                status, resp_headers = resp.status, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            text = redact(exc.read().decode("utf-8", errors="replace"))
            cls = classify_http(exc.code, text)
            if absent and issubclass(cls, absent):
                return None
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")[:300]
            error = cls(
                f"{what} failed: HTTP {exc.code} {first}".rstrip(), what=what, stderr=f"HTTP {exc.code} {text[:4000]}"
            )
            if isinstance(error, RateLimited):
                error.retry_after = retry_after(exc.headers.get("Retry-After"), exc.headers.get("RateLimit-Reset"))
            raise error from exc
        except TimeoutError as exc:
            raise Timeout(f"{what} timed out after {self.tuning.timeout_seconds:.0f}s", what=what) from exc
        except urllib.error.URLError as exc:
            raise NetworkError(f"{what} failed: {exc.reason}", what=what, stderr=str(exc.reason)) from exc
        log.debug("%s: %d in %.2fs", what, status, time.monotonic() - started)
        if status == HTTPStatus.NON_AUTHORITATIVE_INFORMATION and "text/html" in resp_headers.get("content-type", ""):
            # Azure DevOps answers an unauthorized API call with its sign-in page and a 203, not a 401.
            if absent and issubclass(AuthError, absent):
                return None
            raise AuthError(
                f"{what} needs sign-in (the forge answered with its sign-in page)",
                what=what,
                stderr="HTTP 203 sign-in page",
            )
        return Response(status, resp_headers, text)


def retry_after(header: str | None, reset: str | None = None, *, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` (seconds) or ``RateLimit-Reset`` (Unix time) header, if either parses."""
    for value, absolute in ((header, False), (reset, True)):
        if value and value.strip().isdigit():
            seconds = float(value) - (now if now is not None else time.time()) if absolute else float(value)
            return max(0.0, seconds)
    return None


def link_next(headers: dict[str, str]) -> str | None:
    """The ``rel="next"`` URL from a Link header (GitLab, Forgejo, GitHub REST), or None."""
    for part in headers.get("link", "").split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


# ---------------------------------------------------------------- the adapter contract


@dataclass(frozen=True)
class CiContext:
    """What a build learns about the repo's CI before looking at any PR."""

    required: dict[str, Capability[set[str]]]  # per base branch: the check names it needs
    main_failing: Capability[set[str]]  # checks failing on the default branch right now
    default_branch: str


class Forge:
    """Base for adapters. Subclasses set the class attributes and implement the fetching methods.

    ``capabilities`` names what the adapter can do beyond the essentials; the build reads it to decide what to
    attempt and what to report as unsupported: ``diffs``, ``required_checks``, ``default_branch_ci``,
    ``rate_limit``, ``memberships``.
    """

    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    config_dir: ClassVar[str] = ""  # the platform's conventional dot-directory, e.g. ".github"
    shape: ClassVar[str] = "owner/name"  # what a repo locator looks like here, for messages and the setup page
    capabilities: ClassVar[frozenset[str]] = frozenset()
    pr_noun: ClassVar[str] = "pull request"

    def __init__(
        self, repo: str, options: Any, tuning: ForgeTuning | None = None, *, account: str | None = None
    ) -> None:
        self.repo = repo
        self.options = options
        self.tuning = tuning or default_tuning().forge
        self.account = account
        self.sleep = time.sleep  # replaced in tests

    # ---- identity

    @classmethod
    def validate_repo(cls, repo: str) -> str | None:
        """Why ``repo`` isn't a valid locator for this forge, or None when it is."""
        raise NotImplementedError

    @classmethod
    def slug(cls, repo: str) -> str:
        """Path segments for URLs and the data directory: ``<forge>/<repo path>``."""
        return f"{cls.name}/{repo}"

    @classmethod
    def remote_matches(cls, url: str, repo: str) -> bool:
        """Whether a git remote URL (https or ssh, any host alias) points at ``repo``.

        Most forges put the repo's path at the end of the URL, with or without ``.git``; a forge whose URLs
        are shaped differently (Azure DevOps) overrides this.
        """
        return re.search(rf"[:/]{re.escape(repo.lower())}(?:\.git)?/?$", url.lower()) is not None

    def web_url(self) -> str:
        raise NotImplementedError

    def pr_url(self, number: int) -> str:
        raise NotImplementedError

    def pr_ref(self, number: int) -> str:
        """The git ref to fetch a PR's head from the remote, e.g. ``pull/12/head``."""
        raise NotImplementedError

    def file_url(self, path: str, branch: str) -> str:
        raise NotImplementedError

    def new_file_url(self, path: str, branch: str) -> str | None:  # noqa: ARG002 - the default has no such page
        """A page where a maintainer can create ``path`` on ``branch``, for proposing a profile upstream."""
        return None

    def describe_auth(self) -> str:
        """For `doctor`: how calls are authenticated, never including the token."""
        return "gh" if self.name == "github" else "anonymous"

    # ---- retrying

    def retrying(self, what: str, call: Callable[[], Any]) -> Any:
        """Run ``call``, retrying temporary failures with backoff under the process-wide concurrency limit."""
        slots = _slots.get(self.tuning.concurrency)
        attempts = self.tuning.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                with slots:
                    return call()
            except ForgeError as exc:
                if not exc.retryable or attempt == attempts:
                    if exc.retryable:
                        exc.args = (f"{exc.args[0]} (gave up after {attempts} attempts)",)
                    raise
                wait = backoff(attempt, self.tuning, exc)
                log.warning("%s: %s (attempt %d of %d, retrying in %.1fs)", what, exc.reason, attempt, attempts, wait,
                            extra={"attempt": attempt})  # fmt: skip
                self.sleep(wait)
        raise AssertionError("unreachable")

    # ---- fetching (implemented by adapters)

    def repo_info(self) -> dict[str, Any]:
        """``{"full_name", "default_branch", "private", "description"}``; raises when the repo can't be read."""
        raise NotImplementedError

    def fetch_prs(self, limit: int) -> tuple[str, list[PullRequest], int]:
        """Open PRs, newest activity first, up to ``limit``, with the viewer's login and the total open."""
        raise NotImplementedError

    def merged_prs(self, count: int) -> tuple[str, list[MergedPullRequest]]:
        """The default branch's name and the ``count`` most recently merged PRs, for onboarding."""
        raise NotImplementedError

    def tree(self, branch: str) -> tuple[list[str], bool]:
        """File paths in the repo at ``branch``, and whether that's all of them.

        A forge may stop short (GitHub truncates huge trees; paged forges stop at ``max_tree_files``), in which
        case the second value is False and onboarding says so rather than judging patterns against a partial list.
        """
        raise NotImplementedError

    def read_file(self, path: str) -> str | None:
        """A file's text at the default branch, or None only when it doesn't exist."""
        raise NotImplementedError

    def read_files(self, paths: list[str]) -> dict[str, str]:
        """The subset of ``paths`` that exist, with their text. Adapters override when they can batch."""
        return {p: text for p in paths if (text := self.read_file(p)) is not None}

    def list_dir(self, path: str) -> list[str]:
        """File names directly under ``path`` at the default branch; empty when it doesn't exist."""
        raise NotImplementedError

    def ci_context(self, bases: set[str]) -> CiContext:
        raise NotImplementedError

    def is_member(self, group: str, login: str) -> bool:
        """Is ``login`` in ``group`` (an org, or ``org/team``)? Only for adapters with the ``memberships`` capability."""
        raise NotImplementedError

    def rate_limit(self) -> dict[str, int] | None:
        return None

    # ---- diffs, cached by head so a refresh only downloads PRs that changed

    def fetch_diff(self, number: int) -> str:
        """The PR's unified diff. Raises ``TooLarge`` when the forge refuses to serve it."""
        raise NotImplementedError

    _CACHE_SUFFIXES = (".diff", ".toolarge", ".empty")

    def diff(self, number: int, head: str, cache: Path) -> tuple[str, bool]:
        """A PR's diff, cached by head SHA. Returns (text, fetched).

        Failures are never cached. A refusal (too large) is remembered for this head as a ``.toolarge``
        marker, and a legitimately empty diff as ``.empty``, so neither is asked for again until the head
        changes.
        """
        path = cache / f"pr-{number}-{head[:12]}.diff"
        marker, empty = path.with_suffix(".toolarge"), path.with_suffix(".empty")
        if path.exists() and path.stat().st_size:
            return path.read_text(encoding="utf-8"), False
        if empty.exists():
            return "", False
        if marker.exists():
            raise TooLarge(
                f"pr diff #{number}: too large for {self.label} to serve (remembered for this commit)",
                what=f"pr diff #{number}",
            )
        try:
            text = self.fetch_diff(number)
        except TooLarge:
            self._forget_diffs(number, cache)
            marker.write_text("", encoding="utf-8")
            raise
        self._forget_diffs(number, cache)
        (path if text else empty).write_text(text, encoding="utf-8")
        return text, True

    def diffs(self, prs: list[PullRequest], cache: Path) -> tuple[dict[int, str], dict[int, ForgeError], int]:
        """Diffs for every PR: (texts, failures by PR, how many were downloaded rather than cached)."""
        cache.mkdir(parents=True, exist_ok=True)

        def one(pr: PullRequest) -> tuple[int, str | None, ForgeError | None, bool]:
            try:
                text, fetched = self.diff(pr.number, pr.head, cache)
            except ForgeError as exc:
                return pr.number, None, exc, False
            return pr.number, text, None, fetched

        results = pool_map(one, prs, self.tuning.concurrency)
        self._prune_cache(cache, {p.number for p in prs})
        texts = {n: t for n, t, _, _ in results if t is not None}
        failures = {n: e for n, _, e, _ in results if e is not None}
        return texts, failures, sum(1 for *_, fetched in results if fetched)

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


# ---------------------------------------------------------------- the registry


FORGES: Registry[type[Forge]] = Registry("forge")
DEFAULT = "github"


@dataclass(frozen=True)
class NoForgeOptions:
    pass


def names() -> list[str]:
    return FORGES.names()


def adapter(name: str) -> type[Forge]:
    return FORGES.get(name).impl


def make(cfg: Any) -> Forge:
    """The forge for a repo's config: ``cfg.forge`` names the adapter, ``cfg.forge_options`` its validated options."""
    cls = adapter(cfg.forge)
    options = (getattr(cfg, "forge_options", None) or {}).get(cfg.forge) or FORGES.defaults_for(cfg.forge)
    return cls(cfg.repo, options, cfg.tuning.forge, account=getattr(cfg, "account", None))


def validate_repo(forge: str, repo: str) -> str | None:
    return adapter(forge).validate_repo(repo)


def slug(forge: str, repo: str) -> str:
    return adapter(forge).slug(repo)


def _register_builtin() -> None:
    # Imported here so each adapter module can import this one.
    from readthrough.forges import azure_devops, forgejo, github, gitlab  # noqa: F401


_register_builtin()

__all__ = [
    "DEFAULT", "FORGES", "JITTER_MAX", "CiContext", "Credential", "Forge", "NoForgeOptions", "Response", "Rest",
    "adapter", "backoff", "cached_cli_token", "credential", "jitter", "link_next", "make", "names", "retry_after", "slug",
    "validate_repo",
]  # fmt: skip

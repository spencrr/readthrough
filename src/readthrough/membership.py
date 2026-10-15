"""Team and org membership for author classification.

A profile can say "members of these teams / orgs are internal". Listing a big org (microsoft has far
more members than any API page) isn't practical, so each PR author is checked individually and the
answer is cached for a week.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Protocol

from readthrough.files import write_atomic
from readthrough.github import GhError, GhNotFound
from readthrough.logs import pool_map
from readthrough.tuning import GitHubTuning

log = logging.getLogger(__name__)
DAY = 24 * 3600


class Runner(Protocol):
    """All a membership check needs: run a ``gh`` command, and the concurrency to run them at."""

    tuning: GitHubTuning

    def run(self, cmd: list[str]) -> str: ...


def _check(gh: Runner, key: str) -> bool:
    """Is this login in the org or team? Only a 404 means "no"; other failures raise, so they aren't cached."""
    kind, org, *rest = key.split(":")
    login = rest[-1]
    try:
        if kind == "org":
            gh.run(["gh", "api", f"orgs/{org}/members/{login}"])  # 204 if a member visible to you, else 404
            return True
        out = gh.run(["gh", "api", f"orgs/{org}/teams/{rest[0]}/memberships/{login}"])
        return json.loads(out or "{}").get("state") == "active"
    except GhNotFound:
        return False


def internal_members(
    gh: Runner, *, teams: list[str], orgs: list[str], logins: set[str], cache_path: Path, ttl_days: float = 7.0
) -> tuple[set[str], list[str]]:
    """Logins (lower-case) that belong to one of ``teams`` ("org/team-slug") or ``orgs``, and checks that failed.

    Answers are cached for ``ttl_days``. A check that fails (network, auth) isn't cached, and its login is
    classified by the profile's other rules this time.
    """
    if not (teams or orgs) or not logins:
        return set(), []
    try:
        cache: dict[str, Any] = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cache = {}
    now = time.time()
    keys = [f"org:{o}:{login}" for o in orgs for login in logins]
    keys += [f"team:{t.split('/')[0]}:{t.split('/')[1]}:{login}" for t in teams if "/" in t for login in logins]
    stale = [k for k in keys if k not in cache or now - cache[k][1] > ttl_days * DAY]
    failed: list[str] = []
    if stale:
        log.info("Checking %d team/org memberships", len(stale))

        def check(key: str) -> bool | GhError:
            try:
                return _check(gh, key)
            except GhError as exc:
                return exc

        for key, result in zip(stale, pool_map(check, stale, gh.tuning.concurrency), strict=True):
            if isinstance(result, GhError):
                failed.append(f"{key.split(':', 1)[1]}: {result.reason}")
                cache.pop(key, None)
            else:
                cache[key] = [result, now]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(cache_path, json.dumps(cache))
    return {k.rsplit(":", 1)[1].lower() for k in keys if k in cache and cache[k][0]}, failed

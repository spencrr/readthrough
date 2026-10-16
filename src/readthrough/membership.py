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

from readthrough.faults import ForgeError
from readthrough.files import write_atomic
from readthrough.logs import pool_map
from readthrough.tuning import ForgeTuning

log = logging.getLogger(__name__)
DAY = 24 * 3600


class Membership(Protocol):
    """All a membership check needs from a forge: one lookup, and the concurrency to run them at."""

    tuning: ForgeTuning

    def is_member(self, group: str, login: str) -> bool: ...


def _check(forge: Membership, key: str) -> bool:
    kind, org, *rest = key.split(":")
    login = rest[-1]
    return forge.is_member(org if kind == "org" else f"{org}/{rest[0]}", login)


def internal_members(
    forge: Membership, *, teams: list[str], orgs: list[str], logins: set[str], cache_path: Path, ttl_days: float = 7.0
) -> tuple[set[str], list[str]]:
    """Logins (lower-case) that belong to one of ``teams`` ("org/team-slug") or ``orgs``, and checks that failed.

    Answers are cached for ``ttl_days``. A check that fails (network, auth) isn't cached, and its login is
    classified by the profile's other rules this time. Only the forge's "no" is a no: the adapter raises
    for anything but a definite answer.
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

        def check(key: str) -> bool | ForgeError:
            try:
                return _check(forge, key)
            except ForgeError as exc:
                return exc

        for key, result in zip(stale, pool_map(check, stale, forge.tuning.concurrency), strict=True):
            if isinstance(result, ForgeError):
                failed.append(f"{key.split(':', 1)[1]}: {result.reason}")
                cache.pop(key, None)
            else:
                cache[key] = [result, now]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(cache_path, json.dumps(cache))
    return {k.rsplit(":", 1)[1].lower() for k in keys if k in cache and cache[k][0]}, failed

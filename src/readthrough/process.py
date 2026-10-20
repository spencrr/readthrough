"""Running other programs (``gh``, ``glab``, ``az``, ``git``) and keeping their secrets out of sight.

Every outside program goes through ``run`` with an argument list, never a shell string. Programs are
resolved with ``shutil.which`` so ``.cmd`` shims and wrappers are found on Windows, output is decoded as
UTF-8, every call has a timeout, and failures come back as classified ``ForgeError``s. ``READTHROUGH_GH``
(or ``READTHROUGH_GIT``, ...) substitutes a program; the tests use that to stand in for ``gh``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Literal, overload

from readthrough.faults import ForgeError, Timeout, classify_stderr
from readthrough.tuning import default_tuning

log = logging.getLogger(__name__)

# Keep child consoles from flashing when we run windowless (scheduled task, URL handler).
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
FORGE_CLIS = ("gh", "glab", "az")  # their stderr is classified; anything else is a plain error


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


_seen_secrets: list[Secret] = []
_secrets_lock = threading.Lock()


def remember(secret: Secret) -> Secret:
    """Register a secret so ``redact`` can blank it out of anything we log or raise."""
    with _secrets_lock:
        if secret not in _seen_secrets:
            _seen_secrets.append(secret)
    return secret


def redact(text: str) -> str:
    """Blank out every secret this process has seen, in case a program echoed one into an error."""
    with _secrets_lock:
        values = [s.reveal() for s in _seen_secrets]
    for value in values:
        if value:
            text = text.replace(value, "***")
    return text


def which(program: str) -> list[str]:
    """How to invoke ``program``: its resolved path, or the command ``READTHROUGH_<PROGRAM>`` names.

    The override is a path, or a JSON list for a command with arguments (``["python", "fake_gh.py"]``).
    Unknown programs are returned as given, so the failure is reported where they're run.
    """
    override = os.environ.get(f"READTHROUGH_{program.upper()}")
    if override:
        return json.loads(override) if override.startswith("[") else [override]
    return [shutil.which(program) or program]


def installed(program: str) -> bool:
    return bool(os.environ.get(f"READTHROUGH_{program.upper()}") or shutil.which(program))


def describe(cmd: list[str]) -> str:
    """A short, readable name for a command: `graphql PullRequests`, `GET repos/o/r/branches/main`, `pr diff #12`."""
    if cmd[:3] == ["gh", "api", "graphql"]:
        query = next((a[6:] for a in cmd if a.startswith("query=")), "")
        name = re.search(r"query\s+(\w+)", query)
        number = next((a.split("=", 1)[1] for a in cmd if a.startswith("number=")), "")
        return f"graphql {name.group(1) if name else 'query'}{' #' + number if number else ''}"
    if cmd[:2] in (["gh", "api"], ["glab", "api"]):
        method = cmd[cmd.index("-X") + 1] if "-X" in cmd else "GET"
        path = next((a for a in reversed(cmd[2:]) if not a.startswith("-") and ":" not in a), "?")
        return f"{method} {path}"
    if cmd[:3] == ["gh", "pr", "diff"]:
        return f"pr diff #{cmd[3]}"
    return " ".join(cmd[:4])


def run(
    cmd: list[str], *, check: bool = True, cwd: Path | None = None, env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> str:  # fmt: skip
    """Run a command and return its output. Failures raise a classified ``ForgeError`` (when ``check``)."""
    what = describe(cmd)
    if timeout is None:
        timeout = (default_tuning().forge if cmd[0] in FORGE_CLIS else default_tuning().git).timeout_seconds
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [*which(cmd[0]), *cmd[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
            creationflags=NO_WINDOW, env={**os.environ, **env} if env else None, timeout=timeout, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        log.debug("%s: timed out after %.0fs", what, timeout)
        raise Timeout(
            f"{what} timed out after {timeout:.0f}s", what=what, stderr=f"timed out after {timeout:.0f}s"
        ) from exc
    except FileNotFoundError as exc:
        raise ForgeError(f"{cmd[0]} isn't installed or isn't on PATH", what=what) from exc
    took = time.monotonic() - started
    log.debug("%s: exit %d in %.2fs", what, proc.returncode, took)
    if check and proc.returncode != 0:
        stderr = redact(proc.stderr.strip())
        cls = classify_stderr(stderr) if cmd[0] in FORGE_CLIS else ForgeError
        first = next((line.strip() for line in stderr.splitlines() if line.strip()), f"exit code {proc.returncode}")
        raise cls(f"{what} failed: {first[:300]}", what=what, stderr=stderr[:4000], returncode=proc.returncode)
    return proc.stdout


@overload
def quiet_output(cmd: list[str], *, with_stderr: bool = False) -> str | None: ...
@overload
def quiet_output(
    cmd: list[str], *, with_stderr: bool = False, with_failure: Literal[True]
) -> tuple[str | None, str]: ...


def quiet_output(cmd: list[str], *, with_stderr: bool = False, with_failure: bool = False) -> Any:
    """A program's stdout, or None if it isn't installed or failed. For looking things up (tokens, sign-ins).

    ``with_stderr`` appends what the program wrote to stderr: ``glab auth status`` reports there, not to stdout.
    ``with_failure`` returns ``(stdout or None, why)`` instead, where ``why`` is the redacted stderr of a
    failed run (or what kept it from running), for telling the user what the program said.
    """
    try:
        proc = subprocess.run(
            [*which(cmd[0]), *cmd[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=NO_WINDOW, check=False, timeout=default_tuning().forge.timeout_seconds,
        )  # fmt: skip
    except FileNotFoundError:
        return (None, f"{cmd[0]} isn't installed or isn't on PATH") if with_failure else None
    except subprocess.TimeoutExpired:
        return (None, f"{cmd[0]} didn't answer in time") if with_failure else None
    if proc.returncode != 0:
        return (None, redact(proc.stderr.strip())) if with_failure else None
    out = proc.stdout + ("\n" + proc.stderr if with_stderr and proc.stderr else "")
    return (out, "") if with_failure else out

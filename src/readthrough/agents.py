"""Deep-review agents: the coding agents a review session runs in, chosen by name.

A deep review is one persistent agent session per PR, started with a prompt that carries the PR's marker
and resumed by id after that. Two agents are built in, and ``agent = "auto"`` in ``config.toml`` takes the
first one installed:

- ``copilot``: the GitHub Copilot CLI. Sessions live under ``~/.copilot/session-state/<id>/`` and can be
  opened in VS Code's agent window.
- ``claude``: Claude Code. Sessions are ``~/.claude/projects/<cwd>/<id>.jsonl``.

Each adapter knows how to start and resume a session, where its sessions are kept, how to read a session's
first user message (the marker in it links the session to a PR), whether a session is open right now when the
agent leaves a trace of that, and where its skills go, so ``readthrough install skill`` teaches every agent
you have. Anything an agent can't say (Claude Code leaves no "in use" mark) is reported as unknown, not as
"no". Others, such as Codex or Gemini CLI, are a registry entry away; what's shipped is what could be run.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from readthrough.process import installed, which
from readthrough.registry import Registry
from readthrough.tuning import default_tuning

AUTO = "auto"
AUTO_ORDER = ("copilot", "claude")  # the first installed wins when agent = "auto"
SKILL_NAME = "readthrough"


@dataclass(frozen=True)
class SessionRecord:
    """A session as found on disk, before anything is read from it."""

    id: str
    path: Path  # the directory (Copilot) or file (Claude Code) holding it
    updated: float  # mtime, seconds since the epoch


@dataclass
class SessionInfo:
    id: str
    exists: bool
    updated: int | None = None
    active: bool | None = None  # None: this agent leaves no trace of whether the session is open

    def payload(self) -> dict[str, Any]:
        return {"id": self.id, "exists": self.exists, "updated": self.updated, "active": self.active}


@dataclass(frozen=True)
class AgentOptions:
    extra_args: list[str] = field(default_factory=list)  # appended to every launch, after readthrough's own flags


class Agent:
    """Base for agent adapters. Subclasses set the class attributes and implement the session methods."""

    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    program: ClassVar[str] = ""  # the executable, resolved through process.which
    install_hint: ClassVar[str] = ""

    def __init__(self, options: AgentOptions | None = None) -> None:
        self.options = options or AgentOptions()

    def available(self) -> bool:
        return installed(self.program)

    def executable(self) -> list[str]:
        return which(self.program)

    @property
    def skill_dir(self) -> Path:
        """Where this agent reads a skill named ``readthrough`` from."""
        raise NotImplementedError

    # ---- launching

    def new_session_argv(
        self, *, session_id: str, name: str, prompt: str, add_dirs: list[Path], extra: list[str] | None = None
    ) -> list[str]:
        """Start a session with this id and name, opened on ``prompt``. ``extra`` are the repo's own flags."""
        raise NotImplementedError

    def resume_argv(self, session_id: str, *, add_dirs: list[Path], extra: list[str] | None = None) -> list[str]:
        raise NotImplementedError

    def editor_url(self, session_id: str) -> str | None:  # noqa: ARG002 - the default has no editor deep link
        """A link that opens the session in an editor, when the agent has one."""
        return None

    # ---- sessions on disk

    def sessions(self) -> Iterator[SessionRecord]:
        raise NotImplementedError

    def first_user_message(self, record: SessionRecord) -> str | None:
        """The first thing the user said, ``None`` while the session hasn't written it yet, ``""`` if it never will."""
        raise NotImplementedError

    def session_meta(self, record: SessionRecord) -> dict[str, str]:  # noqa: ARG002 - the default records nothing
        """``client`` (what started it) and ``name`` when the agent records them."""
        return {}

    def session_info(self, session_id: str) -> SessionInfo:
        raise NotImplementedError


# ---------------------------------------------------------------- GitHub Copilot CLI


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))


AGENTS: Registry[type[Agent]] = Registry("agent")


@AGENTS.add("copilot", "GitHub Copilot CLI; sessions open in VS Code's agent window too", options=AgentOptions)
class Copilot(Agent):
    name = "copilot"
    label = "Copilot"
    program = "copilot"
    install_hint = "winget install GitHub.Copilot"

    @property
    def skill_dir(self) -> Path:
        return Path.home() / ".agents" / "skills" / SKILL_NAME

    def new_session_argv(
        self, *, session_id: str, name: str, prompt: str, add_dirs: list[Path], extra: list[str] | None = None
    ) -> list[str]:
        return [*self.executable(), f"--session-id={session_id}", f"--name={name}", "-i", prompt, *_dirs(add_dirs),
                *self.options.extra_args, *(extra or [])]  # fmt: skip

    def resume_argv(self, session_id: str, *, add_dirs: list[Path], extra: list[str] | None = None) -> list[str]:
        return [
            *self.executable(),
            f"--resume={session_id}",
            *_dirs(add_dirs),
            *self.options.extra_args,
            *(extra or []),
        ]

    def editor_url(self, session_id: str) -> str | None:
        """Open an existing Copilot CLI session in VS Code's Agents window (VS Code 1.138+)."""
        return f"vscode://agents/agent-host-session/copilotcli/{session_id}"

    def sessions(self) -> Iterator[SessionRecord]:
        root = copilot_home() / "session-state"
        try:
            entries = list(os.scandir(root))
        except FileNotFoundError:
            return
        for entry in entries:
            if entry.is_dir():
                events = Path(entry.path) / "events.jsonl"
                stamp = (events if events.exists() else Path(entry.path)).stat().st_mtime
                yield SessionRecord(entry.name, Path(entry.path), stamp)

    def first_user_message(self, record: SessionRecord) -> str | None:
        events = record.path / "events.jsonl"
        if not events.exists():
            return None
        read = 0
        with events.open("rb") as handle:
            for raw in handle:
                read += len(raw)
                if read > default_tuning().sessions.first_message_bytes:
                    return ""
                if b'"user.message"' not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    return None  # still being written
                if event.get("type") == "user.message":
                    return str((event.get("data") or {}).get("content") or "")
        return None

    def session_meta(self, record: SessionRecord) -> dict[str, str]:
        path = record.path / "workspace.yaml"
        if not path.exists():
            return {}
        fields = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition(":")
            if sep and key in ("name", "client_name"):
                fields["client" if key == "client_name" else key] = value.strip()
        return fields

    def session_info(self, session_id: str) -> SessionInfo:
        folder = copilot_home() / "session-state" / session_id
        if not folder.is_dir():
            return SessionInfo(session_id, exists=False)
        events = folder / "events.jsonl"
        stamp = (events if events.exists() else folder).stat().st_mtime
        # Copilot leaves inuse.<pid>.lock behind while a session is open; the process must still be running.
        pids = (re.fullmatch(r"inuse\.(\d+)\.lock", p.name) for p in folder.glob("inuse.*.lock"))
        active = any(pid_alive(int(m.group(1))) for m in pids if m)
        return SessionInfo(session_id, exists=True, updated=round(stamp), active=active)


# ---------------------------------------------------------------- Claude Code


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


@AGENTS.add("claude", "Claude Code; sessions are resumed by id in a terminal", options=AgentOptions)
class Claude(Agent):
    name = "claude"
    label = "Claude Code"
    program = "claude"
    install_hint = "npm install -g @anthropic-ai/claude-code"

    @property
    def skill_dir(self) -> Path:
        return claude_home() / "skills" / SKILL_NAME

    def new_session_argv(
        self, *, session_id: str, name: str, prompt: str, add_dirs: list[Path], extra: list[str] | None = None
    ) -> list[str]:
        return [*self.executable(), "--session-id", session_id, "--name", name, *_dirs(add_dirs),
                *self.options.extra_args, *(extra or []), prompt]  # fmt: skip

    def resume_argv(self, session_id: str, *, add_dirs: list[Path], extra: list[str] | None = None) -> list[str]:
        return [*self.executable(), "--resume", session_id, *_dirs(add_dirs), *self.options.extra_args, *(extra or [])]

    def sessions(self) -> Iterator[SessionRecord]:
        root = claude_home() / "projects"
        if not root.is_dir():
            return
        for path in root.glob("*/*.jsonl"):
            if _UUID_RE.fullmatch(path.stem):
                yield SessionRecord(path.stem, path, path.stat().st_mtime)

    def first_user_message(self, record: SessionRecord) -> str | None:
        if not record.path.exists():
            return None
        read = 0
        with record.path.open("rb") as handle:
            for raw in handle:
                read += len(raw)
                if read > default_tuning().sessions.first_message_bytes:
                    return ""
                if b'"type":"user"' not in raw and b'"type": "user"' not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    return None  # still being written
                if event.get("type") == "user" and not event.get("isSidechain"):
                    content = (event.get("message") or {}).get("content")
                    if isinstance(content, list):
                        content = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
                    return str(content or "")
        return None

    def session_meta(self, record: SessionRecord) -> dict[str, str]:  # noqa: ARG002 - Claude Code keeps no client name
        return {"client": "claude"}

    def session_info(self, session_id: str) -> SessionInfo:
        matches = (
            list((claude_home() / "projects").glob(f"*/{session_id}.jsonl")) if _UUID_RE.fullmatch(session_id) else []
        )
        if not matches:
            return SessionInfo(session_id, exists=False)
        # Claude Code leaves no mark of an open session, so whether it's open now can't be said.
        return SessionInfo(session_id, exists=True, updated=round(max(p.stat().st_mtime for p in matches)), active=None)


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _dirs(add_dirs: list[Path]) -> list[str]:
    out: list[str] = []
    for d in add_dirs:
        out += ["--add-dir", str(d)]
    return out


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- choosing


def make(name: str, options: dict[str, Any] | None = None) -> Agent:
    """The agent ``name`` refers to, built with its options from ``[agents.<name>]``."""
    cls = AGENTS.get(name).impl
    opts = (options or {}).get(name) or AGENTS.defaults_for(name)
    return cls(opts)


def resolve(name: str, options: dict[str, Any] | None = None) -> Agent:
    """The configured agent, or for ``auto`` the first installed one (Copilot before Claude Code).

    When nothing is installed, ``auto`` still resolves to the first choice, so messages can say what to install.
    """
    if name != AUTO:
        return make(name, options)
    agents = [make(n, options) for n in AUTO_ORDER]
    return next((a for a in agents if a.available()), agents[0])


def all_agents(options: dict[str, Any] | None = None) -> list[Agent]:
    return [make(n, options) for n in AGENTS.names()]


def describe(name: str, options: dict[str, Any] | None = None) -> str:
    """One line for ``doctor``: which agent reviews run in, and whether it's installed."""
    agent = resolve(name, options)
    chosen = ", chosen automatically" if name == AUTO else ""
    if agent.available():
        return f"{agent.label} ({agent.program}{chosen})"
    return f"{agent.label}: {agent.program} isn't installed ({agent.install_hint})"

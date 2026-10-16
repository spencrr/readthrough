"""Deep-review sessions: one persistent GitHub Copilot CLI session per PR, plus an isolated git worktree.

The session ID is derived from the PR URL, so the same button starts the session the first time and
resumes it afterwards. ``review --fresh`` starts over with a random ID and remembers it in the ledger.

Sessions started anywhere else (VS Code's agent window, a pasted prompt) get IDs readthrough didn't choose.
Every prompt readthrough writes starts with a marker like ``[readthrough:github/owner/name#123]`` (the repo's
slug: its forge, then its path there), and ``SessionIndex`` links any session whose first message carries
one back to its PR.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from readthrough import forges
from readthrough.config import Config
from readthrough.process import run
from readthrough.tuning import default_tuning

log = logging.getLogger(__name__)

URL_SCHEME = "readthrough"
URL_ACTIONS = ("review", "refresh", "open")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))


MARKER_RE = re.compile(r"\[readthrough:([\w.-]+(?:/[\w.-]+)+)#(\d{1,7})\]")


def pr_marker(slug: str, number: int) -> str:
    return f"[readthrough:{slug}#{number}]"


def _same_repo(found: str, slug: str) -> bool:
    """Markers name the repo by slug; ones written before there were forges name GitHub repos by path alone."""
    found, slug = found.lower(), slug.lower()
    return found == slug or (slug.startswith(f"{forges.DEFAULT}/") and found == slug.split("/", 1)[1])


def _first_user_message(folder: Path, max_bytes: int | None = None) -> str | None:
    """The first user message of a session, or None if it hasn't been written yet."""
    events = folder / "events.jsonl"
    if not events.exists():
        return None
    read = 0
    with events.open("rb") as handle:
        for raw in handle:
            read += len(raw)
            if read > (max_bytes or default_tuning().sessions.first_message_bytes):
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


def _workspace_fields(folder: Path) -> dict[str, str]:
    path = folder / "workspace.yaml"
    if not path.exists():
        return {}
    fields = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition(":")
        if sep and key in ("name", "client_name"):
            fields[key] = value.strip()
    return fields


class SessionIndex:
    """Which Copilot sessions belong to which PR, found by the marker in their first message.

    The answer for a session never changes, so it's cached in ``session-index.json``; each scan only
    reads sessions it hasn't seen. Sessions without a first message yet are checked again later.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last = 0.0
        try:
            self._known: dict[str, dict[str, str] | None] = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self._known = {}

    def scan(self, *, force: bool = False) -> bool:
        with self._lock:
            if not force and time.monotonic() - self._last < default_tuning().sessions.scan_seconds:
                return False
            self._last = time.monotonic()
            root = copilot_home() / "session-state"
            try:
                ids = {e.name for e in os.scandir(root) if e.is_dir()}
            except FileNotFoundError:
                ids = set()
            changed = False
            for gone in set(self._known) - ids:
                del self._known[gone]
                changed = True
            for sid in ids - set(self._known):
                first = _first_user_message(root / sid)
                if first is None:
                    # Still starting up? Look again later. Long-idle sessions that never got a message are settled.
                    if time.time() - (root / sid).stat().st_mtime < 3600:
                        continue
                    first = ""
                match = MARKER_RE.search(first[:2000])
                fields = _workspace_fields(root / sid)
                self._known[sid] = (
                    {
                        "pr": f"{match.group(1).lower()}#{match.group(2)}",
                        "client": fields.get("client_name", ""),
                        "name": fields.get("name", ""),
                    }
                    if match
                    else None
                )
                changed = True
            if changed:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._known), encoding="utf-8")
                tmp.replace(self.path)
            return changed

    def sessions_for(self, slug: str, number: int) -> dict[str, dict[str, str]]:
        with self._lock:
            out = {}
            for sid, meta in self._known.items():
                if meta:
                    found, _, n = meta["pr"].rpartition("#")
                    if n == str(number) and _same_repo(found, slug):
                        out[sid] = meta
            return out


_indexes: dict[Path, SessionIndex] = {}
_indexes_lock = threading.Lock()


def session_index(home: Path) -> SessionIndex:
    with _indexes_lock:
        if home not in _indexes:
            _indexes[home] = SessionIndex(home / "session-index.json")
        return _indexes[home]


def pr_sessions(cfg: Config, number: int, primary: str) -> list[dict[str, object]]:
    """Every Copilot session for this PR, the default one first, then the most recently used."""
    found = session_index(cfg.home).sessions_for(cfg.slug, number)
    out = []
    for sid in dict.fromkeys([primary, *found]):
        info = session_info(sid)
        if info["exists"]:
            meta = found.get(sid) or _workspace_fields(copilot_home() / "session-state" / sid)
            meta = {"client": meta.get("client") or meta.get("client_name", ""), "name": meta.get("name", "")}
            client = meta["client"]
            terminal = sid == primary or any(k in client for k in ("cli", "autopilot"))
            info["where"] = "VS Code" if "vscode" in client else "terminal" if terminal else client or "other"
            info["name"] = meta.get("name", "")
            out.append(info)

    def newest_first(info: dict[str, object]) -> float:
        updated = info.get("updated", 0)
        return -float(updated) if isinstance(updated, int | float) else 0.0

    return out[:1] + sorted(out[1:], key=newest_first)


def default_session_id(cfg: Config, number: int) -> str:
    """Derived from the PR's URL on its forge, so the same PR always gets the same session."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, forges.make(cfg).pr_url(number)))


def session_id_for(cfg: Config, number: int, ledger_entry: dict | None) -> str:
    return (ledger_entry or {}).get("session") or default_session_id(cfg, number)


def session_info(session_id: str) -> dict[str, object]:
    folder = copilot_home() / "session-state" / session_id
    if not folder.is_dir():
        return {"id": session_id, "exists": False, "active": False}
    events = folder / "events.jsonl"
    stamp = (events if events.exists() else folder).stat().st_mtime
    pids = (re.fullmatch(r"inuse\.(\d+)\.lock", p.name) for p in folder.glob("inuse.*.lock"))
    active = any(_pid_alive(int(m.group(1))) for m in pids if m)
    return {"id": session_id, "exists": True, "updated": round(stamp), "active": active}


def _pid_alive(pid: int) -> bool:
    """Copilot leaves ``inuse.<pid>.lock`` behind while a session is open; check that process still runs."""
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


def review_prompt(cfg: Config, number: int, brief: Path) -> str:
    return (
        f"{pr_marker(cfg.slug, number)} Use the readthrough skill to run a deep review session for {cfg.repo}#{number}. "
        f"The study brief is at {brief.as_posix()}"
    )


@dataclass
class ReviewLaunch:
    argv: list[str]
    cwd: Path
    session_id: str
    resuming: bool
    title: str


def plan_review(
    cfg: Config, number: int, *, ledger_entry: dict | None, fresh: bool = False, session_id: str | None = None
) -> ReviewLaunch:
    """Start or resume this PR's session. ``session_id`` resumes a specific session already linked to the PR."""
    copilot = shutil.which("copilot")
    if not copilot:
        raise RuntimeError(
            "GitHub Copilot CLI (`copilot`) is not on PATH. Install it with `winget install GitHub.Copilot`."
        )
    if cfg.clone is None:
        raise RuntimeError(
            f"No local clone of {cfg.repo} configured. Set it with `readthrough onboard {cfg.repo} --clone PATH`."
        )
    if session_id is not None:
        linked = {s["id"] for s in pr_sessions(cfg, number, session_id_for(cfg, number, ledger_entry))}
        if session_id not in linked:
            raise ValueError(f"Session {session_id} isn't linked to #{number}")
    else:
        session_id = str(uuid.uuid4()) if fresh else session_id_for(cfg, number, ledger_entry)
    resuming = bool(session_info(session_id)["exists"])
    common = ["--add-dir", str(cfg.repo_dir), *cfg.copilot_args]
    short = cfg.repo.rsplit("/", 1)[-1]
    if resuming:
        argv = [copilot, f"--resume={session_id}", *common]
    else:
        brief = cfg.briefs_dir / f"pr-{number}.md"
        argv = [
            copilot,
            f"--session-id={session_id}",
            f"--name={short} #{number} deep review",
            "-i", review_prompt(cfg, number, brief),
            *common,
        ]  # fmt: skip
    return ReviewLaunch(argv=argv, cwd=cfg.clone, session_id=session_id, resuming=resuming, title=f"{short} #{number}")


def launch_in_terminal(launch: ReviewLaunch, cfg: Config) -> str:
    """Open a new terminal window running the session, with the terminal chosen in config. Returns its name."""
    from readthrough import terminals

    command = terminals.Command(argv=launch.argv, cwd=launch.cwd, title=launch.title)
    return terminals.launch(command, terminal=cfg.terminal, options=cfg.terminal_options)


@dataclass
class ReadthroughUrl:
    action: str
    repo: str | None = None
    number: int | None = None


def parse_readthrough_url(url: str, *, allowed_repos: list[str]) -> ReadthroughUrl:
    """Parse ``readthrough://<action>/<forge>/<owner>/<repo>[/<pr>]``. Anything unexpected is rejected.

    Any web page can try to open this scheme, so this is strict: a fixed set of actions, a numeric PR,
    and only repos you've onboarded (``allowed_repos`` holds their slugs; a GitHub repo may also be named
    by its bare ``owner/repo``). ``readthrough://open`` with no repo opens the dashboard's front page.
    """
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != URL_SCHEME:
        raise ValueError(f"Not a {URL_SCHEME}: URL")
    action = parts.netloc.lower()
    if action not in URL_ACTIONS:
        raise ValueError(f"Unknown action {action!r}")
    if parts.query or parts.fragment:
        raise ValueError("Unexpected query or fragment")
    segments = [s for s in parts.path.split("/") if s]
    if action == "open" and not segments:
        return ReadthroughUrl(action)
    if not all(_SEGMENT_RE.match(s) for s in segments):
        raise ValueError("Unexpected characters in the path")
    number: int | None = None
    if action == "review":
        if len(segments) < 3 or not re.fullmatch(r"\d{1,7}", segments[-1]):
            raise ValueError(f"review needs a repo and a PR number, e.g. {URL_SCHEME}://review/github/owner/name/1234")
        number = int(segments.pop())
    if len(segments) < 2:
        raise ValueError(f"{action} needs a repo, e.g. {URL_SCHEME}://{action}/github/owner/name")
    repo = "/".join(segments)
    match = next((slug for slug in allowed_repos if _same_repo(repo, slug)), None)
    if match is None:
        raise ValueError(f"{repo!r} isn't an onboarded repo")
    return ReadthroughUrl(action, match, number)


def review_url(slug: str, number: int) -> str:
    return f"{URL_SCHEME}://review/{slug}/{number}"


def vscode_session_url(session_id: str) -> str:
    """Open an existing Copilot CLI session in VS Code's Agents window (VS Code 1.138+)."""
    return f"vscode://agents/agent-host-session/copilotcli/{session_id}"


def vscode_new_session_url(prompt: str, workspace: Path | None) -> str:
    """Start a VS Code agent session with the prompt prefilled. VS Code does not auto-submit it."""
    url = f"vscode://agents/new?prompt={quote(prompt, safe='')}"
    return url + (f"&workspace={quote(workspace.resolve().as_uri(), safe='')}" if workspace else "")


# ---------------------------------------------------------------- worktrees


def worktree_path(cfg: Config, number: int) -> Path:
    return cfg.worktrees_dir / f"pr-{number}"


def repo_remote(cfg: Config) -> str:
    """The clone's remote for this repo: as configured, else the one whose URL points at it, else origin."""
    if cfg.remote:
        return cfg.remote
    from readthrough.config import remote_for

    return (remote_for(cfg.clone, cfg.repo) if cfg.clone else None) or "origin"


def ensure_worktree(cfg: Config, number: int, *, base: str = "main") -> tuple[Path, str, str]:
    """Check out the PR's current head in its own worktree. Returns (path, merge_base, head_sha)."""
    if cfg.clone is None:
        raise RuntimeError(f"No local clone of {cfg.repo} configured.")
    clone, ref, remote = str(cfg.clone), f"refs/remotes/readthrough/pr-{number}", repo_remote(cfg)
    run(["git", "-C", clone, "fetch", "--quiet", remote, f"+{forges.make(cfg).pr_ref(number)}:{ref}", base])
    path = worktree_path(cfg, number)
    if (path / ".git").exists():
        run(["git", "-C", str(path), "checkout", "--quiet", "--detach", ref])
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "-C", clone, "worktree", "add", "--quiet", "--detach", str(path), ref])
    head = run(["git", "-C", str(path), "rev-parse", "HEAD"]).strip()
    merge_base = run(["git", "-C", clone, "merge-base", ref, f"{remote}/{base}"]).strip()
    return path, merge_base, head


def remove_worktree(cfg: Config, number: int) -> bool:
    path = worktree_path(cfg, number)
    if cfg.clone is None or not path.exists():
        return False
    run(["git", "-C", str(cfg.clone), "worktree", "remove", "--force", str(path)])
    run(["git", "-C", str(cfg.clone), "update-ref", "-d", f"refs/remotes/readthrough/pr-{number}"], check=False)
    return True

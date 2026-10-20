"""Deep-review sessions: one persistent agent session per PR (Copilot CLI or Claude Code), plus an isolated
git worktree.

The session ID is derived from the PR URL, so the same button starts the session the first time and
resumes it afterwards. ``review --fresh`` starts over with a random ID and remembers it in the ledger.

Sessions started anywhere else (VS Code's agent window, a pasted prompt) get IDs readthrough didn't choose.
Every prompt readthrough writes starts with a marker like ``[readthrough:github/owner/name#123]`` (the repo's
slug: its forge, then its path there), and ``SessionIndex`` links any session of any installed agent whose
first message carries one back to its PR.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from readthrough import agents, forges
from readthrough.agents import Agent, SessionInfo
from readthrough.config import Config
from readthrough.faults import ForgeError
from readthrough.model import PATH_SEGMENT, PR_NUMBER
from readthrough.process import run
from readthrough.tuning import default_tuning

log = logging.getLogger(__name__)

URL_SCHEME = "readthrough"
URL_ACTIONS = ("review", "refresh", "open")
_SEGMENT_RE = re.compile(rf"^{PATH_SEGMENT}$")

MARKER_RE = re.compile(rf"\[readthrough:({PATH_SEGMENT}(?:/{PATH_SEGMENT})+)#({PR_NUMBER})\]")


def pr_marker(slug: str, number: int) -> str:
    return f"[readthrough:{slug}#{number}]"


def _same_repo(found: str, slug: str) -> bool:
    """Markers name the repo by slug; ones written before there were forges name GitHub repos by path alone."""
    found, slug = found.lower(), slug.lower()
    return found == slug or (slug.startswith(f"{forges.DEFAULT}/") and found == slug.split("/", 1)[1])


def _agents(cfg: Config) -> list[Agent]:
    """Every agent this machine has, so sessions started in any of them are found."""
    return [a for a in agents.all_agents(cfg.agent_options) if a.available()]


class SessionIndex:
    """Which agent sessions belong to which PR, found by the marker in their first message.

    The answer for a session never changes, so it's cached in ``session-index.json``; each scan only
    reads sessions it hasn't seen. Sessions without a first message yet are checked again later.
    """

    def __init__(self, path: Path, available: list[Agent]) -> None:
        self.path = path
        self.agents = available
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
            records = {r.id: (agent, r) for agent in self.agents for r in agent.sessions()}
            changed = False
            for gone in set(self._known) - set(records):
                del self._known[gone]
                changed = True
            for sid in set(records) - set(self._known):
                agent, record = records[sid]
                first = agent.first_user_message(record)
                if first is None:
                    # Still starting up? Look again later. Long-idle sessions that never got a message are settled.
                    if time.time() - record.updated < 3600:
                        continue
                    first = ""
                match = MARKER_RE.search(first[:2000])
                meta = agent.session_meta(record)
                self._known[sid] = (
                    {
                        "pr": f"{match.group(1).lower()}#{match.group(2)}",
                        "agent": agent.name,
                        "client": meta.get("client", ""),
                        "name": meta.get("name", ""),
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

    def agent_of(self, session_id: str) -> str | None:
        with self._lock:
            meta = self._known.get(session_id)
            return meta.get("agent") if meta else None


_indexes: dict[Path, SessionIndex] = {}
_indexes_lock = threading.Lock()


def session_index(cfg: Config) -> SessionIndex:
    with _indexes_lock:
        if cfg.home not in _indexes:
            _indexes[cfg.home] = SessionIndex(cfg.home / "session-index.json", _agents(cfg))
        return _indexes[cfg.home]


def agent_for(cfg: Config, session_id: str | None = None) -> Agent:
    """The agent a session belongs to (by the index, else by asking each), or the configured one for a new session."""
    configured = agents.resolve(cfg.agent, cfg.agent_options)
    if session_id is None:
        return configured
    name = session_index(cfg).agent_of(session_id)
    if name:
        return agents.make(name, cfg.agent_options)
    for agent in _agents(cfg):
        if agent.name != configured.name and agent.session_info(session_id).exists:
            return agent
    return configured


def pr_sessions(cfg: Config, number: int, primary: str) -> list[dict[str, object]]:
    """Every agent session for this PR, the default one first, then the most recently used."""
    found = session_index(cfg).sessions_for(cfg.slug, number)
    out = []
    for sid in dict.fromkeys([primary, *found]):
        agent = agent_for(cfg, sid)
        info = agent.session_info(sid)
        if info.exists:
            meta = found.get(sid) or {}
            client = meta.get("client", "")
            terminal = sid == primary or any(k in client for k in ("cli", "autopilot", "claude"))
            out.append({
                **info.payload(),
                "where": "VS Code" if "vscode" in client else "terminal" if terminal else client or "other",
                "name": meta.get("name", ""),
                "agent": agent.name,
                "agent_label": agent.label,
                "editor_url": agent.editor_url(sid),
            })  # fmt: skip

    def newest_first(info: dict[str, object]) -> float:
        updated = info.get("updated", 0)
        return -float(updated) if isinstance(updated, int | float) else 0.0

    return out[:1] + sorted(out[1:], key=newest_first)


def default_session_id(cfg: Config, number: int) -> str:
    """Derived from the PR's URL on its forge, so the same PR always gets the same session."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, forges.make(cfg).pr_url(number)))


def session_id_for(cfg: Config, number: int, ledger_entry: dict | None) -> str:
    return (ledger_entry or {}).get("session") or default_session_id(cfg, number)


def session_info(cfg: Config, session_id: str) -> dict[str, object]:
    """Whether the session exists, when it was last written, and whether it's open now (when the agent can say)."""
    info: SessionInfo = agent_for(cfg, session_id).session_info(session_id)
    return info.payload()


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
    agent: str = "copilot"


def plan_review(
    cfg: Config, number: int, *, ledger_entry: dict | None, fresh: bool = False, session_id: str | None = None
) -> ReviewLaunch:
    """Start or resume this PR's session. ``session_id`` resumes a specific session already linked to the PR.

    A new session runs in the configured agent; an existing one is resumed in the agent that owns it, so a
    Claude Code session linked by its marker resumes in Claude Code even when Copilot is the default.
    """
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
    agent = agent_for(cfg, session_id)
    if not agent.available():
        raise RuntimeError(f"{agent.label} (`{agent.program}`) is not on PATH. Install it with `{agent.install_hint}`.")
    resuming = agent.session_info(session_id).exists
    short = cfg.repo.rsplit("/", 1)[-1]
    if resuming:
        argv = agent.resume_argv(session_id, add_dirs=[cfg.repo_dir], extra=cfg.agent_args)
    else:
        brief = cfg.briefs_dir / f"pr-{number}.md"
        argv = agent.new_session_argv(
            session_id=session_id, name=f"{short} #{number} deep review", prompt=review_prompt(cfg, number, brief),
            add_dirs=[cfg.repo_dir], extra=cfg.agent_args,
        )  # fmt: skip
    return ReviewLaunch(argv=argv, cwd=cfg.clone, session_id=session_id, resuming=resuming, title=f"{short} #{number}",
                        agent=agent.name)  # fmt: skip


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
    segments = [unquote(s) for s in parts.path.split("/") if s]  # a project called "Delivery Team" travels as %20
    if action == "open" and not segments:
        return ReadthroughUrl(action)
    if not all(_SEGMENT_RE.match(s) for s in segments):
        raise ValueError("Unexpected characters in the path")
    number: int | None = None
    if action == "review":
        if len(segments) < 3 or not re.fullmatch(PR_NUMBER, segments[-1]):
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
    return f"{URL_SCHEME}://review/{quote(slug)}/{number}"


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

    return (remote_for(cfg.clone, cfg.repo, cfg.forge) if cfg.clone else None) or "origin"


def ensure_worktree(cfg: Config, number: int, *, base: str = "main") -> tuple[Path, str, str]:
    """Check out the PR's current head in its own worktree. Returns (path, merge_base, head_sha)."""
    if cfg.clone is None:
        raise RuntimeError(f"No local clone of {cfg.repo} configured.")
    clone, ref, remote = str(cfg.clone), f"refs/remotes/readthrough/pr-{number}", repo_remote(cfg)
    run(["git", "-C", clone, "fetch", "--quiet", remote, f"+{forges.make(cfg).pr_ref(number)}:{ref}", base])
    path = worktree_path(cfg, number)
    if (path / ".git").exists() and not _worktree_attached(clone, path):
        # The clone or the data directory moved since this was made, so its `.git` file points nowhere.
        aside = path.with_name(f"{path.name}.detached-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}")
        path.rename(aside)
        log.warning(
            "The worktree for #%d no longer belonged to %s; moved it to %s and made a fresh one", number, clone, aside
        )
    if (path / ".git").exists():
        run(["git", "-C", str(path), "checkout", "--quiet", "--detach", ref])
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "-C", clone, "worktree", "add", "--quiet", "--detach", str(path), ref])
    head = run(["git", "-C", str(path), "rev-parse", "HEAD"]).strip()
    merge_base = run(["git", "-C", clone, "merge-base", ref, f"{remote}/{base}"]).strip()
    return path, merge_base, head


def _worktree_attached(clone: str, path: Path) -> bool:
    """Whether git still knows ``path`` as a worktree of ``clone``, repairing the link if moving broke it."""
    run(["git", "-C", clone, "worktree", "repair", str(path)], check=False)
    try:
        common = run(["git", "-C", str(path), "rev-parse", "--git-common-dir"]).strip()
    except ForgeError:
        return False
    return Path(path, common).resolve() == Path(clone, ".git").resolve()


def remove_worktree(cfg: Config, number: int) -> bool:
    path = worktree_path(cfg, number)
    if cfg.clone is None or not path.exists():
        return False
    run(["git", "-C", str(cfg.clone), "worktree", "remove", "--force", str(path)])
    run(["git", "-C", str(cfg.clone), "update-ref", "-d", f"refs/remotes/readthrough/pr-{number}"], check=False)
    return True

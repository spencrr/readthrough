"""Copilot-assisted onboarding: a Copilot session reads the repo and proposes a profile.

The session gets read-only tools over the repo and one tool to propose a profile. Each proposal is
validated and measured with the same checks ``readthrough onboard --accept`` uses, and the result goes
back to the model so it can fix problems. The best proposal wins; you still review it before accepting.
"""

# No `from __future__ import annotations`: the Copilot SDK reads tool parameter types at runtime.

import asyncio
import logging
import tomllib
import uuid
from collections import Counter
from collections.abc import Callable
from importlib.resources import files
from typing import Any

from readthrough.config import Config
from readthrough.forges import Forge
from readthrough.onboard import RepoFacts, author_table, check_profile_text, draft_profile, measure, report
from readthrough.process import run
from readthrough.profile import ProfileError, parse_profile
from readthrough.rules import glob_to_regex

log = logging.getLogger(__name__)


SYSTEM_MESSAGE = """\
You are drafting an architecture profile for {repo}. readthrough uses it to place each open pull request
on the repo's architecture, tell reviewers which component owns what, and ask them good questions.

How to work:
- Start from the heuristic draft you're given. Its directory-based components are a starting point,
  not the answer: find the real boundaries (packages, layers, plugin families, entry points) by
  reading the code and docs with your tools.
- Aim for 6 to 20 components a newcomer would recognise, grouped into 3 to 6 layers ordered from
  entry points down to shared foundations, plus a support layer for tests, docs, and infra.
- For each source component, write `responsibility` and `does_not_own` only when the code or docs
  back them up. `does_not_own` should name the neighbouring component that owns each thing. If you
  aren't sure, leave both out; a wrong contract is worse than none.
- If the repo has an architecture doc with one section per component, point [contracts] at it and set
  each component's `contract` to its heading instead of writing responsibilities inline.
- Add a few [[signals]] for risky paths you can see (migrations, auth, public API, dependency
  manifests, CI config) and [review] guidance distilled from the contributing docs.
- Keep an [authors] section. Fill internal_teams or internal_orgs only if the docs or CODEOWNERS
  make the maintainer group obvious; otherwise leave it empty.
- Call propose_profile with the complete TOML. It replies with coverage and problems. Fix them and
  propose again until there are no problems. Stop after that; don't polish forever.
- Finish with 3 to 6 short lines: what you based the map on, and what the maintainer should check.
- You can't edit files or run commands. The tools listed are everything you have.
"""


def copilot_draft(
    cfg: Config, forge: Forge | None, facts: RepoFacts, *, on_progress: Callable[[str], None] = log.info
) -> str:
    """Run a Copilot drafting session and return the best profile it proposed (TOML text)."""
    try:
        import copilot  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("--copilot needs the Copilot SDK. Install readthrough with the [study] extra.") from exc
    drafter = _Drafter(cfg, forge, facts, on_progress)
    asyncio.run(drafter.run())
    if drafter.best is None:
        raise RuntimeError(
            "Copilot didn't propose a valid profile. Try again, or start from the plain draft (--update)."
        )
    problems, share, text = drafter.best
    on_progress(f"Best proposal: {share:.0%} coverage, {len(problems)} problem(s) left")
    if drafter.summary:
        on_progress("Copilot's notes:\n" + drafter.summary.strip())
    header = f"# Drafted with Copilot by `readthrough onboard {facts.repo} --copilot`. Review it before accepting.\n"
    return header + mark_drafter(text.lstrip(), "copilot")


def mark_drafter(text: str, drafter: str) -> str:
    """Set the profile's top-level ``drafted_by`` (it must sit before the first table header)."""
    lines = [line for line in text.splitlines() if not line.startswith("drafted_by")]
    at = next((i + 1 for i, line in enumerate(lines) if line.startswith("schema")), 0)
    lines.insert(at, f'drafted_by = "{drafter}"')
    return "\n".join(lines) + "\n"


class _Drafter:
    def __init__(self, cfg: Config, forge: Forge | None, facts: RepoFacts, on_progress: Callable[[str], None]) -> None:
        self.cfg, self.forge, self.facts, self.say = cfg, forge, facts, on_progress
        self.tree = set(facts.tree)
        self.clone = cfg.clone if cfg.clone and (cfg.clone / ".git").exists() else None
        self.best: tuple[list[str], float, str] | None = None
        self.proposals = 0
        self.summary = ""

    # ---- the session

    async def run(self) -> None:
        from copilot import CopilotClient
        from copilot.generated.rpc import PermissionDecisionUserNotAvailable

        client = CopilotClient()
        await client.start()
        try:
            tools = self._tools()
            options: dict[str, Any] = {
                "tools": tools,
                "available_tools": [t.name for t in tools],
                "on_permission_request": lambda _req, _ctx: PermissionDecisionUserNotAvailable(),
                "client_name": "readthrough-onboard",
                "working_directory": str(self.clone or self.cfg.home),
                "enable_skills": False,
                "system_message": {"mode": "append", "content": SYSTEM_MESSAGE.format(repo=self.facts.repo)},
            }
            if self.cfg.study_model:
                options["model"] = self.cfg.study_model
            session = await client.create_session(session_id=str(uuid.uuid4()), **options)
            try:
                self.summary = await self._send(session, self._first_prompt())
            finally:
                await session.disconnect()
        finally:
            await client.stop()

    async def _send(self, session: Any, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        last: list[str] = []
        error: list[str] = []

        def on_event(event: Any) -> None:
            kind = getattr(event.type, "value", str(event.type))
            data = event.data
            if kind == "assistant.message":
                last[:] = [getattr(data, "content", None) or ""]
            elif kind == "tool.execution_start":
                name = getattr(data, "tool_name", "?")
                if name != "propose_profile":
                    self.say(f"Copilot: {name.replace('_', ' ')}")
            elif kind == "session.error":
                error.append(getattr(data, "message", None) or "Copilot reported an error")
                loop.call_soon_threadsafe(done.set)
            elif kind == "session.idle":
                loop.call_soon_threadsafe(done.set)

        unsubscribe = session.on(on_event)
        try:
            await session.send(prompt)
            await asyncio.wait_for(done.wait(), timeout=self.cfg.tuning.study.onboarding_turn_timeout_seconds)
        finally:
            unsubscribe()
        if error and self.best is None:
            raise RuntimeError(error[0])
        return last[0] if last else ""

    def _first_prompt(self) -> str:
        f = self.facts
        reference = files("readthrough").joinpath("assets/profile-reference.md").read_text(encoding="utf-8")
        draft = draft_profile(f)
        profile = parse_profile(tomllib.loads(draft), origin="heuristic draft")
        cov = measure(profile, f, raw=tomllib.loads(draft))
        docs = "\n\n".join(
            f"<doc path={p!r}>\n{text[: self.cfg.tuning.study.onboarding_doc_excerpt_chars]}\n</doc>"
            for p, text in f.docs.items()
            if text
        )
        owners = "\n".join(f"{glob} {' '.join(o)}" for glob, o in f.codeowners[:60]) or "(no CODEOWNERS)"
        parts = [
            f"Draft the architecture profile for {f.repo} (default branch {f.default_branch}).",
            f"<profile_reference>\n{reference}\n</profile_reference>",
            f"<top_directories>\n{self._dirs('', 2)}\n</top_directories>",
            f"<codeowners>\n{owners}\n</codeowners>",
            f"<docs>\n{docs or '(no architecture or contributing docs found)'}\n</docs>",
            f"<recent_authors>\n{self._authors(profile)}\n</recent_authors>",
            f"<heuristic_draft>\n{draft}\n</heuristic_draft>",
            f"<heuristic_draft_check>\n{report(cov, profile, [])}\n</heuristic_draft_check>",
            ""
            if self.clone
            else "There's no local clone, so search_code isn't available; read_file works through the API.",
        ]
        return "\n\n".join(p for p in parts if p)

    # ---- helpers behind the tools

    def _dirs(self, prefix: str, depth: int) -> str:
        prefix = prefix.strip("/")
        base = prefix.count("/") + 1 if prefix else 0
        counts: Counter[str] = Counter()
        churn: Counter[str] = Counter()
        for path in self.facts.tree:
            if prefix and not path.startswith(prefix + "/"):
                continue
            parts = path.split("/")
            key = "/".join(parts[: base + depth]) + ("/" if len(parts) > base + depth else "")
            counts[key] += 1
            churn[key] += self.facts.churn.get(path, 0)
        rows = [f"{k}  files={counts[k]}  recent_churn={churn[k]}" for k in sorted(counts)]
        if len(rows) > 400:
            rows = [*rows[:400], f"... {len(rows) - 400} more; list a narrower prefix"]
        return "\n".join(rows) or f"Nothing under {prefix!r}"

    def _authors(self, profile: Any) -> str:
        rows = author_table(profile, self.facts)
        return "\n".join(f"{login} association={assoc} kind={kind} prs={n}" for login, assoc, kind, n in rows[:60])

    def _read(self, path: str, start: int, end: int) -> str:
        path = path.strip().lstrip("/")
        if path not in self.tree:
            return f"{path} isn't a file in the repo. Use list_directory to find paths."
        if self.clone:
            text = (self.clone / path).read_text(encoding="utf-8", errors="replace")
        elif self.forge is not None:
            text = self.forge.read_file(path) or ""
        else:
            return "read_file needs a local clone or forge access."
        lines = text.splitlines()
        end = min(end, start + 399, len(lines))
        body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
        return f"{path} lines {start}-{end} of {len(lines)}\n{body}"

    def _search(self, pattern: str, path_glob: str | None) -> str:
        if not self.clone:
            return "search_code needs a local clone; use read_file and list_directory instead."
        cmd = ["git", "-C", str(self.clone), "grep", "-n", "-I", "-E", "--max-count=20", "-e", pattern]
        if path_glob:
            cmd += ["--", path_glob]
        return _clip(run(cmd, check=False), limit=self.cfg.tuning.study.onboarding_tool_output_chars) or "No matches."

    def _propose(self, text: str) -> str:
        self.proposals += 1
        try:
            profile, cov = check_profile_text(text, self.facts, origin="proposal")
        except (ProfileError, tomllib.TOMLDecodeError) as exc:
            self.say(f"Proposal {self.proposals}: invalid ({str(exc)[:120]})")
            return f"Invalid profile: {exc}\nFix it and propose again."
        if profile.repo.lower() != self.facts.repo.lower():
            return f"repo must be {self.facts.repo!r}."
        problems = cov.problems()
        self.say(f"Proposal {self.proposals}: {cov.share:.0%} coverage, {len(problems)} problem(s)")
        key = (len(problems), -cov.share)
        if self.best is None or key < (len(self.best[0]), -self.best[1]):
            self.best = (problems, cov.share, text)
        verdict = (
            "No problems: this is good enough to accept. Stop and summarise."
            if not problems
            else "Fix these and propose again."
        )
        return report(cov, profile, author_table(profile, self.facts)) + "\n\n" + verdict

    # ---- tools

    def _tools(self) -> list[Any]:
        from copilot import define_tool
        from pydantic import BaseModel, Field

        drafter = self

        class DirParams(BaseModel):
            prefix: str = Field("", description="Directory to list, e.g. 'src/app'. Empty for the repo root.")
            depth: int = Field(2, ge=1, le=4, description="How many levels below the prefix to show")

        class ReadParams(BaseModel):
            path: str = Field(description="Repo-relative file path")
            start_line: int = Field(1, ge=1)
            end_line: int = Field(200, ge=1)

        class SearchParams(BaseModel):
            pattern: str = Field(description="Extended regex for git grep -E")
            path_glob: str | None = Field(None, description="Optional pathspec, e.g. 'src/**/*.py'")

        class GlobParams(BaseModel):
            glob: str = Field(description="A profile path glob, e.g. 'src/app/score/**'")

        class ProposeParams(BaseModel):
            toml: str = Field(description="The complete profile as TOML")

        @define_tool(
            name="list_directory",
            description="Directories and files with file counts and recent PR churn.",
            skip_permission=True,
        )
        def list_directory(params: DirParams) -> str:
            return drafter._dirs(params.prefix, params.depth)

        @define_tool(
            name="read_file",
            description="Read lines of a file on the default branch. Lines are numbered.",
            skip_permission=True,
        )
        async def read_file(params: ReadParams) -> str:
            return await asyncio.to_thread(drafter._read, params.path, params.start_line, params.end_line)

        @define_tool(
            name="search_code", description="git grep the repo. Returns path:line:text matches.", skip_permission=True
        )
        async def search_code(params: SearchParams) -> str:
            return await asyncio.to_thread(drafter._search, params.pattern, params.path_glob)

        @define_tool(
            name="match_glob",
            description="Which repo files a profile glob matches, and their recent churn.",
            skip_permission=True,
        )
        def match_glob(params: GlobParams) -> str:
            rx = glob_to_regex(params.glob)
            hits = [p for p in drafter.facts.tree if rx.match(p)]
            churn = sum(drafter.facts.churn.get(p, 0) for p in hits)
            return (
                f"{len(hits)} files, recent churn {churn}\n"
                + "\n".join(hits[:40])
                + ("\n..." if len(hits) > 40 else "")
            )

        @define_tool(
            name="propose_profile",
            description="Validate and measure a complete profile. Returns coverage and problems.",
            skip_permission=True,
        )
        def propose_profile(params: ProposeParams) -> str:
            return drafter._propose(params.toml)

        return [list_directory, read_file, search_code, match_glob, propose_profile]


def _clip(text: str, *, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} characters]"

"""Model-assisted onboarding: a model reads the repo and proposes a profile.

The conversation runs on the configured model runtime (``runtime`` in config.toml: the Copilot SDK, Anthropic,
or OpenAI; see ``runtimes/``) with read-only tools over the repo and one tool to propose a profile. Each
proposal is validated and measured with the same checks ``readthrough onboard --accept`` uses, and the
result goes back to the model so it can fix problems. The best proposal wins; you still review it before
accepting, and the profile says a model drafted it, and which.
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

from readthrough import runtimes
from readthrough.config import Config
from readthrough.forges import Forge
from readthrough.onboard import RepoFacts, author_table, check_profile_text, draft_profile, measure, report
from readthrough.process import run
from readthrough.profile import ProfileError, parse_profile
from readthrough.rules import glob_to_regex

log = logging.getLogger(__name__)

MODEL_DRAFTER = "model"  # the drafted_by a model's draft carries (profile.DRAFTERS); the header names the model


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


def model_draft(
    cfg: Config, forge: Forge | None, facts: RepoFacts, *, on_progress: Callable[[str], None] = log.info
) -> str:
    """Run a drafting conversation on the configured runtime and return the best profile it proposed (TOML)."""
    _, ok, why = runtimes.resolve(cfg.runtime, cfg.runtime_options)
    if not ok:
        raise RuntimeError(f"--assisted needs a model runtime: {why}.")
    drafter = _Drafter(cfg, forge, facts, on_progress)
    asyncio.run(drafter.run())
    if drafter.best is None:
        raise RuntimeError(
            "The model didn't propose a valid profile. Try again, or start from the plain draft (--update)."
        )
    problems, share, text = drafter.best
    on_progress(f"Best proposal: {share:.0%} coverage, {len(problems)} problem(s) left")
    if drafter.summary:
        on_progress("The model's notes:\n" + drafter.summary.strip())
    who = (
        f"{drafter.model} ({drafter.runtime_name} runtime)" if drafter.model else f"the {drafter.runtime_name} runtime"
    )
    header = f"# Drafted with {who} by `readthrough onboard {facts.repo} --assisted`. Review it before accepting.\n"
    return header + mark_drafter(text.lstrip(), MODEL_DRAFTER)


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
        self.runtime_name = ""
        self.model: str | None = None  # the model the runtime reported using, for the profile's header

    # ---- the session

    async def run(self) -> None:
        runtime = runtimes.make(self.cfg.runtime, self.cfg.runtime_options)
        self.runtime_name = runtime.name
        try:
            conversation = runtimes.Conversation(
                key=f"readthrough:onboarding:{uuid.uuid4()}",
                system=SYSTEM_MESSAGE.format(repo=self.facts.repo),
                tools=self._tools(),
                working_directory=str(self.clone or self.cfg.home),
                model=self.cfg.study_model,
                resume=False,
            )
            session = await runtime.open(conversation)
            try:
                self.summary = await self._send(session, self._first_prompt())
            finally:
                await session.close()
        finally:
            await runtime.stop()

    def _record_usage(self, used: runtimes.Usage) -> None:
        from readthrough.assistants import record_usage

        self.model = used.model or self.model
        try:
            record_usage(self.cfg, used, purpose="draft", ref="onboarding", by=MODEL_DRAFTER)
        except Exception:
            log.exception("Couldn't record model usage for the onboarding draft")

    async def _send(self, session: runtimes.Session, prompt: str) -> str:
        final = ""
        error: str | None = None

        async def consume() -> None:
            nonlocal final, error
            async for event in session.send(prompt):
                if event.kind == "usage" and event.usage is not None:
                    self._record_usage(event.usage)
                elif event.kind == "tool" and event.name != "propose_profile":
                    self.say(f"model: {event.name.replace('_', ' ')}")
                elif event.kind == "error":
                    error = event.text
                elif event.kind == "done":
                    final = event.text

        await asyncio.wait_for(consume(), timeout=self.cfg.tuning.study.onboarding_turn_timeout_seconds)
        if error and self.best is None:
            raise RuntimeError(error)
        return final

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

    def _tools(self) -> list[runtimes.ToolSpec]:
        drafter = self

        async def list_directory(args: dict[str, Any]) -> str:
            return drafter._dirs(args.get("prefix") or "", int(args.get("depth") or 2))

        async def read_file(args: dict[str, Any]) -> str:
            return await asyncio.to_thread(
                drafter._read, args["path"], int(args.get("start_line") or 1), int(args.get("end_line") or 200)
            )

        async def search_code(args: dict[str, Any]) -> str:
            return await asyncio.to_thread(drafter._search, args["pattern"], args.get("path_glob"))

        async def match_glob(args: dict[str, Any]) -> str:
            rx = glob_to_regex(args["glob"])
            hits = [p for p in drafter.facts.tree if rx.match(p)]
            churn = sum(drafter.facts.churn.get(p, 0) for p in hits)
            return (
                f"{len(hits)} files, recent churn {churn}\n"
                + "\n".join(hits[:40])
                + ("\n..." if len(hits) > 40 else "")
            )

        async def propose_profile(args: dict[str, Any]) -> str:
            return drafter._propose(args["toml"])

        s = runtimes.ToolSpec
        return [
            s("list_directory", "Directories and files with file counts and recent PR churn.", {
                "properties": {
                    "prefix": {"type": "string", "description": "Directory to list, e.g. 'src/app'. Empty for the repo root.", "default": ""},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 4, "description": "How many levels below the prefix to show", "default": 2},
                },
            }, list_directory),
            s("read_file", "Read lines of a file on the default branch. Lines are numbered.", {
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "end_line": {"type": "integer", "minimum": 1, "default": 200},
                },
                "required": ["path"],
            }, read_file),
            s("search_code", "git grep the repo. Returns path:line:text matches.", {
                "properties": {
                    "pattern": {"type": "string", "description": "Extended regex for git grep -E"},
                    "path_glob": {"type": "string", "description": "Optional pathspec, e.g. 'src/**/*.py'"},
                },
                "required": ["pattern"],
            }, search_code),
            s("match_glob", "Which repo files a profile glob matches, and their recent churn.", {
                "properties": {"glob": {"type": "string", "description": "A profile path glob, e.g. 'src/app/score/**'"}},
                "required": ["glob"],
            }, match_glob),
            s("propose_profile", "Validate and measure a complete profile. Returns coverage and problems.", {
                "properties": {"toml": {"type": "string", "description": "The complete profile as TOML"}},
                "required": ["toml"],
            }, propose_profile),
        ]  # fmt: skip


def _clip(text: str, *, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} characters]"

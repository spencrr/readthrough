"""Study assistants: what answers when you press Quiz me, Brief me, or Hint in the Study tab.

Two are built in, and ``assistant = "auto"`` in ``config.toml`` picks the first that works:

* ``copilot`` runs one GitHub Copilot session per PR through the Copilot SDK (the ``[study]`` extra). The
  session runs in the PR's worktree but can't touch it: every built-in tool is off and permission requests
  are refused. It has read-only tools (``get_diff``, ``read_file``, ``search_code``) and append-only ones
  that write to the study log (``ask_quiz_question``, ``assess_answer``, ``assess_teachback``,
  ``append_to_brief``, ``record_finding``). It grades answers against the code.
* ``rules`` needs nothing installed. It asks the questions the repo's profile already carries (the
  always-questions, each touched component's questions, the author-kind questions, and one per signal),
  points at files for hints, and assembles a briefing from the PR's metadata. It never grades: there is no
  answer key, and self-awarded verdicts are not evidence. Instead, after a component's last question it
  shows that component's contract so you can check yourself, and the log keeps your answers and any
  "what I missed" notes.

Every entry an assistant writes carries its name in ``by``, so the log always shows who said what.
The engine (``engine.py``) owns the loop, the busy state, and the learner's side of the log; an assistant
only gets a ``Harness`` to append entries and broadcast progress through.
"""

# No `from __future__ import annotations` here: the Copilot SDK reads tool parameter types with
# get_type_hints, and the parameter models are local classes that string annotations can't resolve.

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from readthrough import review, study, usage
from readthrough.agents import Copilot
from readthrough.config import AUTO, Config
from readthrough.limits import MAX_SEARCH_OUTPUT
from readthrough.process import quiet_output, run
from readthrough.registry import Registry

log = logging.getLogger(__name__)


class Harness(Protocol):
    """What an assistant may do: read the PR, append to the log, and report progress."""

    cfg: Config
    broadcast: Callable[[str, Any], None]
    pr_info: Callable[[int], dict[str, Any] | None]

    def append(self, number: int, kind: str, *, by: str, **fields: Any) -> dict[str, Any]: ...
    def components(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Turn:
    """One request from the learner. The engine has already recorded the learner's side."""

    number: int
    action: str  # chat | quiz | brief | answer | teachback | hint
    text: str
    qid: str | None
    prompt: str  # what to say to a model, if there is one


class Assistant(Protocol):
    name: str

    async def turn(self, turn: Turn) -> None: ...
    async def stop(self) -> None: ...


AssistantFactory = Callable[[Harness, Any], Assistant]
ASSISTANTS: Registry[AssistantFactory] = Registry("study assistant")
AUTO_ORDER = ("copilot", "rules")


@dataclass(frozen=True)
class Resolved:
    name: str
    available: bool
    reason: str  # why it isn't available, or ""


def available(name: str) -> tuple[bool, str]:
    if name == "copilot":
        try:
            import copilot  # noqa: F401
        except ImportError:
            return False, "The Copilot assistant needs the Copilot SDK: install readthrough with the [study] extra."
    return True, ""


def resolve(name: str = AUTO) -> Resolved:
    """Which assistant ``name`` means right now. ``auto`` is the first available one, Copilot first."""
    if name != AUTO:
        ASSISTANTS.get(name)
        return Resolved(name, *available(name))
    for candidate in AUTO_ORDER:
        ok, _ = available(candidate)
        if ok:
            return Resolved(candidate, True, "")
    return Resolved(AUTO_ORDER[-1], False, "no study assistant is available")


def make(harness: Harness, name: str, options: dict[str, Any] | None = None) -> Assistant:
    """Build the configured assistant. ``options`` maps assistant names to validated options objects."""
    resolved = resolve(name)
    factory = ASSISTANTS.get(resolved.name)
    opts = (options or {}).get(resolved.name) or ASSISTANTS.defaults_for(resolved.name)
    return factory.impl(harness, opts)


# ---------------------------------------------------------------- rules: no model, no dependencies


@dataclass(frozen=True)
class BankQuestion:
    question: str
    focus: str
    refs: list[str]
    group: str  # "always", a component id, "author", or "signal:<id>"
    reference: str = ""  # shown once, after the group's last question has been answered


RULES_BY = "rules"
CANT_DISCUSS = (
    "I'm the rules-based assistant: I can quiz you from the profile's questions, assemble a briefing, and "
    "point you at files, but I can't discuss the code. For a conversation that reads the diff, install "
    "readthrough with the [study] extra, which brings GitHub Copilot."
)
SIGNAL_KINDS = ("risk", "blocker", "responsibility", "correctness", "convention")


def question_bank(item: dict[str, Any], components: list[dict[str, Any]], profile: Any) -> list[BankQuestion]:
    """The questions the profile has for this PR, in the order the brief lists them: shared ones first,
    then each touched component's (most changed first), the author kind's, then one per signal."""
    by_id = {c["id"]: c for c in components}
    files_in = {}
    for f in item.get("files", []):
        files_in.setdefault(f["comp"], []).append(f["path"])
    bank: list[BankQuestion] = []
    seen: set[str] = set()

    def add(q: str, focus: str, refs: list[str], group: str, reference: str = "") -> None:
        if q not in seen:
            seen.add(q)
            bank.append(BankQuestion(q, focus, refs[:6], group, reference))

    tests = [f["path"] for f in item.get("files", []) if f["comp"] == profile.tests_component]
    for q in profile.questions:
        add(q, "intent", tests, "always")
    for comp in sorted(item.get("by_comp", {}), key=lambda c: -item["by_comp"][c]):
        component = profile.component_by_id.get(comp)
        if component is None:
            continue
        for q in component.questions:
            add(q, "responsibility", files_in.get(comp, []), comp, contract_text(by_id.get(comp, {})))
    kind = profile.authors.kinds.get(item.get("author_kind", ""))
    for q in kind.questions if kind else []:
        add(q, "risk", [], "author")
    for flag in item.get("flags", []):
        if flag.get("kind") in SIGNAL_KINDS:
            add(
                f"Signal: {flag['msg']}. Where in the diff did you confirm or rule this out?",
                "risk",
                [],
                f"signal:{flag['id']}",
            )
    smells: dict[str, list[dict[str, Any]]] = {}
    for s in item.get("smells", []):
        smells.setdefault(s["id"], []).append(s)
    for sid, hits in smells.items():
        if hits[0].get("kind") in SIGNAL_KINDS:
            add(
                f"Line heuristic: {hits[0]['msg']}. Is each flagged line a real instance, or a false positive?",
                "risk", [f"{s['path']}:{s['line']}" for s in hits], f"signal:{sid}",
            )  # fmt: skip
    return bank


def contract_text(component: dict[str, Any]) -> str:
    contract = component.get("contract") or {}
    parts = []
    if contract.get("responsibility"):
        parts.append(
            f"**{component.get('label', component.get('id', ''))}** is responsible for: {contract['responsibility']}"
        )
    if contract.get("does_not_own"):
        parts.append(contract["does_not_own"])
    return "\n\n".join(parts)


class RulesAssistant:
    name = RULES_BY

    def __init__(self, harness: Harness, options: Any) -> None:
        self.h = harness

    async def stop(self) -> None:
        return None

    async def turn(self, turn: Turn) -> None:
        handler = {
            "quiz": self._quiz, "answer": self._after_answer, "hint": self._hint,
            "brief": self._brief, "teachback": self._after_teachback,
        }.get(turn.action, self._chat)  # fmt: skip
        handler(turn)

    # ---- the bank for one PR

    def _bank(self, number: int) -> list[BankQuestion]:
        from readthrough.profile import ProfileError, resolve_profile

        item = self.h.pr_info(number) or {}
        try:
            profile = resolve_profile(self.h.cfg)
        except ProfileError:
            return []
        return question_bank(item, self.h.components(), profile)

    def _asked(self, number: int) -> dict[str, dict[str, Any]]:
        """Asked questions by text, so the bank's position survives a reload."""
        return {e["question"]: e for e in study.load(self.h.cfg, number) if e["kind"] == "question"}

    def _say(self, number: int, text: str, **fields: Any) -> None:
        self.h.append(number, "chat", by=RULES_BY, role="assistant", text=text, **fields)

    # ---- actions

    def _quiz(self, turn: Turn) -> None:
        asked = self._asked(turn.number)
        nxt = next((q for q in self._bank(turn.number) if q.question not in asked), None)
        if nxt is None:
            self._say(
                turn.number, "That's every question the profile has for this PR. Teach it back when you're ready."
            )
            return
        self.h.append(
            turn.number, "question", by=RULES_BY, question=nxt.question, focus=nxt.focus, refs=nxt.refs,
            group=nxt.group, expected_points=[],
        )  # fmt: skip

    def _after_answer(self, turn: Turn) -> None:
        """Once every question about a component has been answered, show its contract once, so the reference
        can't leak into the answers that follow."""
        bank = self._bank(turn.number)
        entries = study.load(self.h.cfg, turn.number)
        asked = {e["question"]: e for e in entries if e["kind"] == "question"}
        answered = {e["qid"] for e in entries if e["kind"] == "answer"}
        shown = {e.get("group") for e in entries if e["kind"] == "chat" and e.get("action") == "reference"}
        entry = next((e for e in asked.values() if e.get("qid") == turn.qid), None)
        if entry is None:
            return
        group = entry.get("group") or next((q.group for q in bank if q.question == entry["question"]), "")
        reference = next((q.reference for q in bank if q.group == group and q.reference), "")
        unanswered = [q for q in bank if q.group == group and asked.get(q.question, {}).get("qid") not in answered]
        if unanswered or not reference or group in shown:
            return  # nothing to add; the page offers a "what I missed" note on its own
        self._say(
            turn.number,
            "That was the last question about this component. Check your answers against its contract, and "
            f"note anything you missed:\n\n{reference}",
            action="reference", group=group,
        )  # fmt: skip

    def _hint(self, turn: Turn) -> None:
        entry = next((e for e in self._asked(turn.number).values() if e.get("qid") == turn.qid), None)
        if entry is None:
            self._say(turn.number, "There's no open question to hint at. Ask for the next one.")
            return
        refs = entry.get("refs") or []
        where = ", ".join(f"`{r}`" for r in refs) if refs else "the author's description and the tests"
        self._say(
            turn.number, f"Look at {where}. I can't say more without reading the code.", action="hint", qid=turn.qid
        )

    def _brief(self, turn: Turn) -> None:
        item = self.h.pr_info(turn.number) or {}
        by_id = {c["id"]: c for c in self.h.components()}
        out = ["**What the author says it changes**", (item.get("body") or "_No description._").strip()[:1500], ""]
        out.append("**Where it lands**")
        for comp, churn in sorted(item.get("by_comp", {}).items(), key=lambda kv: -kv[1]):
            c = by_id.get(comp, {})
            line = f"- {c.get('label', comp)} ({c.get('layer', '')}), {churn} lines"
            responsibility = (c.get("contract") or {}).get("responsibility")
            out.append(line + (f": {responsibility}" if responsibility else ""))
        watch = [f"- [{f['kind']}] {f['msg']}" for f in item.get("flags", []) if f.get("kind") != "info"]
        smells: dict[str, list[dict[str, Any]]] = {}
        for s in item.get("smells", []):
            smells.setdefault(s["id"], []).append(s)
        for group in smells.values():
            spots = ", ".join(f"`{s['path']}:{s['line']}`" for s in group[:3])
            watch.append(
                f"- [{group[0]['kind']}] {group[0]['msg']}: {spots}"
                + (f" and {len(group) - 3} more" if len(group) > 3 else "")
            )
        out += [
            "",
            "**Watch**",
            *(watch or ["- No automated signals. That means the cheap checks found nothing, not that it's safe."]),
        ]
        files = sorted(item.get("files", []), key=lambda f: -(f["add"] + f["del"]))[:8]
        out += ["", "**Most changed**", *(f"- `{f['path']}` +{f['add']}/-{f['del']}" for f in files)]
        out += [
            "",
            "_Assembled by rules from the profile and the PR's metadata; nothing here comes from reading the code._",
        ]
        self._say(turn.number, "\n".join(out))

    def _after_teachback(self, turn: Turn) -> None:
        self._say(
            turn.number,
            "Recorded. I can't assess it, so compare it yourself with the brief's architecture and signals "
            "sections, and note what you missed.",
        )

    def _chat(self, turn: Turn) -> None:
        self._say(turn.number, CANT_DISCUSS)


ASSISTANTS.add("rules", "the profile's own questions, hints, and briefing; no model, nothing to install")(
    RulesAssistant
)


# ---------------------------------------------------------------- copilot: the SDK

COPILOT_BY = "copilot"

PROVIDER_TYPES = ("openai", "azure", "anthropic")


@dataclass(frozen=True)
class CopilotOptions:
    """Where the Copilot assistant's model comes from.

    By default, GitHub Copilot through the Copilot CLI's own sign-in. With ``provider`` set, the same
    runtime talks to a model provider of yours instead (bring your own key), and no GitHub sign-in is
    involved at all: the Study tab then works for repos on any forge without a GitHub account, and the
    bill goes to that provider. The key is read from ``api_key_env``, never from the config file.
    """

    provider: str | None = None  # openai | azure | anthropic; unset means GitHub Copilot
    base_url: str | None = None  # the provider's endpoint, e.g. https://api.anthropic.com or an Azure resource
    model: str | None = None  # the provider's model name; `study_model` per repo still wins when set
    api_key_env: str = "READTHROUGH_MODEL_API_KEY"  # the variable holding the provider's key
    # azure only: sign in with `az` (an Entra token for Cognitive Services) instead of a key. Many Azure
    # OpenAI resources have key auth disabled by policy, so this is often the only way in.
    entra: bool = False
    wire_api: str | None = None  # openai only: completions | responses (the SDK's default when unset)
    azure_api_version: str | None = None  # azure only

    def __post_init__(self) -> None:
        if self.provider is not None and self.provider not in PROVIDER_TYPES:
            raise ValueError(f"provider must be one of {', '.join(PROVIDER_TYPES)}, not {self.provider!r}")
        if self.provider is not None and not self.base_url:
            raise ValueError("a provider needs base_url")
        if self.provider is not None and not self.model:
            raise ValueError("a provider needs model: the model name it serves")
        if self.entra and self.provider != "azure":
            raise ValueError("entra sign-in applies to the azure provider only")

    @property
    def byok(self) -> bool:
        return self.provider is not None

    def provider_config(self) -> dict[str, Any] | None:
        """The SDK's ``ProviderConfig`` for a provider of your own: the key from the environment, or for Azure
        with ``entra``, a callback that fetches a fresh Entra token from ``az`` whenever the runtime asks."""
        if self.provider is None:
            return None
        config: dict[str, Any] = {"type": self.provider, "base_url": self.base_url}
        if self.entra:
            config["bearer_token_provider"] = lambda _args: entra_token()
        else:
            key = os.environ.get(self.api_key_env, "").strip()
            if not key:
                raise RuntimeError(
                    f"The Copilot assistant is set to use {self.provider} at {self.base_url}, but {self.api_key_env} "
                    "isn't set. Put the provider's API key in that variable (never in config.toml)."
                )
            config["api_key"] = key
        if self.wire_api:
            config["wire_api"] = self.wire_api
        if self.provider == "azure" and self.azure_api_version:
            config["azure"] = {"api_version": self.azure_api_version}
        return config

    def billed_to(self) -> str:
        """Who pays for a turn: the provider, or GitHub Copilot through whoever is signed in."""
        return f"{self.provider}:{urlsplit(self.base_url or '').hostname or self.base_url}" if self.byok else "copilot"


COGNITIVE_SERVICES = "https://cognitiveservices.azure.com"


def entra_token() -> str:
    """A bearer token for Azure OpenAI from a signed-in ``az``. Asked for on demand; az caches and refreshes."""
    out, why = quiet_output(
        ["az", "account", "get-access-token", "--resource", COGNITIVE_SERVICES, "--query", "accessToken", "-o", "tsv"],
        with_failure=True,
    )
    if not out or not out.strip():
        raise RuntimeError(
            f"az has no token for Azure OpenAI ({why.splitlines()[0][:160] if why else 'not signed in'}); run az login"
        )
    return out.strip()


def copilot_options(cfg: Config) -> CopilotOptions:
    """The ``[assistants.copilot]`` options in effect, for anything that opens a Copilot SDK session."""
    opts = (cfg.assistant_options or {}).get("copilot")
    return opts if isinstance(opts, CopilotOptions) else CopilotOptions()


def copilot_client(options: CopilotOptions) -> Any:
    """A Copilot SDK client that, with a provider of our own, never falls back to the CLI's GitHub sign-in."""
    from copilot import CopilotClient

    return CopilotClient(use_logged_in_user=False) if options.byok else CopilotClient()


def record_sdk_usage(
    cfg: Config, data: Any, *, purpose: str, ref: str, by: str, options: CopilotOptions
) -> dict[str, Any] | None:
    """Append what a Copilot SDK ``assistant.usage`` event reports to the repo's usage ledger.

    Who is billed comes from the options, not the event: a provider of ours by host, else GitHub Copilot
    through whoever the dashboard's build saw signed in (the forge's viewer), else just ``copilot``.
    """
    fields = usage.from_sdk_usage(data)
    if not fields["call_id"]:
        return None  # an event with no call id can't be recorded idempotently; the SDK always sends one
    billed_to = options.billed_to()
    if not options.byok:
        viewer = _viewer(cfg)
        billed_to = f"copilot:{viewer}" if viewer else "copilot"
    return usage.record(
        cfg, call_id=fields["call_id"], purpose=purpose, ref=ref, model=fields["model"], billed_to=billed_to,
        by=by, tokens=fields["tokens"], multiplier=fields["multiplier"], nano_aiu=fields["nano_aiu"],
    )  # fmt: skip


def _viewer(cfg: Config) -> str:
    """The login the last build saw, from data.json; "" when there is no build or the forge reports none."""
    try:
        return str(json.loads(cfg.data_path.read_text(encoding="utf-8")).get("viewer") or "")
    except (OSError, ValueError):
        return ""


def model_options(cfg: Config, options: CopilotOptions) -> dict[str, Any]:
    """Session options that pick the model: the provider when configured, and the model name, where the repo's
    ``study_model`` beats the provider's default."""
    out: dict[str, Any] = {}
    provider = options.provider_config()
    if provider is not None:
        out["provider"] = provider
    model = cfg.study_model or options.model
    if model:
        out["model"] = model
    return out


SYSTEM_MESSAGE = """\
You are the study assistant inside readthrough. You help a maintainer of {repo} genuinely understand one
pull request before they review it. Your job is to make them think, not to think for them.

How you work:
- Socratic first. Ask, don't tell. Never reveal an answer or the expected points of a question
  before the learner has answered it.
- Quiz questions go through the ask_quiz_question tool, one at a time. That tool ends your turn,
  so put any lead-in in a sentence before calling it. Target what a reviewer must understand:
  intent, control flow, which component owns what (the architecture contracts in the brief),
  unhappy paths, whether the tests would fail without the change, and real risks. Mix easy and
  hard, and start with intent.
- After the learner answers, call assess_answer exactly once. correct, partial, or incorrect,
  with brief feedback and evidence as path:line references. For partial or incorrect answers,
  give a hint that points where to look instead of the answer, and offer to try again.
- Ground every claim in the code with get_diff, read_file, or search_code, and cite path:line.
  If you haven't looked, look before you answer.
- If the learner asks for a briefing, give it plainly: what the PR changes and why in three or four
  sentences, then the three to five things most worth a reviewer's attention, each with path:line and
  why it matters. Don't quiz in that turn. Asking is their choice, and the log records it.
- Use append_to_brief for short, durable insights worth keeping with the brief, and
  record_finding only for concrete problems you can point to in the code.
- Keep replies short: a few sentences, plain language, no headings. Don't narrate your tool use
  ("let me look at...", "search works"); the learner only wants the substance.
- You cannot edit files or run commands. The tools listed are everything you have.
"""


def system_message(repo: str, guidance: str) -> str:
    text = SYSTEM_MESSAGE.format(repo=repo)
    return text + (f"\nAbout this repository:\n{guidance.strip()}\n" if guidance.strip() else "")


def study_session_id(cfg: Config, number: int) -> str:
    """Derived from the PR's URL like the review session's id, but never the same one."""
    from readthrough import forges

    return str(uuid.uuid5(uuid.NAMESPACE_URL, forges.make(cfg).pr_url(number) + "#study"))


class CopilotAssistant:
    """One persistent Copilot session per PR (a fixed id, so it resumes across server restarts)."""

    name = COPILOT_BY

    def __init__(self, harness: Harness, options: Any) -> None:
        self.h = harness
        self.cfg = harness.cfg
        self.options: CopilotOptions = options if isinstance(options, CopilotOptions) else CopilotOptions()
        self._client: Any = None
        self._client_lock: asyncio.Lock | None = None
        self._sessions: dict[int, Any] = {}
        self._pr_by_session: dict[str, int] = {}
        self._worktrees: dict[int, tuple[Path, str]] = {}
        self._tools: list[Any] | None = None
        self._model_in_use: dict[int, str] = {}  # per PR, the model the runtime last reported using

    async def stop(self) -> None:
        for session in self._sessions.values():
            await session.disconnect()
        if self._client is not None:
            await self._client.stop()

    async def turn(self, turn: Turn) -> None:
        session, created = await self._session_for(turn.number)
        prompt = turn.prompt
        if created:
            prompt = self._first_turn_context(turn.number) + "\n\n" + prompt
        text = await self._send(turn.number, session, prompt)
        if text.strip():
            self._write(turn.number, "chat", role="assistant", text=text.strip())

    def _write(self, number: int, kind: str, **fields: Any) -> dict[str, Any]:
        """Append in the assistant's name, recording which model wrote it. ``by`` says who ran the conversation
        (this assistant); ``model`` says whose judgement it is, which is what a verdict's weight depends on."""
        model = self._model_in_use.get(number)
        return self.h.append(number, kind, by=COPILOT_BY, **({"model": model} if model else {}), **fields)

    async def _send(self, number: int, session: Any, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        parts: list[str] = []
        error: list[str] = []

        def on_event(event: Any) -> None:
            kind = getattr(event.type, "value", str(event.type))
            data = event.data
            if kind == "assistant.usage":
                if model := getattr(data, "model", None):
                    self._model_in_use[number] = str(model)
                self._record_usage(number, data)
            elif kind == "assistant.message_delta":
                delta = getattr(data, "delta_content", None) or ""
                parts.append(delta)
                self.h.broadcast("study", {"pr": number, "type": "delta", "text": delta})
            elif kind == "assistant.message" and parts and not parts[-1].endswith("\n"):
                parts.append("\n\n")  # separate the text before and after a tool call
            elif kind == "tool.execution_start":
                name = getattr(data, "tool_name", "?")
                if name == "ask_quiz_question" and "".join(parts).strip():
                    # Keep the lead-in above the question card it introduces.
                    self._write(number, "chat", role="assistant", text="".join(parts).strip())
                    parts.clear()
                self.h.broadcast("study", {"pr": number, "type": "tool", "name": name})
            elif kind == "session.error":
                error.append(getattr(data, "message", None) or "Copilot reported an error")
                loop.call_soon_threadsafe(done.set)
            elif kind == "session.idle":
                loop.call_soon_threadsafe(done.set)

        unsubscribe = session.on(on_event)
        try:
            await session.send(prompt)
            await asyncio.wait_for(done.wait(), timeout=self.cfg.tuning.study.turn_timeout_seconds)
        finally:
            unsubscribe()
        if error:
            raise RuntimeError(error[0])
        return "".join(parts)

    async def _session_for(self, number: int) -> tuple[Any, bool]:
        if number in self._sessions:
            return self._sessions[number], False
        from copilot.generated.rpc import PermissionDecisionUserNotAvailable

        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            if self._client is None:
                client = copilot_client(self.options)
                await client.start()
                self._client = client
        worktree, _ = await self._worktree(number)
        session_id = study_session_id(self.cfg, number)
        self._pr_by_session[session_id] = number
        tools = self._build_tools()
        options: dict[str, Any] = {
            "tools": tools,
            "available_tools": [t.name for t in tools],
            "on_permission_request": lambda _req, _ctx: PermissionDecisionUserNotAvailable(),
            "client_name": "readthrough",
            "working_directory": str(worktree),
            "enable_skills": False,
            "streaming": True,
            "system_message": {"mode": "append", "content": system_message(self.cfg.repo, self._study_guidance())},
        }
        options.update(model_options(self.cfg, self.options))
        exists = Copilot().session_info(session_id).exists  # the SDK shares the Copilot CLI's session store
        session, session_id, created = await open_study_session(self._client, session_id, options, resume=exists)
        self._pr_by_session[session_id] = number
        self._sessions[number] = session
        return session, created

    def _record_usage(self, number: int, data: Any) -> None:
        """One ledger line per model call, from the SDK's usage event. A failure to record never fails the turn."""
        try:
            entry = record_sdk_usage(self.cfg, data, purpose="study", ref=f"#{number}", by=COPILOT_BY,
                                     options=self.options)  # fmt: skip
        except Exception:
            log.exception("Couldn't record model usage for #%d", number)
            return
        if entry is not None:
            self.h.broadcast("study", {"pr": number, "type": "usage", "entry": entry})

    async def _worktree(self, number: int) -> tuple[Path, str]:
        if number not in self._worktrees:
            info = self.h.pr_info(number) or {}
            path, merge_base, _ = await asyncio.to_thread(
                review.ensure_worktree, self.cfg, number, base=info.get("base", "main")
            )
            self._worktrees[number] = (path, merge_base)
        return self._worktrees[number]

    def _study_guidance(self) -> str:
        from readthrough.profile import ProfileError, resolve_profile

        try:
            return resolve_profile(self.cfg).study_guidance
        except ProfileError:
            return ""

    def _first_turn_context(self, number: int) -> str:
        info = self.h.pr_info(number) or {}
        brief = self.cfg.briefs_dir / f"pr-{number}.md"
        brief_text = brief.read_text(encoding="utf-8") if brief.exists() else "(no brief yet)"
        return (
            f"We're studying {self.cfg.repo}#{number}: {info.get('title', '')}. The PR is checked out in your "
            "working directory. Here is its study brief (architecture contracts, reading order, signals, "
            f"and any earlier study log):\n\n<brief>\n{brief_text[:40_000]}\n</brief>"
        )

    # ---- tools

    def _number_for(self, invocation: Any) -> int:
        number = self._pr_by_session.get(getattr(invocation, "session_id", ""))
        if number is None:
            raise RuntimeError("This tool only works inside a readthrough study session.")
        return number

    def _build_tools(self) -> list[Any]:
        if self._tools is not None:
            return self._tools
        from copilot import define_tool
        from pydantic import BaseModel, Field

        assistant = self

        class DiffParams(BaseModel):
            path: str | None = Field(
                None, description="Limit the diff to this repo-relative file. Omit for the whole PR."
            )

        class ReadParams(BaseModel):
            path: str = Field(description="Repo-relative file path, e.g. src/app/module.py")
            start_line: int = Field(1, ge=1)
            end_line: int = Field(200, ge=1)

        class SearchParams(BaseModel):
            pattern: str = Field(description="Extended regex, passed to git grep -E")
            path_glob: str | None = Field(None, description="Optional pathspec, e.g. 'src/app/**'")

        class QuestionParams(BaseModel):
            question: str = Field(description="One question for the learner. Don't hint at the answer.")
            focus: str = Field(
                description="What it tests: intent, control-flow, responsibility, unhappy-path, tests, or risk"
            )
            refs: list[str] = Field(default_factory=list, description="path:line locations the answer depends on")
            expected_points: list[str] = Field(
                description="What a good answer covers. Hidden from the learner until they answer."
            )

        class AssessParams(BaseModel):
            qid: str = Field(description="The question id, e.g. Q2")
            verdict: str = Field(description="correct, partial, or incorrect")
            feedback: str = Field(
                description="One to three sentences. For partial or incorrect, a hint, not the answer."
            )
            evidence: list[str] = Field(default_factory=list, description="path:line references supporting the verdict")

        class TeachbackParams(BaseModel):
            verdict: str = Field(description="correct, partial, or incorrect")
            feedback: str = Field(description="What they got right and the specific gaps")
            gaps: list[str] = Field(default_factory=list)

        class NoteParams(BaseModel):
            text: str = Field(description="A short, durable insight in markdown")
            section: str | None = Field(None, description="Which brief section it relates to, if any")

        class FindingParams(BaseModel):
            severity: str = Field(description="blocker, major, minor, or question")
            summary: str
            path: str | None = None
            line: int | None = None

        @define_tool(
            name="get_diff",
            description="The PR's diff against its merge base, optionally for one file.",
            skip_permission=True,
        )
        async def get_diff(params: DiffParams, invocation: Any) -> str:
            number = assistant._number_for(invocation)
            worktree, merge_base = await assistant._worktree(number)
            cmd = ["git", "-C", str(worktree), "diff", f"{merge_base}..HEAD"]
            if params.path:
                cmd += ["--", assistant._safe_rel(worktree, params.path)]
            return _clip(
                await asyncio.to_thread(run, cmd, check=False), limit=assistant.cfg.tuning.study.max_tool_output_chars
            )

        @define_tool(
            name="read_file",
            description="Read lines of a file at the PR's head commit. Lines are numbered.",
            skip_permission=True,
        )
        async def read_file(params: ReadParams, invocation: Any) -> str:
            worktree, _ = await assistant._worktree(assistant._number_for(invocation))
            path = worktree / assistant._safe_rel(worktree, params.path)
            if not path.is_file():
                return f"No such file: {params.path}"
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            end = min(params.end_line, params.start_line + 399, len(lines))
            body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(params.start_line, end + 1))
            return f"{params.path} lines {params.start_line}-{end} of {len(lines)}\n{body}"

        @define_tool(
            name="search_code",
            description="git grep the PR's head commit. Returns path:line:text matches.",
            skip_permission=True,
        )
        async def search_code(params: SearchParams, invocation: Any) -> str:
            worktree, _ = await assistant._worktree(assistant._number_for(invocation))
            cmd = ["git", "-C", str(worktree), "grep", "-n", "-I", "-E", "--max-count=50", "-e", params.pattern]
            if params.path_glob:
                cmd += ["--", params.path_glob]
            out = await asyncio.to_thread(run, cmd, check=False)
            return (
                _clip(out, limit=min(MAX_SEARCH_OUTPUT, assistant.cfg.tuning.study.max_tool_output_chars))
                or "No matches."
            )

        @define_tool(
            name="ask_quiz_question",
            description="Ask the learner one quiz question. Ends your turn.",
            skip_permission=True,
            is_terminal=True,
        )
        def ask_quiz_question(params: QuestionParams, invocation: Any) -> str:
            entry = assistant._write(assistant._number_for(invocation), "question", **params.model_dump())
            return f"Asked {entry['qid']}. Wait for the learner's answer."

        @define_tool(
            name="assess_answer",
            description="Record your assessment of the learner's answer to a question.",
            skip_permission=True,
        )
        def assess_answer(params: AssessParams, invocation: Any) -> str:
            assistant._write(assistant._number_for(invocation), "assessment", **params.model_dump())
            return "Recorded."

        @define_tool(
            name="assess_teachback",
            description="Record your assessment of the learner's teach-back.",
            skip_permission=True,
        )
        def assess_teachback(params: TeachbackParams, invocation: Any) -> str:
            assistant._write(assistant._number_for(invocation), "teachback_assessment", **params.model_dump())
            return "Recorded."

        @define_tool(
            name="append_to_brief", description="Append a short insight to the PR's study brief.", skip_permission=True
        )
        def append_to_brief(params: NoteParams, invocation: Any) -> str:
            assistant._write(assistant._number_for(invocation), "note", **params.model_dump(exclude_none=True))
            return "Appended."

        @define_tool(
            name="record_finding",
            description="Record a concrete problem in the PR, with its location.",
            skip_permission=True,
        )
        def record_finding(params: FindingParams, invocation: Any) -> str:
            assistant._write(assistant._number_for(invocation), "finding", **params.model_dump(exclude_none=True))
            return "Recorded."

        self._tools = [
            get_diff,
            read_file,
            search_code,
            ask_quiz_question,
            assess_answer,
            assess_teachback,
            append_to_brief,
            record_finding,
        ]
        return self._tools

    @staticmethod
    def _safe_rel(worktree: Path, rel: str) -> str:
        """Resolve a model-supplied path and refuse anything outside the worktree."""
        target = (worktree / rel).resolve()
        if not target.is_relative_to(worktree.resolve()):
            raise ValueError(f"{rel} is outside the PR checkout")
        return target.relative_to(worktree.resolve()).as_posix()


ASSISTANTS.add(
    "copilot",
    "a Copilot SDK session per PR that reads the code and grades answers: GitHub Copilot, or your own model provider (needs [study])",
    options=CopilotOptions,
)(CopilotAssistant)


async def open_study_session(
    client: Any, session_id: str, options: dict[str, Any], *, resume: bool
) -> tuple[Any, str, bool]:
    """Resume the PR's study session, or start one. Returns (session, its id, whether it's new).

    A session whose history Copilot can't read back (say its folder was half-deleted) would otherwise break
    the Study tab for that PR for good. Fall back to a fresh session: the study log, not the chat history,
    is the record, and a new session is handed the brief with the log at its end.
    """
    if resume:
        try:
            return await client.resume_session(session_id, **options), session_id, False
        except Exception as exc:  # noqa: BLE001 - SDK errors have no common type
            log.warning("Couldn't resume study session %s (%s); starting a fresh one", session_id, str(exc)[:200])
            session_id = str(uuid.uuid4())
    return await client.create_session(session_id=session_id, **options), session_id, True


def _clip(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} characters; narrow the request]"

"""The study assistants: the rules one end to end, the model one on a fake runtime, and how they're chosen."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import NOW, PROFILE, PROFILE_PATH, REPO

from readthrough import assistants, runtimes, study, usage
from readthrough.analyze import analyze
from readthrough.assistants import (
    ASSISTANTS,
    ModelAssistant,
    RulesAssistant,
    Turn,
    _clip,
    question_bank,
    system_message,
)
from readthrough.config import Config
from readthrough.registry import RegistryError
from readthrough.rules import Contract

DIFF = """\
diff --git a/courier/transports/smtp.py b/courier/transports/smtp.py
--- a/courier/transports/smtp.py
+++ b/courier/transports/smtp.py
@@ -10,1 +10,2 @@ class X:
     def keep(self):
+        Store.open().save(m)
"""


class Harness:
    """What an assistant sees: a Config, the PR items, the components, and somewhere to append."""

    def __init__(self, cfg: Config, items: list[dict], components: list[dict]):
        self.cfg = cfg
        self.items = {it["number"]: it for it in items}
        self._components = components
        self.events: list[tuple[str, object]] = []

    def append(self, number: int, kind: str, *, by: str, **fields):
        entry = study.append(self.cfg, number, kind, by=by, **fields)
        self.events.append(("entry", entry))
        return entry

    def broadcast(self, event: str, data) -> None:
        self.events.append((event, data))

    def pr_info(self, number: int):
        return self.items.get(number)

    def components(self):
        return self._components


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(home=tmp_path / "home", repo=REPO, clone=tmp_path / "clone", assistant="rules")
    cfg.briefs_dir.mkdir(parents=True)
    cfg.study_dir.mkdir(parents=True)
    cfg.local_profile_path.write_text(PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    (cfg.briefs_dir / "pr-7.md").write_text("# #7 Title\n\n" + ("brief " * 10), encoding="utf-8")
    # The timeout is generous: the fakes signal `done` themselves, so a turn ends at once. Only the test that
    # checks the timeout itself shortens it; a tiny default would race the event loop on a slow machine.
    cfg.tuning = replace(cfg.tuning, study=replace(cfg.tuning.study, max_tool_output_chars=32, turn_timeout_seconds=5))
    return cfg


def _components():
    contract = Contract(
        responsibility="Deliver an envelope over one protocol.",
        does_not_own="- **Does not own**: persistence (the pipeline saves to the store).",
    )
    return [
        {"id": c.id, "label": c.label, "layer": c.layer, "contract": vars(contract) if c.id == "transports" else {}}
        for c in PROFILE.components
    ]


@pytest.fixture
def harness(cfg, make_pull) -> Harness:
    item = analyze(make_pull(number=7), profile=PROFILE, viewer="me", instructions=[], diff_text=DIFF, now=NOW)
    return Harness(cfg, [item], _components())


# ---------------------------------------------------------------- choosing


def test_resolve_prefers_the_model_assistant_when_a_runtime_can_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runtimes, "resolve", lambda name, options=None: ("copilot", False, "no sdk"))
    assert assistants.resolve("auto") == assistants.Resolved("rules", True, "")
    resolved = assistants.resolve("model")
    assert resolved.name == "model" and not resolved.available and "no model to run on: no sdk" in resolved.reason
    assert resolved.runtime == "copilot", "which runtime it would have run on, so the page can say what's missing"
    monkeypatch.setattr(runtimes, "resolve", lambda name, options=None: ("anthropic", True, ""))
    assert assistants.resolve("auto") == assistants.Resolved("model", True, "", runtime="anthropic")
    assert assistants.resolve("rules").runtime is None
    with pytest.raises(RegistryError, match="did you mean 'rules'"):
        assistants.resolve("ruels")
    # The old name of the model assistant resembles nothing, so the error lists what there is.
    with pytest.raises(RegistryError, match=r"unknown study assistant 'copilot' \(one of: model, rules\)"):
        assistants.resolve("copilot")


def test_available_names_what_is_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        runtimes,
        "available",
        lambda name: (
            (False, "the copilot runtime needs the Copilot SDK: install readthrough with the [study] extra")
            if name == "copilot"
            else (False, f"the {name} runtime needs an API key")
        ),
    )
    ok, reason = assistants.available("model")
    assert not ok and "install the [study] extra, or set an API key" in reason
    assert assistants.available("rules") == (True, "")


def test_make_builds_the_configured_assistant(harness, monkeypatch: pytest.MonkeyPatch):
    assert isinstance(assistants.make(harness, "rules"), RulesAssistant)
    monkeypatch.setattr(runtimes, "resolve", lambda name, options=None: ("fake", True, ""))
    monkeypatch.setattr(runtimes, "make", lambda name, options=None: FakeRuntime())
    assert isinstance(assistants.make(harness, "auto"), ModelAssistant)
    assert [p.name for p in ASSISTANTS] == ["rules", "model"]


# ---------------------------------------------------------------- rules


def test_question_bank_follows_the_brief_and_dedupes(harness):
    item = harness.pr_info(7)
    bank = question_bank(item, harness.components(), PROFILE)
    groups = [q.group for q in bank]
    # Shared questions first, then the touched components (most changed first), then signals.
    assert groups[: len(PROFILE.questions)] == ["always"] * len(PROFILE.questions)
    assert groups.index("transports") < groups.index("tests") if "tests" in groups else True
    assert any(g.startswith("signal:transport-store") for g in groups)
    transports = [q for q in bank if q.group == "transports"]
    assert transports and all(q.refs == ["courier/transports/smtp.py"] for q in transports)
    assert "Deliver an envelope" in transports[0].reference
    assert len({q.question for q in bank}) == len(bank)


def test_rules_quiz_asks_in_order_shows_reference_after_a_groups_last_question(harness):
    rules = RulesAssistant(harness, None)
    bank = question_bank(harness.pr_info(7), harness.components(), PROFILE)
    always, transports = [q for q in bank if q.group == "always"], [q for q in bank if q.group == "transports"]

    async def ask():
        await rules.turn(Turn(7, "quiz", "", None, ""))

    for _ in range(len(always) + len(transports)):
        asyncio.run(ask())
    questions = [e for e in study.load(harness.cfg, 7) if e["kind"] == "question"]
    assert [q["question"] for q in questions] == [q.question for q in always + transports]
    assert all(q["by"] == "rules" and q["expected_points"] == [] for q in questions)

    # Answering the first transports question adds nothing; answering the last shows the contract once.
    first, last = questions[len(always)], questions[-1]
    for q in (first, last):
        harness.append(7, "answer", by="you", qid=q["qid"], text="an answer")
        asyncio.run(rules.turn(Turn(7, "answer", "an answer", q["qid"], "")))
    chats = [e for e in study.load(harness.cfg, 7) if e["kind"] == "chat"]
    assert len(chats) == 1 and chats[0]["action"] == "reference" and chats[0]["group"] == "transports"
    assert "Deliver an envelope" in chats[0]["text"] and "Does not own" in chats[0]["text"]
    # No verdicts, ever.
    assert not any(e["kind"] in ("assessment", "teachback_assessment") for e in study.load(harness.cfg, 7))


def test_rules_runs_out_of_questions_gracefully(harness):
    rules = RulesAssistant(harness, None)
    bank = question_bank(harness.pr_info(7), harness.components(), PROFILE)
    for _ in range(len(bank) + 1):
        asyncio.run(rules.turn(Turn(7, "quiz", "", None, "")))
    entries = study.load(harness.cfg, 7)
    assert sum(e["kind"] == "question" for e in entries) == len(bank)
    assert "every question" in entries[-1]["text"]


def test_rules_hint_brief_teachback_and_chat(harness):
    rules = RulesAssistant(harness, None)
    asyncio.run(rules.turn(Turn(7, "hint", "", "Q9", "")))
    assert "no open question" in study.load(harness.cfg, 7)[-1]["text"]

    asyncio.run(rules.turn(Turn(7, "quiz", "", None, "")))
    qid = study.load(harness.cfg, 7)[-1]["qid"]
    asyncio.run(rules.turn(Turn(7, "hint", "", qid, "")))
    hint = study.load(harness.cfg, 7)[-1]
    assert hint["action"] == "hint" and hint["qid"] == qid and "Look at" in hint["text"]

    asyncio.run(rules.turn(Turn(7, "brief", "", None, "")))
    brief = study.load(harness.cfg, 7)[-1]["text"]
    assert "Where it lands" in brief and "Transports" in brief and "Deliver an envelope" in brief
    assert "transport touching the store" in brief and "`courier/transports/smtp.py:11`" in brief
    assert brief.endswith("nothing here comes from reading the code._")

    asyncio.run(rules.turn(Turn(7, "teachback", "It adds retries", None, "")))
    assert "can't assess" in study.load(harness.cfg, 7)[-1]["text"]
    asyncio.run(rules.turn(Turn(7, "chat", "why?", None, "why?")))
    assert "[study]" in study.load(harness.cfg, 7)[-1]["text"]
    asyncio.run(rules.stop())


def test_rules_without_a_profile_has_no_questions(harness):
    harness.cfg.local_profile_path.unlink()
    rules = RulesAssistant(harness, None)
    asyncio.run(rules.turn(Turn(7, "quiz", "", None, "")))
    assert "every question" in study.load(harness.cfg, 7)[-1]["text"]


# ---------------------------------------------------------------- the model assistant, on a fake runtime


class FakeSession:
    """A runtime session scripted with the events one prompt produces. Tool calls run the real handlers."""

    def __init__(self, script: list[Any], *, fresh: bool = True):
        self.script = script  # Events, or ("call", tool_name, args) to run a tool and report it
        self.sent: list[str] = []
        self._fresh = fresh
        self.closed = False
        self.tools: dict[str, runtimes.ToolSpec] = {}

    @property
    def fresh(self) -> bool:
        return self._fresh

    async def send(self, prompt: str):
        self.sent.append(prompt)
        self._fresh = False
        text: list[str] = []
        for item in self.script:
            if isinstance(item, tuple):
                _, name, args = item
                yield runtimes.Event("tool", name=name)
                await self.tools[name].handler(args)
                continue
            if item.kind == "text":
                text.append(item.text)
            yield runtimes.Event("done", text="".join(text)) if item.kind == "done" and not item.text else item

    async def close(self) -> None:
        self.closed = True


class FakeRuntime:
    name = "fake"

    def __init__(self, options: Any = None):
        self.sessions: dict[str, FakeSession] = {}
        self.opened: list[runtimes.Conversation] = []
        self.script: list[Any] = [runtimes.Event("done")]
        self.stopped = False

    def describe(self) -> str:
        return "a fake runtime"

    @property
    def model(self) -> str | None:
        return None

    async def open(self, conversation: runtimes.Conversation) -> FakeSession:
        self.opened.append(conversation)
        session = self.sessions.get(conversation.key)
        if session is None or not conversation.resume:
            session = self.sessions[conversation.key] = FakeSession(list(self.script))
        session.tools = {t.name: t for t in conversation.tools}
        return session

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    runtime = FakeRuntime()
    monkeypatch.setattr(runtimes, "make", lambda name, options=None: runtime)
    monkeypatch.setattr(runtimes, "resolve", lambda name, options=None: ("fake", True, ""))
    return runtime


def _usage(**kw: Any) -> runtimes.Usage:
    base: dict[str, Any] = {
        "call_id": "call-1",
        "model": "claude-sonnet-5-5",
        "input_tokens": 100,
        "output_tokens": 20,
        "billed_to": "anthropic:api.anthropic.com",
    }
    return runtimes.Usage(**{**base, **kw})


def test_system_message_adds_guidance_only_when_present():
    assert system_message("o/r", "").endswith("tools listed are everything you have.\n")
    assert "About this repository:\nuse the framework map\n" in system_message("o/r", "  use the framework map  ")


def test_a_fresh_conversation_gets_the_brief_then_the_prompt(harness, fake_runtime, monkeypatch):
    monkeypatch.setattr(assistants.review, "ensure_worktree", lambda cfg, number, base: (Path("wt"), "base", None))
    fake_runtime.script = [runtimes.Event("text", text="  A reply  "), runtimes.Event("done")]
    assistant = ModelAssistant(harness, None)
    asyncio.run(assistant.turn(Turn(7, "chat", "original prompt", None, "original prompt")))
    session = fake_runtime.sessions[next(iter(fake_runtime.sessions))]
    assert f"We're studying {REPO}#7" in session.sent[0] and session.sent[0].endswith("original prompt")
    assert "brief brief" in session.sent[0], "the brief rides along on the first turn"
    conversation = fake_runtime.opened[0]
    assert conversation.key.endswith("#study") and conversation.working_directory == "wt"
    assert [t.name for t in conversation.tools] == ["get_diff", "read_file", "search_code", "ask_quiz_question",
                                                    "assess_answer", "assess_teachback", "append_to_brief", "record_finding"]  # fmt: skip
    assert "The learner maintains Courier" in conversation.system
    last = study.load(harness.cfg, 7)[-1]
    assert (last["kind"], last["by"], last["runtime"], last["text"]) == ("chat", "model", "fake", "A reply")
    asyncio.run(assistant.turn(Turn(7, "chat", "again", None, "again")))
    assert session.sent[1] == "again", "the second turn is the prompt alone"


def test_streaming_text_tool_calls_and_the_quiz_lead_in(harness, fake_runtime, monkeypatch):
    monkeypatch.setattr(assistants.review, "ensure_worktree", lambda cfg, number, base: (Path("wt"), "base", None))
    fake_runtime.script = [
        runtimes.Event("text", text="Lead-in"),
        ("call", "ask_quiz_question", {"question": "Why?", "focus": "intent", "expected_points": ["x"]}),
        runtimes.Event("done"),
    ]
    assistant = ModelAssistant(harness, None)
    asyncio.run(assistant.turn(Turn(7, "quiz", "", None, "Ask me the next quiz question.")))
    kinds = [(e["kind"], e.get("text", e.get("question"))) for e in study.load(harness.cfg, 7)]
    assert kinds == [("chat", "Lead-in"), ("question", "Why?")], "the lead-in stays above the question card"
    assert ("study", {"pr": 7, "type": "tool", "name": "ask_quiz_question"}) in harness.events
    assert ("study", {"pr": 7, "type": "delta", "text": "Lead-in"}) in harness.events


def test_runtime_errors_and_timeouts_fail_the_turn(harness, fake_runtime, monkeypatch):
    monkeypatch.setattr(assistants.review, "ensure_worktree", lambda cfg, number, base: (Path("wt"), "base", None))
    fake_runtime.script = [runtimes.Event("error", text="boom")]
    assistant = ModelAssistant(harness, None)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(assistant.turn(Turn(7, "chat", "x", None, "x")))

    class Hanging(FakeSession):
        async def send(self, prompt: str):
            await asyncio.sleep(10)
            yield runtimes.Event("done")

    harness.cfg.tuning = replace(harness.cfg.tuning, study=replace(harness.cfg.tuning.study, turn_timeout_seconds=0.05))
    hanging = Hanging([])
    fake_runtime.sessions.clear()
    assistant._sessions[8] = hanging
    with pytest.raises(TimeoutError):
        asyncio.run(assistant.turn(Turn(8, "chat", "x", None, "x")))


def test_usage_events_become_ledger_lines(harness, fake_runtime, monkeypatch):
    """Each usage event is one ledger line in the provider's own units, idempotent by call id, billed to whoever
    the runtime says; GitHub Copilot's lines name the dashboard's viewer."""
    monkeypatch.setattr(assistants.review, "ensure_worktree", lambda cfg, number, base: (Path("wt"), "base", None))
    harness.cfg.data_path.parent.mkdir(parents=True, exist_ok=True)
    harness.cfg.data_path.write_text('{"viewer": "spencrr"}', encoding="utf-8")
    copilot_call = _usage(call_id="call-1", model="gpt-5", input_tokens=1200, output_tokens=300, cache_read_tokens=800,
                          multiplier=1.0, nano_aiu=1.5e9, billed_to="copilot")  # fmt: skip
    fake_runtime.script = [runtimes.Event("usage", usage=copilot_call), runtimes.Event("usage", usage=copilot_call),
                           runtimes.Event("text", text="ok"), runtimes.Event("done")]  # fmt: skip
    assistant = ModelAssistant(harness, None)
    asyncio.run(assistant.turn(Turn(7, "chat", "x", None, "x")))
    entries = usage.load(harness.cfg)
    assert len(entries) == 1, "idempotent by call id"
    line = entries[0]
    assert (line["id"], line["purpose"], line["ref"], line["by"]) == ("call-1", "study", "#7", "model")
    assert line["billed_to"] == "copilot:spencrr" and line["via"] == "local"
    assert line["tokens"] == {"input": 1200, "output": 300, "cache_read": 800}, "unreported counts are absent, not 0"
    assert line["multiplier"] == 1.0 and line["nano_aiu"] == 1.5e9 and "usd" not in line
    assert sum(1 for e, d in harness.events if isinstance(d, dict) and d.get("type") == "usage") == 1

    fake_runtime.script = [runtimes.Event("usage", usage=_usage(call_id="call-2", input_tokens=10, output_tokens=5)),
                           runtimes.Event("done")]  # fmt: skip
    fake_runtime.sessions.clear()
    asyncio.run(ModelAssistant(harness, None).turn(Turn(8, "chat", "x", None, "x")))
    line = usage.load(harness.cfg)[1]
    assert line["billed_to"] == "anthropic:api.anthropic.com" and "nano_aiu" not in line and "multiplier" not in line

    summary = usage.summarize(usage.load(harness.cfg))
    assert summary["calls"] == 2 and summary["tokens"] == {"input": 1210, "output": 305, "cache_read": 800}
    assert summary["nano_aiu"] == 1.5e9 and summary["usd"] is None and summary["unpriced_calls"] == 1
    assert summary["by_payer"] == {"copilot:spencrr": 1, "anthropic:api.anthropic.com": 1}
    assert usage.describe(summary) == "2 model calls, 1,515 tokens, 1.50 AIU, 1 calls with no price reported"
    assert usage.describe(usage.summarize([])) == "no model calls recorded"
    with pytest.raises(ValueError, match="price table"):
        usage.record(harness.cfg, call_id="x", purpose="study", ref="#1", model="m", billed_to="b", by="model", usd=1.0)


def test_verdicts_name_the_model_that_reached_them(harness, fake_runtime, monkeypatch):
    """The runtime reports the model with each call; what the assistant writes afterwards carries it and the
    runtime, and the brief says which model graded, through what, from the entries alone."""
    monkeypatch.setattr(assistants.review, "ensure_worktree", lambda cfg, number, base: (Path("wt"), "base", None))
    study.append(harness.cfg, 7, "question", by="model", question="Why?")
    study.append(harness.cfg, 7, "answer", by="you", qid="Q1", text="Because.")
    fake_runtime.script = [
        runtimes.Event("usage", usage=_usage(model="gpt-4.1")),
        ("call", "assess_answer", {"qid": "Q1", "verdict": "correct", "feedback": "Yes."}),
        runtimes.Event("text", text="Right."),
        runtimes.Event("done"),
    ]
    asyncio.run(ModelAssistant(harness, None).turn(Turn(7, "answer", "Because.", "Q1", "My answer")))
    entries = study.load(harness.cfg, 7)
    assert entries[-1]["model"] == "gpt-4.1" and entries[-1]["kind"] == "chat"
    assert (entries[-2]["model"], entries[-2]["by"], entries[-2]["runtime"], entries[-2]["kind"]) == (
        "gpt-4.1", "model", "fake", "assessment"
    )  # fmt: skip
    assert "assessed by gpt-4.1 via fake" in study.render_appendix(entries)
    assert study.graders([{"kind": "assessment", "by": "model", "runtime": "openai"}]) == (
        "a model not named in the log via openai"
    ), "a runtime that reported no model: say so rather than guess"
    assert study.graders([]) == "nobody"


def test_tool_handlers_read_search_truncate_and_append(harness, tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 11)), encoding="utf-8")
    monkeypatch.setattr(assistants.review, "ensure_worktree", lambda cfg, number, base: (worktree, "base", None))
    monkeypatch.setattr(runtimes, "make", lambda name, options=None: FakeRuntime())
    calls = []

    def fake_run(cmd, *, check=True, **kw):
        calls.append(cmd)
        return "x" * 100 if "diff" in cmd else "src/a.py:1:line 1\n"

    monkeypatch.setattr(assistants, "run", fake_run)
    assistant = ModelAssistant(harness, None)
    tools = {t.name: t for t in assistant._tools(7)}

    diff = asyncio.run(tools["get_diff"].handler({"path": "src/a.py"}))
    assert diff.startswith("x" * 32) and "truncated 68 characters" in diff
    assert calls[-1][-3:] == ["base..HEAD", "--", "src/a.py"]
    with pytest.raises(ValueError, match="outside the PR checkout"):
        asyncio.run(tools["get_diff"].handler({"path": "../escape.py"}))

    text = asyncio.run(tools["read_file"].handler({"path": "src/a.py", "start_line": 2, "end_line": 3}))
    assert "lines 2-3 of 10" in text and "    2  line 2" in text
    assert asyncio.run(tools["read_file"].handler({"path": "nope.py"})).startswith("No such file")

    found = asyncio.run(tools["search_code"].handler({"pattern": "line", "path_glob": "src/**"}))
    assert found.startswith("src/a.py:1:line 1") and calls[-1][-2:] == ["--", "src/**"]

    asked = asyncio.run(
        tools["ask_quiz_question"].handler({"question": "Why?", "focus": "intent", "expected_points": ["p"]})
    )
    assert asked == "Asked Q1. Wait for the learner's answer."
    assert asyncio.run(tools["append_to_brief"].handler({"text": "note"})) == "Appended."
    assert asyncio.run(tools["record_finding"].handler({"severity": "minor", "summary": "s"})) == "Recorded."
    assert [e["kind"] for e in study.load(harness.cfg, 7)] == ["question", "note", "finding"]
    assert all(e["by"] == "model" and e["runtime"] == "fake" for e in study.load(harness.cfg, 7))
    for spec in tools.values():
        schema = runtimes.tool_schema(spec)
        assert schema["type"] == "object" and "properties" in schema
    assert tools["ask_quiz_question"].terminal and not tools["assess_answer"].terminal


def test_the_worktree_follows_the_pr_head_and_the_current_clone(harness, monkeypatch):
    """The checkout is made once per head, not once per process: a push to the PR between turns must reach
    the tools, and a clone configured after a failed first turn must be the one used."""
    made = []

    def fake_ensure(cfg, number, base):
        made.append((cfg.clone, number))
        return Path(f"wt-{len(made)}"), "base", None

    monkeypatch.setattr(assistants.review, "ensure_worktree", fake_ensure)
    monkeypatch.setattr(runtimes, "make", lambda name, options=None: FakeRuntime())
    assistant = ModelAssistant(harness, None)
    harness.items[7]["head"] = "aaa"
    assert asyncio.run(assistant._worktree(7)) == (Path("wt-1"), "base")
    assert asyncio.run(assistant._worktree(7)) == (Path("wt-1"), "base"), "same head: no second checkout"
    harness.items[7]["head"] = "bbb"
    assert asyncio.run(assistant._worktree(7)) == (Path("wt-2"), "base"), "new head: checked out again"

    harness.cfg = replace(harness.cfg, clone=Path("new-clone"))
    harness.items[7]["head"] = "ccc"
    asyncio.run(assistant._worktree(7))
    assert made[-1] == (Path("new-clone"), 7), "the assistant reads the clone through the harness, not a copy"


def test_stop_stops_the_runtime(harness, fake_runtime, monkeypatch):
    monkeypatch.setattr(assistants.review, "ensure_worktree", lambda cfg, number, base: (Path("wt"), "base", None))
    assistant = ModelAssistant(harness, None)
    asyncio.run(assistant.turn(Turn(7, "chat", "x", None, "x")))
    asyncio.run(assistant.stop())
    assert fake_runtime.stopped and assistant._sessions == {}


def test_clip():
    assert _clip("abc", limit=5) == "abc"
    assert _clip("abcdefgh", limit=5).startswith("abcde\n... [truncated 3 characters")

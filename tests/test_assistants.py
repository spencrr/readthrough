"""The study assistants: the rules one end to end, the Copilot one against a fake SDK, and how they're chosen."""

import asyncio
import sys
import types
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import NOW, PROFILE, PROFILE_PATH, REPO

from readthrough import assistants, study
from readthrough.analyze import analyze
from readthrough.assistants import (
    ASSISTANTS,
    CopilotAssistant,
    RulesAssistant,
    Turn,
    _clip,
    open_study_session,
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
def harness(cfg, make_pr) -> Harness:
    item = analyze(make_pr(number=7), profile=PROFILE, viewer="me", instructions=[], diff_text=DIFF, now=NOW)
    return Harness(cfg, [item], _components())


# ---------------------------------------------------------------- choosing


def test_resolve_prefers_copilot_when_installed_and_rules_otherwise(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        assistants, "available", lambda name: (name != "copilot", "no sdk" if name == "copilot" else "")
    )
    assert assistants.resolve("auto") == assistants.Resolved("rules", True, "")
    assert assistants.resolve("copilot") == assistants.Resolved("copilot", False, "no sdk")
    monkeypatch.setattr(assistants, "available", lambda name: (True, ""))
    assert assistants.resolve("auto") == assistants.Resolved("copilot", True, "")
    with pytest.raises(RegistryError, match="did you mean 'rules'"):
        assistants.resolve("ruels")


def test_available_names_the_missing_extra(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "copilot", None)  # makes `import copilot` raise ImportError
    ok, reason = assistants.available("copilot")
    assert not ok and "[study]" in reason
    assert assistants.available("rules") == (True, "")


def test_make_builds_the_configured_assistant(harness, monkeypatch: pytest.MonkeyPatch):
    assert isinstance(assistants.make(harness, "rules"), RulesAssistant)
    monkeypatch.setattr(assistants, "available", lambda name: (True, ""))
    assert isinstance(assistants.make(harness, "auto"), CopilotAssistant)
    assert [p.name for p in ASSISTANTS] == ["rules", "copilot"]


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


# ---------------------------------------------------------------- copilot


class Params(SimpleNamespace):
    def model_dump(self, *, exclude_none: bool = False):
        data = dict(self.__dict__)
        return {k: v for k, v in data.items() if v is not None} if exclude_none else data


def _event(kind: str, **data):
    return SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data))


def _install_fake_copilot(monkeypatch: pytest.MonkeyPatch, client=None) -> None:
    def define_tool(**tool_options):
        def decorate(func):
            func.name = tool_options["name"]
            func.tool_options = tool_options
            return func

        return decorate

    class PermissionDecisionUserNotAvailable:
        pass

    monkeypatch.setitem(
        sys.modules,
        "copilot",
        types.SimpleNamespace(define_tool=define_tool, CopilotClient=(lambda: client) if client else object),
    )
    monkeypatch.setitem(sys.modules, "copilot.generated", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "copilot.generated.rpc",
        types.SimpleNamespace(PermissionDecisionUserNotAvailable=PermissionDecisionUserNotAvailable),
    )


class FakeSession:
    def __init__(self, events):
        self.events = events
        self.sent = []
        self.unsubscribed = False
        self.callback: Callable[[Any], None] = lambda _event: None

    def on(self, callback):
        self.callback = callback

        def unsubscribe():
            self.unsubscribed = True

        return unsubscribe

    async def send(self, prompt):
        self.sent.append(prompt)
        for event in self.events:
            self.callback(event)


def test_system_message_adds_guidance_only_when_present():
    assert system_message("o/r", "").endswith("tools listed are everything you have.\n")
    assert "About this repository:\nuse the framework map\n" in system_message("o/r", "  use the framework map  ")


def test_turn_adds_first_context_on_a_new_session(harness, monkeypatch: pytest.MonkeyPatch):
    copilot = CopilotAssistant(harness, None)
    prompts = []

    async def session_for(number: int) -> tuple[Any, bool]:
        return object(), True

    async def send(number: int, session: Any, prompt: str) -> str:
        prompts.append(prompt)
        return "  Copilot reply  "

    monkeypatch.setattr(copilot, "_session_for", session_for)
    monkeypatch.setattr(copilot, "_send", send)
    asyncio.run(copilot.turn(Turn(7, "chat", "original prompt", None, "original prompt")))
    assert f"We're studying {REPO}#7" in prompts[0] and prompts[0].endswith("original prompt")
    assert study.load(harness.cfg, 7)[-1] | {"text": "Copilot reply", "by": "copilot"} == study.load(harness.cfg, 7)[-1]


def test_send_streams_deltas_records_quiz_leadin_and_unsubscribes(harness):
    session = FakeSession(
        [
            _event("assistant.message_delta", delta_content="Lead-in"),
            _event("tool.execution_start", tool_name="ask_quiz_question"),
            _event("assistant.message_delta", delta_content="After tool"),
            _event("assistant.message", content="After tool"),
            _event("session.idle"),
        ]
    )
    copilot = CopilotAssistant(harness, None)
    text = asyncio.run(copilot._send(7, session, "prompt"))
    assert text == "After tool\n\n"
    assert session.sent == ["prompt"] and session.unsubscribed
    assert study.load(harness.cfg, 7)[0]["text"] == "Lead-in"
    assert ("study", {"pr": 7, "type": "tool", "name": "ask_quiz_question"}) in harness.events
    assert ("study", {"pr": 7, "type": "delta", "text": "After tool"}) in harness.events


def test_send_raises_session_errors_and_timeouts(harness):
    copilot = CopilotAssistant(harness, None)
    error_session = FakeSession([_event("session.error", message="boom")])
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(copilot._send(7, error_session, "prompt"))
    assert error_session.unsubscribed
    harness.cfg.tuning = replace(harness.cfg.tuning, study=replace(harness.cfg.tuning.study, turn_timeout_seconds=0.05))
    timeout_session = FakeSession([])  # never signals idle or error
    with pytest.raises(TimeoutError):
        asyncio.run(copilot._send(7, timeout_session, "prompt"))
    assert timeout_session.unsubscribed


def test_session_for_starts_client_builds_options_and_caches_session(harness, tmp_path, monkeypatch):
    class Client:
        def __init__(self):
            self.started = False
            self.resumed = []

        async def start(self):
            self.started = True

        async def resume_session(self, sid, **options):
            self.resumed.append((sid, options))
            return SimpleNamespace(session_id=sid)

    client = Client()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _install_fake_copilot(monkeypatch, client)
    monkeypatch.setattr(assistants.review, "ensure_worktree", lambda cfg, number, base: (worktree, "base", None))
    monkeypatch.setattr(assistants.review, "session_info", lambda _sid: {"exists": True})

    copilot = CopilotAssistant(harness, None)
    harness.cfg.study_model = "gpt-test"
    session, created = asyncio.run(copilot._session_for(7))
    cached, cached_created = asyncio.run(copilot._session_for(7))

    assert client.started
    assert session is cached and created is False and cached_created is False
    sid, options = client.resumed[0]
    assert copilot._pr_by_session[sid] == 7
    assert options["client_name"] == "readthrough"
    assert options["working_directory"] == str(worktree)
    assert options["model"] == "gpt-test"
    assert options["available_tools"] == [tool.name for tool in copilot._tools or []]
    assert "The learner maintains Courier" in options["system_message"]["content"]
    assert options["on_permission_request"](None, None).__class__.__name__ == "PermissionDecisionUserNotAvailable"


def test_tool_handlers_read_search_truncate_and_append(harness, tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "app.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    copilot = CopilotAssistant(harness, None)
    copilot._worktrees[7] = (worktree, "base")
    copilot._pr_by_session["sid"] = 7
    _install_fake_copilot(monkeypatch)
    tools = {tool.name: tool for tool in copilot._build_tools()}
    commands = []

    def fake_run(cmd, check=True):
        commands.append((cmd, check))
        return "x" * 80

    monkeypatch.setattr(assistants, "run", fake_run)
    invocation = SimpleNamespace(session_id="sid")

    assert asyncio.run(
        tools["read_file"](SimpleNamespace(path="src/app.py", start_line=2, end_line=99), invocation)
    ) == ("src/app.py lines 2-3 of 3\n    2  two\n    3  three")
    assert "No such file" in asyncio.run(
        tools["read_file"](SimpleNamespace(path="src/missing.py", start_line=1, end_line=10), invocation)
    )
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(tools["read_file"](SimpleNamespace(path="../escape.py", start_line=1, end_line=10), invocation))
    diff = asyncio.run(tools["get_diff"](SimpleNamespace(path="src/app.py"), invocation))
    search = asyncio.run(tools["search_code"](SimpleNamespace(pattern="one", path_glob="src/**"), invocation))
    assert diff.endswith("narrow the request]") and search.endswith("narrow the request]")
    assert commands[0][0][-2:] == ["--", "src/app.py"]
    assert commands[1][0][-2:] == ["--", "src/**"]

    assert tools["ask_quiz_question"](
        Params(question="Why?", focus="intent", refs=["src/app.py:1"], expected_points=["point"]), invocation
    ).startswith("Asked Q1")
    assert (
        tools["assess_answer"](Params(qid="Q1", verdict="correct", feedback="yes", evidence=[]), invocation)
        == "Recorded."
    )
    assert (
        tools["assess_teachback"](Params(verdict="partial", feedback="gap", gaps=["tests"]), invocation) == "Recorded."
    )
    assert tools["append_to_brief"](Params(text="Remember this", section=None), invocation) == "Appended."
    assert (
        tools["record_finding"](Params(severity="major", summary="Bug", path="src/app.py", line=2), invocation)
        == "Recorded."
    )
    entries = study.load(harness.cfg, 7)
    assert [e["kind"] for e in entries] == ["question", "assessment", "teachback_assessment", "note", "finding"]
    assert all(e["by"] == "copilot" for e in entries)
    with pytest.raises(RuntimeError, match="study session"):
        copilot._number_for(SimpleNamespace(session_id="unknown"))


def test_stop_disconnects_sessions_and_the_client(harness):
    copilot = CopilotAssistant(harness, None)
    closed = []

    class Session:
        async def disconnect(self):
            closed.append("session")

    class Client:
        async def stop(self):
            closed.append("client")

    copilot._sessions[7] = Session()
    copilot._client = Client()
    asyncio.run(copilot.stop())
    assert closed == ["session", "client"]


def test_open_study_session_falls_back_to_a_fresh_one():
    class Client:
        def __init__(self):
            self.created = []

        async def resume_session(self, sid, **options):
            raise OSError("history is gone")

        async def create_session(self, *, session_id, **options):
            self.created.append(session_id)
            return "session"

    client = Client()
    session, sid, created = asyncio.run(open_study_session(client, "old-id", {}, resume=True))
    assert session == "session" and created and sid != "old-id" and client.created == [sid]


def test_clip():
    assert _clip("abcdef", limit=20) == "abcdef"
    assert _clip("abcdef", limit=3) == "abc\n... [truncated 3 characters; narrow the request]"

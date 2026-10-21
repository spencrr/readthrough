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
from readthrough.agents import SessionInfo
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
def harness(cfg, make_pull) -> Harness:
    item = analyze(make_pull(number=7), profile=PROFILE, viewer="me", instructions=[], diff_text=DIFF, now=NOW)
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
        types.SimpleNamespace(
            define_tool=define_tool, CopilotClient=(lambda **kw: client.built(kw)) if client else object
        ),
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
            self.construction: dict = {}

        def built(self, kwargs):
            self.construction = kwargs
            return self

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
    monkeypatch.setattr(assistants.Copilot, "session_info", lambda _self, sid: SessionInfo(sid, exists=True))

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


def test_copilot_options_describe_a_provider_of_your_own(monkeypatch):
    from readthrough.assistants import CopilotOptions

    default = CopilotOptions()
    assert not default.byok and default.provider_config() is None and default.billed_to() == "copilot"
    with pytest.raises(ValueError, match="provider must be one of"):
        CopilotOptions(provider="gemini", base_url="https://x", model="m")
    with pytest.raises(ValueError, match="needs base_url"):
        CopilotOptions(provider="anthropic", model="claude-sonnet-4-5")
    with pytest.raises(ValueError, match="needs model"):
        CopilotOptions(provider="anthropic", base_url="https://api.anthropic.com")
    opts = CopilotOptions(
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-4-5", api_key_env="MY_KEY"
    )
    assert opts.byok and opts.billed_to() == "anthropic:api.anthropic.com"
    monkeypatch.delenv("MY_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MY_KEY isn't set"):
        opts.provider_config()
    monkeypatch.setenv("MY_KEY", "sk-ant-test")
    assert opts.provider_config() == {
        "type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-test",
    }
    azure = CopilotOptions(
        provider="azure",
        base_url="https://r.openai.azure.com",
        model="gpt-5",
        api_key_env="MY_KEY",
        azure_api_version="2025-04-01",
    )
    assert (azure.provider_config() or {})["azure"] == {"api_version": "2025-04-01"}
    # The config loader validates the table like any plugin's, so a typo is caught there.
    from readthrough.assistants import ASSISTANTS

    with pytest.raises(Exception, match="base_ur"):
        ASSISTANTS.options_for(
            "copilot", {"provider": "openai", "base_ur": "https://x", "model": "m"}, where="[assistants.copilot]"
        )


def test_a_provider_of_your_own_means_no_github_sign_in(harness, tmp_path, monkeypatch):
    """With [assistants.copilot] provider set, the runtime talks to that provider and never falls back to the
    Copilot CLI's GitHub login; the repo's study_model still wins over the provider's model."""
    from readthrough.assistants import CopilotOptions

    class Client:
        def __init__(self):
            self.created = []
            self.construction: dict = {}

        def built(self, kwargs):
            self.construction = kwargs
            return self

        async def start(self):
            pass

        async def create_session(self, session_id, **options):
            self.created.append(options)
            return SimpleNamespace(session_id=session_id)

    client = Client()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _install_fake_copilot(monkeypatch, client)
    monkeypatch.setattr(assistants.review, "ensure_worktree", lambda cfg, number, base: (worktree, "base", None))
    monkeypatch.setattr(assistants.Copilot, "session_info", lambda _self, sid: SessionInfo(sid, exists=False))
    monkeypatch.setenv("READTHROUGH_MODEL_API_KEY", "sk-test")
    opts = CopilotOptions(
        provider="openai", base_url="https://api.openai.com/v1", model="gpt-5-codex", wire_api="responses"
    )

    copilot = CopilotAssistant(harness, opts)
    asyncio.run(copilot._session_for(7))
    assert client.construction == {"use_logged_in_user": False}
    options = client.created[0]
    assert options["provider"] == {
        "type": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test",
        "wire_api": "responses",
    }
    assert options["model"] == "gpt-5-codex"

    harness.cfg.study_model = "o4-mini"
    other = CopilotAssistant(harness, opts)
    asyncio.run(other._session_for(8))
    assert client.created[1]["model"] == "o4-mini", "the repo's own model choice wins"

    plain = CopilotAssistant(harness, None)
    asyncio.run(plain._session_for(9))
    assert client.construction == {}, "GitHub Copilot: the CLI's own sign-in, as before"
    assert "provider" not in client.created[2]


def test_usage_events_become_ledger_lines(harness, monkeypatch):
    """Each assistant.usage event is one ledger line in the provider's own units, idempotent by call id, billed
    to the Copilot viewer or to the provider; a line names what was measured and nothing else."""
    from readthrough import usage
    from readthrough.assistants import CopilotOptions

    harness.cfg.data_path.parent.mkdir(parents=True, exist_ok=True)
    harness.cfg.data_path.write_text('{"viewer": "spencrr"}', encoding="utf-8")
    copilot_usage = SimpleNamespace(total_nano_aiu=1.5e9)
    event = _event("assistant.usage", api_call_id="call-1", model="claude-sonnet-4-5", input_tokens=1200, output_tokens=300,
                   cache_read_tokens=800, cache_write_tokens=None, reasoning_tokens=None, cost=1.0, is_byok=False,
                   copilot_usage=copilot_usage)  # fmt: skip
    session = FakeSession([event, event, _event("session.idle")])  # the same call reported twice
    copilot = CopilotAssistant(harness, None)
    asyncio.run(copilot._send(7, session, "prompt"))
    entries = usage.load(harness.cfg)
    assert len(entries) == 1, "idempotent by call id"
    line = entries[0]
    assert line["id"] == "call-1" and line["purpose"] == "study" and line["ref"] == "#7" and line["by"] == "copilot"
    assert line["billed_to"] == "copilot:spencrr" and line["via"] == "local"
    assert line["tokens"] == {"input": 1200, "output": 300, "cache_read": 800}, "unreported counts are absent, not 0"
    assert line["multiplier"] == 1.0 and line["nano_aiu"] == 1.5e9 and "usd" not in line
    assert sum(1 for e, d in harness.events if d.get("type") == "usage") == 1

    byok = CopilotAssistant(
        harness, CopilotOptions(provider="anthropic", base_url="https://api.anthropic.com", model="m")
    )
    byok_event = _event("assistant.usage", api_call_id="call-2", model="claude-opus-4-8", input_tokens=10, output_tokens=5,
                        cache_read_tokens=None, cache_write_tokens=None, reasoning_tokens=None, cost=None, is_byok=True,
                        copilot_usage=None)  # fmt: skip
    asyncio.run(byok._send(8, FakeSession([byok_event, _event("session.idle")]), "prompt"))
    line = usage.load(harness.cfg)[1]
    assert line["billed_to"] == "anthropic:api.anthropic.com" and "nano_aiu" not in line and "multiplier" not in line

    summary = usage.summarize(usage.load(harness.cfg))
    assert summary["calls"] == 2 and summary["tokens"] == {"input": 1210, "output": 305, "cache_read": 800}
    assert summary["nano_aiu"] == 1.5e9 and summary["usd"] is None and summary["unpriced_calls"] == 1
    assert summary["by_payer"] == {"copilot:spencrr": 1, "anthropic:api.anthropic.com": 1}
    assert usage.describe(summary) == "2 model calls, 1,515 tokens, 1.50 AIU, 1 calls with no price reported"
    assert usage.describe(usage.summarize([])) == "no model calls recorded"
    with pytest.raises(ValueError, match="price table"):
        usage.record(
            harness.cfg, call_id="x", purpose="study", ref="#1", model="m", billed_to="b", by="copilot", usd=1.0
        )


def test_verdicts_name_the_model_that_reached_them(harness):
    """The runtime reports the model in use with each call; what the assistant writes afterwards carries it, and the
    brief says which model graded rather than just "Copilot"."""
    copilot = CopilotAssistant(harness, None)
    study.append(harness.cfg, 7, "question", by="copilot", question="Why?")
    study.append(harness.cfg, 7, "answer", by="you", qid="Q1", text="Because.")
    usage_event = _event("assistant.usage", api_call_id="c1", model="gpt-4.1", input_tokens=1, output_tokens=1,
                         cache_read_tokens=None, cache_write_tokens=None, reasoning_tokens=None, cost=None,
                         is_byok=True, copilot_usage=None)  # fmt: skip
    text = asyncio.run(copilot._send(7, FakeSession([usage_event, _event("assistant.message_delta", delta_content="Right."),
                                                     _event("session.idle")]), "prompt"))  # fmt: skip
    copilot._write(7, "assessment", qid="Q1", verdict="correct", feedback="Yes.")
    copilot._write(7, "chat", role="assistant", text=text)  # as turn() does with what _send returns
    entries = study.load(harness.cfg, 7)
    assert entries[-1]["model"] == "gpt-4.1" and entries[-1]["kind"] == "chat"
    assert entries[-2]["model"] == "gpt-4.1" and entries[-2]["by"] == "copilot" and entries[-2]["kind"] == "assessment"
    assert "assessed by gpt-4.1 (through Copilot)" in study.render_appendix(entries)
    assert study.graders([{"kind": "assessment", "by": "copilot"}]) == "Copilot", (
        "an older log, before models were recorded"
    )
    assert study.graders([]) == "nobody"


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

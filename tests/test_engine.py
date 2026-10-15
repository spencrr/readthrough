import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from readthrough import assistants, study
from readthrough import engine as engine_mod
from readthrough.config import Config
from readthrough.engine import Busy, StudyEngine, public_entry


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(home=tmp_path / "home", repo="o/r", clone=tmp_path / "clone", assistant="rules")
    cfg.briefs_dir.mkdir(parents=True)
    cfg.study_dir.mkdir(parents=True)
    (cfg.briefs_dir / "pr-7.md").write_text("# #7 Title\n\n" + ("brief " * 10), encoding="utf-8")
    return cfg


class RecordingAssistant:
    """Stands in for a real assistant: remembers the turns and can be told to fail."""

    name = "recording"

    def __init__(self, reply: str = "", fail: Exception | None = None):
        self.turns: list[assistants.Turn] = []
        self.reply = reply
        self.fail = fail
        self.stopped = False
        self.harness: engine_mod.StudyEngine | None = None

    async def turn(self, turn: assistants.Turn) -> None:
        self.turns.append(turn)
        if self.fail:
            raise self.fail
        if self.reply and self.harness is not None:
            self.harness.append(turn.number, "chat", by="rules", role="assistant", text=self.reply)

    async def stop(self) -> None:
        self.stopped = True


def _engine(cfg, events=None, assistant=None, pr_info=None) -> StudyEngine:
    engine = StudyEngine(cfg, (lambda e, d: events.append((e, d))) if events is not None else (lambda _e, _d: None),
                         pr_info or (lambda n: {"number": n, "title": "A title"}))  # fmt: skip
    if assistant is not None:
        assistant.harness = engine
        engine._assistant = assistant
    return engine


def test_submit_validates_inputs_and_schedules_a_turn(cfg, monkeypatch: pytest.MonkeyPatch):
    events = []
    engine = _engine(cfg, events, RecordingAssistant(), pr_info=lambda n: {"number": n} if n == 7 else None)
    scheduled = []

    def fake_run_coroutine_threadsafe(coro, loop):
        scheduled.append((coro, loop))
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(engine, "_ensure_loop", lambda: "loop")
    monkeypatch.setattr(engine_mod.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)

    with pytest.raises(ValueError, match="action"):
        engine.submit(7, "dance")
    with pytest.raises(ValueError, match="open PR"):
        engine.submit(8, "quiz")

    engine.submit(7, "quiz", "Focus on tests")
    assert 7 in engine._busy
    assert scheduled and scheduled[0][1] == "loop"
    assert events[-1] == ("study", {"pr": 7, "type": "start", "action": "quiz", "assistant": "rules"})
    with pytest.raises(Busy):
        engine.submit(7, "quiz")


def test_submit_refuses_when_the_assistant_is_unavailable(cfg, monkeypatch: pytest.MonkeyPatch):
    engine = _engine(cfg)
    monkeypatch.setattr(assistants, "resolve", lambda name: assistants.Resolved("copilot", False, "needs the SDK"))
    with pytest.raises(RuntimeError, match="needs the SDK"):
        engine.submit(7, "quiz")


def test_record_and_prompt_covers_actions_and_validation(cfg):
    entries = []
    engine = _engine(cfg, entries)
    study.append(cfg, 7, "question", by="copilot", question="Why?")

    assert "assess_answer" in engine._record_and_prompt(7, "answer", "Because", "Q1")
    assert "assess_teachback" in engine._record_and_prompt(7, "teachback", "It changes routing", None)
    assert "Ask me the next quiz question" in engine._record_and_prompt(7, "quiz", "", None)
    assert "path:line" in engine._record_and_prompt(7, "brief", "", None)
    assert engine._record_and_prompt(7, "hint", "", "Q1") == "Give me a hint for Q1, without the answer."
    hint = study.load(cfg, 7)[-1]
    assert (hint["text"], hint["action"], hint["qid"]) == ("Hint for Q1", "hint", "Q1")
    assert engine._record_and_prompt(7, "chat", "hello", None) == "hello"

    with pytest.raises(ValueError, match="question id"):
        engine._record_and_prompt(7, "answer", "", None)
    with pytest.raises(ValueError, match="teach-back"):
        engine._record_and_prompt(7, "teachback", "", None)
    assert entries[-1][1]["summary"]["questions"] == 1


def test_answers_are_tagged_with_the_aids_that_came_first(cfg):
    engine = _engine(cfg)
    study.append(cfg, 7, "question", by="rules", question="Why?")
    study.append(cfg, 7, "question", by="rules", question="How?")
    engine._record_and_prompt(7, "answer", "Because", "Q1")
    engine._record_and_prompt(7, "hint", "", "Q2")
    engine._record_and_prompt(7, "answer", "Like so", "Q2")
    engine._record_and_prompt(7, "brief", "", None)
    study.append(cfg, 7, "question", by="rules", question="Where?")
    engine._record_and_prompt(7, "answer", "There", "Q3")

    answers = {e["qid"]: e["aided"] for e in study.load(cfg, 7) if e["kind"] == "answer"}
    assert answers == {"Q1": [], "Q2": ["hint"], "Q3": ["brief"]}
    summary = study.summarize(study.load(cfg, 7))
    assert (summary["answered"], summary["unaided"], summary["after_hint"], summary["after_brief"]) == (3, 1, 1, 1)


def test_turn_runs_the_assistant_and_reports_done(cfg):
    events = []
    assistant = RecordingAssistant(reply="  A reply  ")
    engine = _engine(cfg, events, assistant)
    engine._busy.add(7)

    asyncio.run(engine._turn(assistants.Turn(7, "chat", "hello", None, "hello")))

    assert assistant.turns[0].prompt == "hello"
    assert study.load(cfg, 7)[-1]["text"] == "  A reply  "
    assert events[-1] == ("study", {"pr": 7, "type": "done"})
    assert 7 not in engine._busy


def test_turn_reports_errors_and_clears_busy(cfg):
    events = []
    engine = _engine(cfg, events, RecordingAssistant(fail=RuntimeError("no session")))
    engine._busy.add(7)

    asyncio.run(engine._turn(assistants.Turn(7, "quiz", "", None, "quiz")))

    assert events[-1] == ("study", {"pr": 7, "type": "error", "message": "no session"})
    assert 7 not in engine._busy


def test_stop_stops_the_assistant_and_the_loop(cfg):
    assistant = RecordingAssistant()
    engine = _engine(cfg, None, assistant)
    engine.stop()  # no loop yet: nothing to do
    loop = engine._ensure_loop()
    engine.stop()
    for _ in range(50):
        if not loop.is_running():
            break
        asyncio.run(asyncio.sleep(0.01))
    assert assistant.stopped and not loop.is_running()


def test_the_assistant_is_built_from_config(cfg):
    engine = _engine(cfg)
    assert isinstance(engine.assistant, assistants.RulesAssistant)
    assert engine.resolved == assistants.Resolved("rules", True, "")


def test_public_entry_hides_question_answers():
    question = {"kind": "question", "qid": "Q1", "question": "Why?", "expected_points": ["secret"]}
    assert "expected_points" not in public_entry(question, [])
    assert public_entry(question, [{"kind": "answer", "qid": "Q1"}]) == question

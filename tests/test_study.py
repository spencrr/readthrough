import sys
from pathlib import Path

import pytest

from readthrough import study
from readthrough.analyze import author_kind
from readthrough.assistants import study_session_id
from readthrough.config import Config
from readthrough.engine import StudyEngine, public_entries
from readthrough.profile import DEFAULT_KINDS, Authors


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(home=tmp_path / "home", repo="o/r", clone=tmp_path, assistant="rules")
    cfg.briefs_dir.mkdir(parents=True)
    (cfg.briefs_dir / "pr-5.md").write_text("# #5 Title\n\nBase brief.\n", encoding="utf-8")
    return cfg


def test_append_validates_kind_writer_and_fields(cfg):
    with pytest.raises(ValueError, match="unknown"):
        study.append(cfg, 5, "edit", by="you", text="x")
    with pytest.raises(ValueError, match="can't write"):
        study.append(cfg, 5, "question", by="you", question="q?")
    with pytest.raises(ValueError, match="can't write"):
        study.append(cfg, 5, "assessment", by="rules", qid="Q1", verdict="correct", feedback="f")
    with pytest.raises(ValueError, match="needs"):
        study.append(cfg, 5, "prediction", by="you", text="  ")
    with pytest.raises(ValueError, match="no question"):
        study.append(cfg, 5, "answer", by="you", qid="Q9", text="a")
    with pytest.raises(ValueError, match="no question"):
        study.append(cfg, 5, "reflection", by="you", qid="Q9", text="missed it")
    with pytest.raises(ValueError, match="verdict"):
        q = study.append(cfg, 5, "question", by="copilot", question="q?")
        study.append(cfg, 5, "assessment", by="copilot", qid=q["qid"], verdict="great", feedback="f")


def test_rules_mode_log_counts_answers_without_grading_them(cfg):
    q = study.append(cfg, 5, "question", by="rules", question="Why?", focus="intent", group="always")
    study.append(cfg, 5, "chat", by="you", role="user", text="Hint for Q1", action="hint", qid="Q1")
    answer = study.append(cfg, 5, "answer", by="you", qid=q["qid"], text="Because.")
    assert answer["aided"] == ["hint"]
    study.append(cfg, 5, "reflection", by="you", qid="Q1", text="I forgot the retry path.")
    study.append(cfg, 5, "teachback", by="you", text="It adds retries to the transport.")
    study.append(cfg, 5, "reflection", by="you", text="Missed the migration.")

    entries = study.load(cfg, 5)
    s = study.summarize(entries)
    assert (s["answered"], s["unaided"], s["after_hint"], s["assessed"], s["score"]) == (1, 0, 1, 0, None)
    assert (s["teachbacks"], s["teachback"], s["reflections"]) == (1, None, 2)
    appendix = study.render_appendix(entries)
    assert "Quiz: 1 answered, not graded (1 after a hint or briefing)" in appendix
    assert "Teach-back: written, not graded" in appendix
    assert "_(asked by rules)_" in appendix and "_(after a hint)_" in appendix
    assert appendix.count("_What I missed:_") == 2
    assert "correct" not in appendix.lower().replace("not graded", "")


def test_question_ids_summary_and_appendix(cfg):
    study.append(cfg, 5, "prediction", by="you", text="It touches scorers.")
    q1 = study.append(cfg, 5, "question", by="copilot", question="Why?", expected_points=["secret"])
    q2 = study.append(cfg, 5, "question", by="copilot", question="How?", expected_points=["hidden"])
    assert (q1["qid"], q2["qid"]) == ("Q1", "Q2")
    study.append(cfg, 5, "answer", by="you", qid="Q1", text="Because.")
    study.append(
        cfg, 5, "assessment", by="copilot", qid="Q1", verdict="partial", feedback="Look at line 3.", evidence=["a.py:3"]
    )
    study.append(cfg, 5, "assessment", by="copilot", qid="Q1", verdict="correct", feedback="Now yes.")

    entries = study.load(cfg, 5)
    s = study.summarize(entries)
    assert (s["questions"], s["answered"], s["assessed"], s["correct"], s["partial"]) == (2, 1, 1, 1, 0)
    assert s["score"] == 1.0 and s["predictions"] == 1

    appendix = study.render_appendix(entries)
    assert appendix.startswith(study.MARKER)
    assert "secret" not in appendix and "hidden" not in appendix
    assert "**Correct.** Now yes." in appendix

    study.refresh_brief(cfg, 5)
    study.refresh_brief(cfg, 5)
    brief = (cfg.briefs_dir / "pr-5.md").read_text(encoding="utf-8")
    assert brief.count(study.MARKER) == 1 and brief.startswith("# #5 Title")


def test_public_entries_hide_expected_points_until_answered(cfg):
    study.append(cfg, 5, "question", by="copilot", question="Why?", expected_points=["secret"])
    assert "expected_points" not in public_entries(study.load(cfg, 5))[0]
    study.append(cfg, 5, "answer", by="you", qid="Q1", text="guess")
    assert public_entries(study.load(cfg, 5))[0]["expected_points"] == ["secret"]


def test_torn_line_is_skipped(cfg):
    study.append(cfg, 5, "note", by="you", text="ok")
    with study.log_path(cfg, 5).open("a", encoding="utf-8") as handle:
        handle.write('{"id": 2, "kind": "no')
    assert [e["text"] for e in study.load(cfg, 5)] == ["ok"]


def test_engine_records_learner_input_before_prompting(cfg):
    events = []
    engine = StudyEngine(cfg, lambda e, d: events.append((e, d)), lambda n: {"number": n})
    study.append(cfg, 5, "question", by="copilot", question="Why?")
    prompt = engine._record_and_prompt(5, "answer", "My answer", "Q1")
    assert "Q1" in prompt and "assess_answer" in prompt
    assert study.load(cfg, 5)[-1]["kind"] == "answer"
    assert events[-1][0] == "study" and events[-1][1]["type"] == "entry"
    with pytest.raises(ValueError):
        engine._record_and_prompt(5, "chat", "", None)


def test_briefing_and_skipped_prediction_are_on_the_record(cfg):
    engine = StudyEngine(cfg, lambda e, d: None, lambda n: {"number": n})
    study.append(cfg, 5, "prediction", by="you", text="Skipped the prediction.", skipped=True)
    prompt = engine._record_and_prompt(5, "brief", "", None)
    assert "path:line" in prompt and "No quiz question" in prompt
    q = study.append(cfg, 5, "question", by="copilot", question="Why?")
    study.append(cfg, 5, "answer", by="you", qid=q["qid"], text="Because.")
    entries = study.load(cfg, 5)
    s = study.summarize(entries)
    assert (s["predictions"], s["prediction_skipped"], s["briefed"]) == (0, True, True)
    appendix = study.render_appendix(entries)
    assert "_skipped_" in appendix
    assert "briefing before answering any quiz question" in appendix
    study.append(cfg, 5, "note", by="you", text="Worth remembering", pinned=3)
    assert "pinned from the conversation" in study.render_appendix(study.load(cfg, 5))


def test_copilot_tools_refuse_paths_outside_the_worktree(tmp_path: Path):
    from readthrough.assistants import _safe_rel

    (tmp_path / "pkg").mkdir()
    assert _safe_rel(tmp_path, "pkg/../pkg/x.py") == "pkg/x.py"
    escapes = ["../secret.txt", "../../x", str(tmp_path.parent / "x")]
    if sys.platform == "win32":
        escapes.append("..\\..\\x")  # a separator on Windows; on POSIX a backslash is just a character in a name
    for bad in escapes:
        with pytest.raises(ValueError):
            _safe_rel(tmp_path, bad)


def test_study_session_id_is_stable_and_distinct_from_review(tmp_path: Path):
    from readthrough.review import default_session_id

    cfg = Config(home=tmp_path, repo="o/r")
    assert study_session_id(cfg, 1) == study_session_id(cfg, 1)
    assert study_session_id(cfg, 1) != default_session_id(cfg, 1)


@pytest.mark.parametrize(
    ("login", "association", "bot", "expected"),
    [
        ("romanlutz", "MEMBER", False, "internal"),
        ("someone", "COLLABORATOR", False, "internal"),
        ("u7k4rs6", "CONTRIBUTOR", False, "external"),
        ("newbie", "FIRST_TIME_CONTRIBUTOR", False, "first-time"),
        ("dependabot", "CONTRIBUTOR", True, "bot"),
        ("renovate[bot]", "NONE", False, "bot"),
    ],
)
def test_author_kind(login, association, bot, expected):
    assert author_kind(login, association, Authors(kinds=dict(DEFAULT_KINDS)), bot=bot) == expected


def test_author_kind_overrides():
    authors = Authors(internal=["contractor"], external=["alum"], kinds=dict(DEFAULT_KINDS))
    assert author_kind("Contractor", "CONTRIBUTOR", authors) == "internal"
    assert author_kind("alum", "MEMBER", authors) == "external"


def test_handoff_line_says_where_understanding_is_solid(cfg):
    q1 = study.append(cfg, 6, "question", by="copilot", question="Why?", focus="intent")
    q2 = study.append(cfg, 6, "question", by="copilot", question="Who owns it?", focus="responsibility")
    study.append(cfg, 6, "question", by="copilot", question="Tests?", focus="tests")
    for q, v in ((q1, "correct"), (q2, "incorrect")):
        study.append(cfg, 6, "answer", by="you", qid=q["qid"], text="...")
        study.append(cfg, 6, "assessment", by="copilot", qid=q["qid"], verdict=v, feedback="f")
    appendix = study.render_appendix(study.load(cfg, 6))
    assert "**Handoff:** solid on Q1 (intent); revisit Q2 (responsibility); unanswered Q3 (tests)." in appendix


def test_unresumable_study_session_falls_back_to_a_fresh_one():
    import asyncio

    from readthrough.runtimes.copilot import open_study_session

    class Client:
        def __init__(self, broken):
            self.broken, self.created = broken, []

        async def resume_session(self, sid, **options):
            if self.broken:
                raise RuntimeError("Session history cannot be recovered")
            return f"resumed:{sid}"

        async def create_session(self, *, session_id, **options):
            self.created.append(session_id)
            return f"new:{session_id}"

    assert asyncio.run(open_study_session(Client(False), "abc", {}, resume=True)) == ("resumed:abc", False)
    broken = Client(True)
    session, created = asyncio.run(open_study_session(broken, "abc", {}, resume=True))
    assert created and broken.created and broken.created[0] != "abc" and session == f"new:{broken.created[0]}"
    assert asyncio.run(open_study_session(Client(False), "abc", {}, resume=False))[1] is True

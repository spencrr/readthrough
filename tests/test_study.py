import json
import sys
from pathlib import Path
from typing import cast

import pytest

from readthrough import study
from readthrough.analyze import author_kind
from readthrough.assistants import study_session_id
from readthrough.config import Config
from readthrough.contract import Action, PullRequestItem
from readthrough.engine import StudyEngine, public_entries
from readthrough.profile import DEFAULT_KINDS, Authors


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(home=tmp_path / "home", repo="o/r", clone=tmp_path, assistant="rules")
    cfg.briefs_dir.mkdir(parents=True)
    (cfg.briefs_dir / "pr-5.md").write_text("# #5 Title\n\nBase brief.\n", encoding="utf-8")
    return cfg


def test_every_entry_kind_says_who_may_write_it():
    assert set(study.KINDS) == set(study.EntryKind), "a new kind needs its row: who may write it, and what it needs"


def test_append_validates_kind_writer_and_fields(cfg):
    with pytest.raises(ValueError, match="unknown"):
        study.append(cfg, 5, "edit", by="you", head=None, text="x")
    with pytest.raises(ValueError, match="can't write"):
        study.append(cfg, 5, "question", by="you", head=None, question="q?")
    with pytest.raises(ValueError, match="can't write"):
        study.append(cfg, 5, "assessment", by="rules", head=None, qid="Q1", verdict="correct", feedback="f")
    with pytest.raises(ValueError, match="needs"):
        study.append(cfg, 5, "prediction", by="you", head=None, text="  ")
    with pytest.raises(ValueError, match="no question"):
        study.append(cfg, 5, "answer", by="you", head=None, qid="Q9", text="a")
    with pytest.raises(ValueError, match="no question"):
        study.append(cfg, 5, "reflection", by="you", head=None, qid="Q9", text="missed it")
    with pytest.raises(ValueError, match="verdict"):
        q = study.append(cfg, 5, "question", by="model", head=None, question="q?")
        study.append(cfg, 5, "assessment", by="model", head=None, qid=q["qid"], verdict="great", feedback="f")
    # The log's own keys and the shapes of values are not the writer's to choose: a model's tool call is a writer.
    with pytest.raises(ValueError, match="id, ts are set by the log"):
        study.append(cfg, 5, "note", by="you", head=None, text="x", id=1, ts="then")
    with pytest.raises(ValueError, match="line must be a number"):
        study.append(cfg, 5, "finding", by="model", head=None, summary="s", severity="minor", path="a.py", line="<img>")
    with pytest.raises(ValueError, match="refs must be a list of strings"):
        study.append(cfg, 5, "note", by="you", head=None, text="x", refs=[{"x": 1}])
    with pytest.raises(ValueError, match="path must be a string"):
        study.append(cfg, 5, "note", by="you", head=None, text="x", path={"nested": True})
    entry = study.append(
        cfg, 5, "finding", by="model", head=None, summary="s", severity="minor", path="a.py", line="12"
    )
    assert entry["line"] == 12 and entry["id"] == 2


def test_each_entry_records_the_commit_it_was_about(cfg):
    """The log sets ``head`` from what the writer saw. A pinned note is about what it pins, whatever the page shows
    now, and an entry from before heads were recorded stays exactly as it was written."""
    a, b = "a" * 40, "b" * 40
    path = study.log_path(cfg, 5)
    path.parent.mkdir(parents=True)
    old = '{"id": 1, "ts": "2026-01-01T00:00:00+00:00", "kind": "note", "by": "you", "text": "before heads"}\n'
    path.write_text(old, encoding="utf-8")
    assert study.append(cfg, 5, "chat", by="model", head=a, role="assistant", text="hi")["head"] == a
    assert "head" not in study.append(cfg, 5, "note", by="you", head=None, text="unknown"), "absent, never null"
    assert study.append(cfg, 5, "note", by="you", head=b, text="hi", pinned=2)["head"] == a
    assert "head" not in study.append(cfg, 5, "note", by="you", head=b, text="old", pinned=1)
    for bad in ("abc1234", "A" * 40, ""):
        with pytest.raises(ValueError, match="full commit SHA"):
            study.append(cfg, 5, "note", by="you", head=bad, text="x")
    with pytest.raises(ValueError, match="no entry 99"):
        study.append(cfg, 5, "note", by="you", head=a, text="x", pinned=99)
    assert path.read_text(encoding="utf-8").startswith(old), "the log is never rewritten"


def test_appends_from_two_processes_never_share_an_id(cfg):
    """A review agent's terminal (`readthrough study N --finding`) and the server's assistant can write to one
    log at the same moment. Ids come from the file's length, so the read and the append must be one step across
    processes, not only across this process's threads."""
    import subprocess

    script = (
        "import sys; from pathlib import Path; from readthrough import study; from readthrough.config import Config\n"
        f"cfg = Config(home=Path({str(cfg.home)!r}), repo='o/r')\n"
        "for i in range(40): study.append(cfg, 5, 'note', by='you', head=None, text=f'other process {i}')\n"
    )
    other = subprocess.Popen([sys.executable, "-c", script])
    try:
        for i in range(40):
            study.append(cfg, 5, "note", by="you", head=None, text=f"this process {i}")
    finally:
        assert other.wait(timeout=60) == 0
    entries = study.load(cfg, 5)
    assert len(entries) == 80
    assert [e["id"] for e in entries] == list(range(1, 81)), "every id unique and in file order"
    assert sum(e["text"].startswith("other") for e in entries) == 40


def test_older_logs_read_as_the_model_without_a_guessed_runtime(cfg):
    """Older logs say `by: copilot`, the assistant's former name. They're read as `model`; the runtime stays
    unrecorded (the same name was written through other runtimes for a while, so inferring one would be a
    guess), the file itself is never rewritten, and nothing can write the old name again."""
    path = study.log_path(cfg, 5)
    path.parent.mkdir(parents=True)
    old = [
        {"id": 1, "ts": "2026-10-01T00:00:00+00:00", "kind": "question", "by": "copilot", "qid": "Q1", "question": "Why?"},
        {"id": 2, "ts": "2026-10-01T00:00:01+00:00", "kind": "answer", "by": "you", "qid": "Q1", "text": "Because."},
        {"id": 3, "ts": "2026-10-01T00:00:02+00:00", "kind": "assessment", "by": "copilot", "qid": "Q1",
         "verdict": "correct", "feedback": "Yes.", "model": "claude-sonnet-5"},
        {"id": 4, "ts": "2026-10-01T00:00:03+00:00", "kind": "failure", "by": "readthrough", "assistant": "copilot",
         "text": "timed out"},
    ]  # fmt: skip
    path.write_text("".join(json.dumps(e) + "\n" for e in old), encoding="utf-8")
    entries = study.load(cfg, 5)
    assert [e["by"] for e in entries] == ["model", "you", "model", "readthrough"]
    assert not any("runtime" in e for e in entries), "no runtime was recorded, so none is claimed"
    assert entries[-1]["assistant"] == "model"
    assert study.graders(entries) == "claude-sonnet-5"
    assert "assessed by claude-sonnet-5." in study.render_appendix(entries)
    assert '"by": "copilot"' in path.read_text(encoding="utf-8"), "the log is append-only; reading normalizes"
    with pytest.raises(ValueError, match="can't write"):
        study.append(cfg, 5, "chat", by="copilot", head=None, role="assistant", text="x")


def test_rules_mode_log_counts_answers_without_grading_them(cfg):
    q = study.append(cfg, 5, "question", by="rules", head=None, question="Why?", focus="intent", group="always")
    study.append(cfg, 5, "chat", by="you", head=None, role="user", text="Hint for Q1", action="hint", qid="Q1")
    answer = study.append(cfg, 5, "answer", by="you", head=None, qid=q["qid"], text="Because.")
    assert answer["aided"] == ["hint"]
    study.append(cfg, 5, "reflection", by="you", head=None, qid="Q1", text="I forgot the retry path.")
    study.append(cfg, 5, "teachback", by="you", head=None, text="It adds retries to the transport.")
    study.append(cfg, 5, "reflection", by="you", head=None, text="Missed the migration.")

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
    study.append(cfg, 5, "prediction", by="you", head=None, text="It touches scorers.")
    q1 = study.append(cfg, 5, "question", by="model", head=None, question="Why?", expected_points=["secret"])
    q2 = study.append(cfg, 5, "question", by="model", head=None, question="How?", expected_points=["hidden"])
    assert (q1["qid"], q2["qid"]) == ("Q1", "Q2")
    study.append(cfg, 5, "answer", by="you", head=None, qid="Q1", text="Because.")
    study.append(
        cfg,
        5,
        "assessment",
        by="model",
        head=None,
        qid="Q1",
        verdict="partial",
        feedback="Look at line 3.",
        evidence=["a.py:3"],
    )
    study.append(cfg, 5, "assessment", by="model", head=None, qid="Q1", verdict="correct", feedback="Now yes.")

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
    study.append(cfg, 5, "question", by="model", head=None, question="Why?", expected_points=["secret"])
    assert "expected_points" not in public_entries(study.load(cfg, 5))[0]
    study.append(cfg, 5, "answer", by="you", head=None, qid="Q1", text="guess")
    assert public_entries(study.load(cfg, 5))[0]["expected_points"] == ["secret"]


def test_torn_line_is_skipped(cfg):
    study.append(cfg, 5, "note", by="you", head=None, text="ok")
    with study.log_path(cfg, 5).open("a", encoding="utf-8") as handle:
        handle.write('{"id": 2, "kind": "no')
    assert [e["text"] for e in study.load(cfg, 5)] == ["ok"]


def test_engine_records_learner_input_before_prompting(cfg):
    events = []
    engine = StudyEngine(cfg, lambda e, d: events.append((e, d)), lambda n: cast("PullRequestItem", {"number": n}))
    study.append(cfg, 5, "question", by="model", head=None, question="Why?")
    prompt = engine._record_and_prompt(5, Action.ANSWER, "My answer", "Q1", None)
    assert "Q1" in prompt and "assess_answer" in prompt
    assert study.load(cfg, 5)[-1]["kind"] == "answer"
    assert events[-1][0] == "study" and events[-1][1]["type"] == "entry"
    with pytest.raises(ValueError):
        engine._record_and_prompt(5, Action.CHAT, "", None, None)


def test_briefing_and_skipped_prediction_are_on_the_record(cfg):
    engine = StudyEngine(cfg, lambda e, d: None, lambda n: cast("PullRequestItem", {"number": n}))
    study.append(cfg, 5, "prediction", by="you", head=None, text="Skipped the prediction.", skipped=True)
    prompt = engine._record_and_prompt(5, Action.BRIEF, "", None, None)
    assert "path:line" in prompt and "No quiz question" in prompt
    q = study.append(cfg, 5, "question", by="model", head=None, question="Why?")
    study.append(cfg, 5, "answer", by="you", head=None, qid=q["qid"], text="Because.")
    entries = study.load(cfg, 5)
    s = study.summarize(entries)
    assert (s["predictions"], s["prediction_skipped"], s["briefed"]) == (0, True, True)
    appendix = study.render_appendix(entries)
    assert "_skipped_" in appendix
    assert "briefing before answering any quiz question" in appendix
    study.append(cfg, 5, "note", by="you", head=None, text="Worth remembering", pinned=3)
    assert "pinned from the conversation" in study.render_appendix(study.load(cfg, 5))


def test_model_tools_read_only_paths_inside_the_repo():
    """The tools read the commit by tree path, so nothing on disk is followed; what's refused is a path that
    could only mean somewhere else."""
    from readthrough.assistants import _repo_path

    assert _repo_path("pkg/x.py") == "pkg/x.py" and _repo_path("pkg\\x.py") == "pkg/x.py"
    for bad in ["../secret.txt", "pkg/../../x", "/etc/passwd", "C:/x", "C:\\x", "\\\\server\\share\\x", ""]:
        with pytest.raises(ValueError, match="inside the repo"):
            _repo_path(bad)


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
    q1 = study.append(cfg, 6, "question", by="model", head=None, question="Why?", focus="intent")
    q2 = study.append(cfg, 6, "question", by="model", head=None, question="Who owns it?", focus="responsibility")
    study.append(cfg, 6, "question", by="model", head=None, question="Tests?", focus="tests")
    for q, v in ((q1, "correct"), (q2, "incorrect")):
        study.append(cfg, 6, "answer", by="you", head=None, qid=q["qid"], text="...")
        study.append(cfg, 6, "assessment", by="model", head=None, qid=q["qid"], verdict=v, feedback="f")
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

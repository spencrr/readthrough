"""The study log: an append-only record of how well you understand each PR.

Everything that shows understanding (or the lack of it) is appended here: your prediction before
reading (or that you skipped it), whether you asked for a briefing, quiz questions and your answers (each
tagged with whether a hint or a briefing came first), the assistant's assessments with code evidence, your
teach-back, your "what I missed" reflections, pinned notes, and findings. Entries are never edited or
deleted, so the log is an honest trail. Only Copilot writes verdicts; the rules assistant never does, so a
score always comes from one grader that read the code.

The log lives in the repo's data directory (``study/pr-<n>.jsonl``), and its evidence is rendered as the last
section of the brief, so a terminal deep review reads the same evidence the dashboard shows.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

from readthrough.config import Config
from readthrough.limits import MAX_STUDY_TEXT

MARKER = "<!-- readthrough:study-log -->"
VERDICTS = ("correct", "partial", "incorrect")
SEVERITIES = ("blocker", "major", "minor", "question")

ASSISTANTS = ("copilot", "rules")
AIDS = ("hint", "brief")  # what can come before an answer and inform it
TOOL = "readthrough"  # the engine itself, which only ever records that an assistant failed to answer

# kind -> (who may write it, required fields)
KINDS: dict[str, tuple[set[str], tuple[str, ...]]] = {
    "prediction": ({"you"}, ("text",)),
    "teachback": ({"you"}, ("text",)),
    "answer": ({"you"}, ("qid", "text")),
    "reflection": ({"you"}, ("text",)),  # "what I missed", after an answer or a teach-back
    "chat": ({"you", *ASSISTANTS}, ("role", "text")),
    "note": ({"you", *ASSISTANTS}, ("text",)),
    "finding": ({"you", "copilot"}, ("severity", "summary")),
    "question": (set(ASSISTANTS), ("question",)),
    "assessment": ({"copilot"}, ("qid", "verdict", "feedback")),
    "teachback_assessment": ({"copilot"}, ("verdict", "feedback")),
    # A turn that ended in an error, so a request with no reply is never read as one you abandoned.
    "failure": ({TOOL}, ("assistant", "text")),
}

_lock = threading.Lock()


def log_path(cfg: Config, number: int) -> Path:
    return cfg.study_dir / f"pr-{number}.jsonl"


def load(cfg: Config, number: int) -> list[dict[str, Any]]:
    path = log_path(cfg, number)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue  # a torn final line from a crash; skip it rather than lose the log
    return entries


def append(cfg: Config, number: int, kind: str, *, by: str, **fields: Any) -> dict[str, Any]:
    """Validate and append one entry. Returns the stored entry (with its id)."""
    if kind not in KINDS:
        raise ValueError(f"unknown entry kind {kind!r}")
    writers, required = KINDS[kind]
    if by not in writers:
        raise ValueError(f"{by} can't write {kind} entries")
    missing = [f for f in required if not str(fields.get(f, "")).strip()]
    if missing:
        raise ValueError(f"{kind} needs {', '.join(missing)}")
    for key, value in fields.items():
        if isinstance(value, str) and len(value) > MAX_STUDY_TEXT:
            fields[key] = value[:MAX_STUDY_TEXT]
    if kind in ("assessment", "teachback_assessment") and fields["verdict"] not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    if kind == "finding" and fields["severity"] not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")

    with _lock:
        entries = load(cfg, number)
        if (
            kind in ("answer", "assessment", "reflection")
            and fields.get("qid") is not None
            and not any(e["kind"] == "question" and e.get("qid") == fields["qid"] for e in entries)
        ):
            raise ValueError(f"no question {fields['qid']}")
        if kind == "answer":
            fields["aided"] = aids_before(entries, fields["qid"])
        if kind == "question":
            fields["qid"] = f"Q{sum(e['kind'] == 'question' for e in entries) + 1}"
        entry = {
            "id": len(entries) + 1,
            "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "kind": kind,
            "by": by,
            **fields,
        }
        path = log_path(cfg, number)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    return entry


def aids_before(entries: list[dict[str, Any]], qid: str) -> list[str]:
    """What the learner had seen before answering ``qid``: a hint for it, or a briefing. Computed from the
    log, never asserted by anyone, so "worked out unaided" is something a reader can check."""
    aids = []
    if any(e["kind"] == "chat" and e.get("action") == "hint" and e.get("qid") == qid for e in entries):
        aids.append("hint")
    if any(e["kind"] == "chat" and e.get("action") == "brief" for e in entries):
        aids.append("brief")
    return aids


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    questions = {e["qid"]: e for e in entries if e["kind"] == "question"}
    latest_assessment: dict[str, str] = {}
    for e in entries:
        if e["kind"] == "assessment":
            latest_assessment[e["qid"]] = e["verdict"]
    answers = {e["qid"]: e for e in entries if e["kind"] == "answer" and e["qid"] in questions}
    counts = {v: sum(1 for x in latest_assessment.values() if x == v) for v in VERDICTS}
    assessed = sum(counts.values())
    teachbacks = [e for e in entries if e["kind"] == "teachback_assessment"]
    return {
        "questions": len(questions),
        "answered": len(answers),
        "unaided": sum(1 for a in answers.values() if not a.get("aided")),
        "after_hint": sum(1 for a in answers.values() if "hint" in (a.get("aided") or [])),
        "after_brief": sum(1 for a in answers.values() if "brief" in (a.get("aided") or [])),
        "assessed": assessed,
        **counts,
        "score": round((counts["correct"] + 0.5 * counts["partial"]) / assessed, 2) if assessed else None,
        "predictions": sum(e["kind"] == "prediction" and not e.get("skipped") for e in entries),
        "prediction_skipped": any(e["kind"] == "prediction" and e.get("skipped") for e in entries),
        "briefed": any(e["kind"] == "chat" and e.get("action") == "brief" for e in entries),
        "teachback": teachbacks[-1]["verdict"] if teachbacks else None,
        "teachbacks": sum(e["kind"] == "teachback" for e in entries),
        "reflections": sum(e["kind"] == "reflection" for e in entries),
        "findings": sum(e["kind"] == "finding" for e in entries),
        "notes": sum(e["kind"] == "note" for e in entries),
    }


def _handoff(entries: list[dict[str, Any]]) -> str:
    """One line for whoever picks this up next (a deep review): where understanding is solid and where it isn't."""
    focus = {e["qid"]: e.get("focus") or "" for e in entries if e["kind"] == "question"}
    verdict = {e["qid"]: e["verdict"] for e in entries if e["kind"] == "assessment"}
    answered = {e["qid"] for e in entries if e["kind"] == "answer"}

    def names(qids: list[str]) -> str:
        return ", ".join(f"{q} ({focus[q]})" if focus.get(q) else q for q in qids)

    right = [q for q, v in verdict.items() if v == "correct"]
    shaky = [q for q, v in verdict.items() if v != "correct"]
    open_ = [q for q in focus if q not in answered]
    parts = []
    if right:
        parts.append(f"solid on {names(right)}")
    if shaky:
        parts.append(f"revisit {names(shaky)}")
    if open_:
        parts.append(f"unanswered {names(open_)}")
    teach = [e for e in entries if e["kind"] == "teachback_assessment"]
    if teach and teach[-1].get("gaps"):
        parts.append("teach-back gaps: " + "; ".join(teach[-1]["gaps"]))
    return "; ".join(parts) + "." if parts else ""


def _quote(text: str) -> str:
    return "\n".join("> " + line for line in text.strip().splitlines())


def render_appendix(entries: list[dict[str, Any]]) -> str:
    """Markdown for the end of the brief. Unanswered questions don't show their expected points."""
    if not entries:
        return ""
    s = summarize(entries)
    out = [MARKER, "", "## 9. Study log (evidence of understanding)", ""]
    if s["assessed"]:
        score = f"{s['correct']} correct, {s['partial']} partial, {s['incorrect']} incorrect of {s['assessed']} assessed by Copilot"
    elif s["answered"]:
        aided = s["answered"] - s["unaided"]
        score = f"{s['answered']} answered, not graded" + (f" ({aided} after a hint or briefing)" if aided else "")
    else:
        score = "no answers yet"
    teach = s["teachback"] or ("written, not graded" if s["teachbacks"] else "not yet")
    out.append(f"Quiz: {score}. Predictions: {s['predictions']}. Teach-back: {teach}. Findings: {s['findings']}.\n")
    handoff = _handoff(entries)
    if handoff:
        out.append(f"**Handoff:** {handoff}\n")
    if s["briefed"]:
        first_brief = next(e["id"] for e in entries if e["kind"] == "chat" and e.get("action") == "brief")
        first_quiz = next((e["id"] for e in entries if e["kind"] == "answer"), None)
        when = (
            "before answering any quiz question"
            if first_quiz is None or first_brief < first_quiz
            else "after starting the quiz"
        )
        out.append(f"Asked for a briefing {when}, so treat later answers as informed by it.\n")

    def section(title: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        picked = [e for e in entries if e["kind"] in kinds]
        if picked:
            if out[-1] != "":
                out.append("")
            out.append(f"### {title}\n")
        return picked

    out.extend(
        f"- {e['ts'][:16].replace('T', ' ')}: " + ("_skipped_" if e.get("skipped") else e["text"].strip())
        for e in section("Predictions (before reading)", ("prediction",))
    )
    answers = {e["qid"]: e for e in entries if e["kind"] == "answer"}
    assessments = {e["qid"]: e for e in entries if e["kind"] == "assessment"}
    reflections: dict[str | None, list[dict[str, Any]]] = {}
    for e in entries:
        if e["kind"] == "reflection":
            reflections.setdefault(e.get("qid"), []).append(e)
    for q in section("Quiz", ("question",)):
        out.append(
            f"**{q['qid']}.** {q['question'].strip()}"
            + (f" _(refs: {', '.join(q.get('refs') or [])})_" if q.get("refs") else "")
            + (f" _(asked by {q['by']})_" if q["by"] != "copilot" else "")
        )
        if q["qid"] in answers:
            a = answers[q["qid"]]
            aided = ", ".join(f"after a {x}" for x in a.get("aided") or [])
            out.append(_quote(a["text"]) + (f"\n\n_({aided})_" if aided else ""))
        if q["qid"] in assessments:
            a = assessments[q["qid"]]
            evidence = f" Evidence: {', '.join(a.get('evidence') or [])}." if a.get("evidence") else ""
            out.append(f"\n**{a['verdict'].capitalize()}.** {a['feedback'].strip()}{evidence}")
        out.extend(f"\n_What I missed:_ {r['text'].strip()}" for r in reflections.get(q["qid"], []))
        out.append("")
    for e in section("Teach-back", ("teachback", "teachback_assessment")):
        out.append(
            _quote(e["text"])
            if e["kind"] == "teachback"
            else f"**{e['verdict'].capitalize()}.** {e['feedback'].strip()}"
        )
        out.append("")
    out.extend(f"_What I missed:_ {r['text'].strip()}\n" for r in reflections.get(None, []))
    for e in section("Findings", ("finding",)):
        where = f"`{e['path']}:{e.get('line') or '?'}` " if e.get("path") else ""
        out.append(f"- [{e['severity']}] {where}{e['summary'].strip()} _({e['by']})_")
    for e in section("Pinned notes", ("note",)):
        who = "pinned from the conversation" if e.get("pinned") else e["by"]
        out.append(f"- {e['text'].strip()} _({who}{', ' + e['section'] if e.get('section') else ''})_")
    return "\n".join(out).rstrip() + "\n"


def refresh_brief(cfg: Config, number: int) -> None:
    """Rewrite the study section at the end of the brief after the log changes."""
    path = cfg.briefs_dir / f"pr-{number}.md"
    if not path.exists():
        return
    base = path.read_text(encoding="utf-8").split(MARKER, 1)[0].rstrip() + "\n"
    appendix = render_appendix(load(cfg, number))
    path.write_text(base + ("\n" + appendix if appendix else ""), encoding="utf-8")

"""The study log: an append-only record of how well you understand each PR.

Everything that shows understanding (or the lack of it) is appended here: your prediction before
reading (or that you skipped it), whether you asked for a briefing, quiz questions and your answers (each
tagged with whether a hint or a briefing came first), the assistant's assessments with code evidence, your
teach-back, your "what I missed" reflections, pinned notes, and findings. Entries are never edited or
deleted, so the log is an honest trail.

``by`` says what kind of author wrote an entry: ``you``, ``model`` (the Socratic assistant, on whichever model
runtime is configured), or ``rules`` (the assistant that needs no model). Only an author that read the code
writes verdicts; the rules assistant never does. What the model writes also carries ``model`` (the model the
runtime reported using) and ``runtime`` (which runtime carried the conversation), because a verdict's weight
depends on who reached it and the same assistant may be talking to GitHub Copilot, Claude, or a deployment
of yours. Both come from the entry, never from the current configuration, so changing runtimes never relabels
an old grade. Older logs say ``by: copilot``, the assistant's former name; ``load`` reads it as ``model`` rather
than rewriting the file, and leaves the runtime unrecorded, since those entries never said which it was.

Each entry records ``head``, the commit it was about, when that's known: the log sets it from what the writer
saw (the page's head for the learner, the commit a turn read for the model), and a pinned note takes its pinned
entry's. An entry without one, including every entry from before this was recorded, says nothing about which
commit it was about.

The log lives in the repo's data directory (``study/pr-<n>.jsonl``), and its evidence is rendered as the last
section of the brief, so a terminal deep review reads the same evidence the dashboard shows.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from enum import StrEnum
from pathlib import Path
from typing import Any

from readthrough.config import Config
from readthrough.contract import Action, StudySummary
from readthrough.files import locked
from readthrough.limits import MAX_STUDY_TEXT

MARKER = "<!-- readthrough:study-log -->"
RESERVED = frozenset({"id", "ts", "kind", "by", "head"})
COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # a full SHA-1 or SHA-256 object name


class Writer(StrEnum):
    """Who wrote an entry: its ``by``."""

    YOU = "you"
    MODEL = "model"  # the Socratic assistant, whichever model runtime carries it
    RULES = "rules"  # the assistant that needs no model and never grades
    TOOL = "readthrough"  # the engine itself, which only ever records that an assistant failed to answer


ASSISTANTS = (Writer.MODEL, Writer.RULES)
LEGACY_MODEL_BY = "copilot"  # the model assistant's former name in the log, after its first engine


class EntryKind(StrEnum):
    PREDICTION = "prediction"
    TEACHBACK = "teachback"
    ANSWER = "answer"
    REFLECTION = "reflection"  # "what I missed", after an answer or a teach-back
    CHAT = "chat"
    NOTE = "note"
    FINDING = "finding"
    QUESTION = "question"
    ASSESSMENT = "assessment"
    TEACHBACK_ASSESSMENT = "teachback_assessment"
    FAILURE = "failure"  # a turn that ended in an error, so a request with no reply is never read as one you abandoned


# What the learner writes directly (``POST /api/study/<n>`` body ``kind``), without asking the assistant anything;
# the rest are the assistants' and the engine's. ``note`` with ``pinned`` keeps a message in the brief; ``prediction``
# with ``skipped`` records skipping it.
LEARNER_KINDS = frozenset({
    EntryKind.PREDICTION, EntryKind.TEACHBACK, EntryKind.NOTE, EntryKind.REFLECTION, EntryKind.FINDING,
})  # fmt: skip


class Verdict(StrEnum):
    CORRECT = "correct"
    PARTIAL = "partial"
    INCORRECT = "incorrect"


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    QUESTION = "question"


# kind -> (who may write it, required fields)
KINDS: dict[EntryKind, tuple[set[Writer], tuple[str, ...]]] = {
    EntryKind.PREDICTION: ({Writer.YOU}, ("text",)),
    EntryKind.TEACHBACK: ({Writer.YOU}, ("text",)),
    EntryKind.ANSWER: ({Writer.YOU}, ("qid", "text")),
    EntryKind.REFLECTION: ({Writer.YOU}, ("text",)),
    EntryKind.CHAT: ({Writer.YOU, *ASSISTANTS}, ("role", "text")),
    EntryKind.NOTE: ({Writer.YOU, *ASSISTANTS}, ("text",)),
    EntryKind.FINDING: ({Writer.YOU, Writer.MODEL}, ("severity", "summary")),
    EntryKind.QUESTION: (set(ASSISTANTS), ("question",)),
    EntryKind.ASSESSMENT: ({Writer.MODEL}, ("qid", "verdict", "feedback")),
    EntryKind.TEACHBACK_ASSESSMENT: ({Writer.MODEL}, ("verdict", "feedback")),
    EntryKind.FAILURE: ({Writer.TOOL}, ("assistant", "text")),
}
GRADES = (EntryKind.ASSESSMENT, EntryKind.TEACHBACK_ASSESSMENT)  # the entries that carry a verdict

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
                entries.append(_current(json.loads(line)))
            except ValueError:
                continue  # a torn final line from a crash; skip it rather than lose the log
    return entries


def _current(entry: dict[str, Any]) -> dict[str, Any]:
    """An entry as this version names things. The model assistant used to write as ``copilot``; that was its
    name, not a record of the runtime (the same name was written through other runtimes for a while), so the
    author is renamed and the runtime is left unrecorded rather than guessed."""
    if entry.get("by") == LEGACY_MODEL_BY:
        entry["by"] = Writer.MODEL
    if entry.get("kind") == EntryKind.FAILURE and entry.get("assistant") == LEGACY_MODEL_BY:
        entry["assistant"] = Writer.MODEL
    return entry


def append(cfg: Config, number: int, kind: str, *, by: str, head: str | None, **fields: Any) -> dict[str, Any]:
    """Validate and append one entry. ``kind`` and ``by`` arrive as text from a request or a tool call, and
    are checked here. ``head`` is the commit the entry is about, or None when the writer can't know it; every
    caller has to say which. Returns the stored entry (with its id)."""
    if head is not None and not (isinstance(head, str) and COMMIT_RE.fullmatch(head)):
        raise ValueError("head must be a full commit SHA, lowercase")
    try:
        kind = EntryKind(kind)
    except ValueError:
        raise ValueError(f"unknown entry kind {kind!r}") from None
    writers, required = KINDS[kind]
    if by not in writers:
        raise ValueError(f"{by} can't write {kind} entries")
    missing = [f for f in required if not str(fields.get(f, "")).strip()]
    if missing:
        raise ValueError(f"{kind} needs {', '.join(missing)}")
    # The log sets these; a writer (a model's tool call included) can't name them, or the record would lie.
    reserved = RESERVED & set(fields)
    if reserved:
        raise ValueError(f"{', '.join(sorted(reserved))} are set by the log, not the writer")
    for key, value in fields.items():
        if isinstance(value, list):
            if not all(isinstance(v, str) for v in value):
                raise ValueError(f"{key} must be a list of strings")
            fields[key] = [v[:MAX_STUDY_TEXT] for v in value]
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"{key} must be a string, a number, true or false, or a list of strings")
        elif isinstance(value, str) and len(value) > MAX_STUDY_TEXT:
            fields[key] = value[:MAX_STUDY_TEXT]
    if fields.get("line") is not None:
        try:
            fields["line"] = int(fields["line"])
        except (TypeError, ValueError):
            raise ValueError("line must be a number") from None
    if kind in GRADES and fields["verdict"] not in list(Verdict):
        raise ValueError(f"verdict must be one of {', '.join(Verdict)}")
    if kind == EntryKind.FINDING and fields["severity"] not in list(Severity):
        raise ValueError(f"severity must be one of {', '.join(Severity)}")

    # Ids and question numbers come from what's already in the file, so reading and appending must be one step
    # for every writer: this process's threads (the lock) and other processes, such as a review agent's
    # terminal recording a finding while the server's assistant writes (the file lock).
    path = log_path(cfg, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, locked(path):
        entries = load(cfg, number)
        if (
            kind in (EntryKind.ANSWER, EntryKind.ASSESSMENT, EntryKind.REFLECTION)
            and fields.get("qid") is not None
            and not any(e["kind"] == EntryKind.QUESTION and e.get("qid") == fields["qid"] for e in entries)
        ):
            raise ValueError(f"no question {fields['qid']}")
        if kind == EntryKind.NOTE and fields.get("pinned") is not None:
            pinned = next((e for e in entries if e["id"] == fields["pinned"]), None)
            if pinned is None:
                raise ValueError(f"no entry {fields['pinned']} to pin")
            head = pinned.get("head")  # what the pinned words were about, whatever the page shows now
        if kind == EntryKind.ANSWER:
            fields["aided"] = aids_before(entries, fields["qid"])
        if kind == EntryKind.QUESTION:
            fields["qid"] = f"Q{sum(e['kind'] == EntryKind.QUESTION for e in entries) + 1}"
        entry = {
            "id": len(entries) + 1,
            "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "kind": kind,
            "by": by,
            **({"head": head} if head else {}),
            **fields,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    return entry


def aids_before(entries: list[dict[str, Any]], qid: str) -> list[str]:
    """What the learner had seen before answering ``qid``: a hint for it, or a briefing. Computed from the
    log, never asserted by anyone, so "worked out unaided" is something a reader can check."""
    aids: list[str] = []
    if any(e["kind"] == EntryKind.CHAT and e.get("action") == Action.HINT and e.get("qid") == qid for e in entries):
        aids.append(Action.HINT)
    if any(e["kind"] == EntryKind.CHAT and e.get("action") == Action.BRIEF for e in entries):
        aids.append(Action.BRIEF)
    return aids


def summarize(entries: list[dict[str, Any]]) -> StudySummary:
    questions = {e["qid"]: e for e in entries if e["kind"] == EntryKind.QUESTION}
    latest_assessment: dict[str, str] = {}
    for e in entries:
        if e["kind"] == EntryKind.ASSESSMENT:
            latest_assessment[e["qid"]] = e["verdict"]
    answers = {e["qid"]: e for e in entries if e["kind"] == EntryKind.ANSWER and e["qid"] in questions}
    counts = {v: sum(1 for x in latest_assessment.values() if x == v) for v in Verdict}
    assessed = sum(counts.values())
    teachbacks = [e for e in entries if e["kind"] == EntryKind.TEACHBACK_ASSESSMENT]
    return {
        "questions": len(questions),
        "answered": len(answers),
        "unaided": sum(1 for a in answers.values() if not a.get("aided")),
        "after_hint": sum(1 for a in answers.values() if Action.HINT in (a.get("aided") or [])),
        "after_brief": sum(1 for a in answers.values() if Action.BRIEF in (a.get("aided") or [])),
        "assessed": assessed,
        "correct": counts[Verdict.CORRECT],
        "partial": counts[Verdict.PARTIAL],
        "incorrect": counts[Verdict.INCORRECT],
        "score": round((counts[Verdict.CORRECT] + 0.5 * counts[Verdict.PARTIAL]) / assessed, 2) if assessed else None,
        "predictions": sum(e["kind"] == EntryKind.PREDICTION and not e.get("skipped") for e in entries),
        "prediction_skipped": any(e["kind"] == EntryKind.PREDICTION and e.get("skipped") for e in entries),
        "briefed": any(e["kind"] == EntryKind.CHAT and e.get("action") == Action.BRIEF for e in entries),
        "teachback": teachbacks[-1]["verdict"] if teachbacks else None,
        "teachbacks": sum(e["kind"] == EntryKind.TEACHBACK for e in entries),
        "reflections": sum(e["kind"] == EntryKind.REFLECTION for e in entries),
        "findings": sum(e["kind"] == EntryKind.FINDING for e in entries),
        "notes": sum(e["kind"] == EntryKind.NOTE for e in entries),
    }


def graders(entries: list[dict[str, Any]]) -> str:
    """Who graded, as precisely as the log knows: the models named on the verdicts and the runtime that carried
    them, else "a model" whose name wasn't recorded. Always from the entries, never from the current
    configuration: a verdict's weight depends on who reached it, and switching runtimes must not relabel it.
    """
    verdicts = [e for e in entries if e["kind"] in GRADES]
    if not verdicts:
        return "nobody"
    models = sorted({e["model"] for e in verdicts if e.get("model")})
    runtimes = sorted({e["runtime"] for e in verdicts if e.get("runtime")})
    who = ", ".join(models) if models else "a model not named in the log"
    via = f" via {', '.join(runtimes)}" if runtimes else ""
    return who + via


def _handoff(entries: list[dict[str, Any]]) -> str:
    """One line for whoever picks this up next (a deep review): where understanding is solid and where it isn't."""
    focus = {e["qid"]: e.get("focus") or "" for e in entries if e["kind"] == EntryKind.QUESTION}
    verdict = {e["qid"]: e["verdict"] for e in entries if e["kind"] == EntryKind.ASSESSMENT}
    answered = {e["qid"] for e in entries if e["kind"] == EntryKind.ANSWER}

    def names(qids: list[str]) -> str:
        return ", ".join(f"{q} ({focus[q]})" if focus.get(q) else q for q in qids)

    right = [q for q, v in verdict.items() if v == Verdict.CORRECT]
    shaky = [q for q, v in verdict.items() if v != Verdict.CORRECT]
    open_ = [q for q in focus if q not in answered]
    parts = []
    if right:
        parts.append(f"solid on {names(right)}")
    if shaky:
        parts.append(f"revisit {names(shaky)}")
    if open_:
        parts.append(f"unanswered {names(open_)}")
    teach = [e for e in entries if e["kind"] == EntryKind.TEACHBACK_ASSESSMENT]
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
        score = f"{s['correct']} correct, {s['partial']} partial, {s['incorrect']} incorrect of {s['assessed']} assessed by {graders(entries)}"
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
        first_brief = next(e["id"] for e in entries if e["kind"] == EntryKind.CHAT and e.get("action") == Action.BRIEF)
        first_quiz = next((e["id"] for e in entries if e["kind"] == EntryKind.ANSWER), None)
        when = (
            "before answering any quiz question"
            if first_quiz is None or first_brief < first_quiz
            else "after starting the quiz"
        )
        out.append(f"Asked for a briefing {when}, so treat later answers as informed by it.\n")

    def section(title: str, kinds: tuple[EntryKind, ...]) -> list[dict[str, Any]]:
        picked = [e for e in entries if e["kind"] in kinds]
        if picked:
            if out[-1] != "":
                out.append("")
            out.append(f"### {title}\n")
        return picked

    out.extend(
        f"- {e['ts'][:16].replace('T', ' ')}: " + ("_skipped_" if e.get("skipped") else e["text"].strip())
        for e in section("Predictions (before reading)", (EntryKind.PREDICTION,))
    )
    answers = {e["qid"]: e for e in entries if e["kind"] == EntryKind.ANSWER}
    assessments = {e["qid"]: e for e in entries if e["kind"] == EntryKind.ASSESSMENT}
    reflections: dict[str | None, list[dict[str, Any]]] = {}
    for e in entries:
        if e["kind"] == EntryKind.REFLECTION:
            reflections.setdefault(e.get("qid"), []).append(e)
    for q in section("Quiz", (EntryKind.QUESTION,)):
        out.append(
            f"**{q['qid']}.** {q['question'].strip()}"
            + (f" _(refs: {', '.join(q.get('refs') or [])})_" if q.get("refs") else "")
            + (f" _(asked by {q['by']})_" if q["by"] != Writer.MODEL else "")
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
    for e in section("Teach-back", (EntryKind.TEACHBACK, EntryKind.TEACHBACK_ASSESSMENT)):
        out.append(
            _quote(e["text"])
            if e["kind"] == EntryKind.TEACHBACK
            else f"**{e['verdict'].capitalize()}.** {e['feedback'].strip()}"
        )
        out.append("")
    out.extend(f"_What I missed:_ {r['text'].strip()}\n" for r in reflections.get(None, []))
    for e in section("Findings", (EntryKind.FINDING,)):
        where = f"`{e['path']}:{e.get('line') or '?'}` " if e.get("path") else ""
        out.append(f"- [{e['severity']}] {where}{e['summary'].strip()} _({e['by']})_")
    for e in section("Pinned notes", (EntryKind.NOTE,)):
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

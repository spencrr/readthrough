"""The study engine: the learner's side of the Study tab, and the loop the assistant runs on.

The engine records what the learner did (an answer, a teach-back, a request for a hint), marks the PR
busy, and hands the turn to the configured assistant (``assistants.py``). Whatever the assistant appends
is broadcast to open pages as it happens. The engine itself never writes on the assistant's behalf, so
``by`` in the log is always truthful.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

from readthrough import assistants, study
from readthrough.config import Config

log = logging.getLogger(__name__)

Broadcast = Callable[[str, Any], None]
ACTIONS = ("chat", "quiz", "brief", "answer", "teachback", "hint")


class Busy(RuntimeError):
    pass


class StudyEngine:
    """Owns an asyncio loop on a background thread; the HTTP server calls ``submit`` from its threads."""

    def __init__(
        self,
        cfg: Config,
        broadcast: Broadcast,
        pr_info: Callable[[int], dict[str, Any] | None],
        components: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.broadcast = broadcast
        self.pr_info = pr_info
        self._components = components or (list)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._busy: set[int] = set()
        self._assistant: assistants.Assistant | None = None

    # ---- the assistant

    @property
    def resolved(self) -> assistants.Resolved:
        """Which assistant is in use and whether it can run, for the page and `doctor`."""
        return assistants.resolve(self.cfg.assistant)

    @property
    def assistant(self) -> assistants.Assistant:
        if self._assistant is None:
            self._assistant = assistants.make(self, self.cfg.assistant, self.cfg.assistant_options)
        return self._assistant

    def components(self) -> list[dict[str, Any]]:
        return self._components()

    # ---- lifecycle

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True, name="study-engine").start()
        return self._loop

    def stop(self) -> None:
        if self._loop is None:
            return
        if self._assistant is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._assistant.stop(), self._loop).result(timeout=15)
            except Exception:
                log.exception("Study engine shutdown failed")
        self._loop.call_soon_threadsafe(self._loop.stop)

    def busy(self, number: int) -> bool:
        return number in self._busy

    # ---- public entry point

    def submit(self, number: int, action: str, text: str = "", qid: str | None = None) -> None:
        """Record what the learner did, then start the assistant's turn about it. Returns immediately."""
        resolved = self.resolved
        if not resolved.available:
            raise RuntimeError(resolved.reason)
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        if self.pr_info(number) is None:
            raise ValueError(f"#{number} isn't an open PR in the current data")
        if number in self._busy:
            raise Busy(f"The assistant is still working on #{number}")
        prompt = self._record_and_prompt(number, action, text.strip(), qid)
        self._busy.add(number)
        self.broadcast("study", {"pr": number, "type": "start", "action": action, "assistant": resolved.name})
        turn = assistants.Turn(number, action, text.strip(), qid, prompt)
        asyncio.run_coroutine_threadsafe(self._turn(turn), self._ensure_loop())

    def _record_and_prompt(self, number: int, action: str, text: str, qid: str | None) -> str:
        if action == "answer":
            if not qid or not text:
                raise ValueError("an answer needs the question id and your answer")
            self.append(number, "answer", by="you", qid=qid, text=text)  # study.append tags it with any aids
            return f"My answer to {qid}:\n\n{text}\n\nAssess it with assess_answer."
        if action == "teachback":
            if not text:
                raise ValueError("write your teach-back first")
            self.append(number, "teachback", by="you", text=text)
            return (
                "Here is my teach-back of the whole PR, as if I were briefing a teammate:\n\n"
                f"{text}\n\nAssess it with assess_teachback: name the specific gaps or misunderstandings."
            )
        if action == "quiz":
            self.append(number, "chat", by="you", role="user", text=text or "Quiz me.")
            return (text + "\n\n" if text else "") + "Ask me the next quiz question."
        if action == "brief":
            self.append(number, "chat", by="you", role="user", text="Brief me.", action="brief")
            return (
                "Brief me on this PR before I dig in: what it changes and why, then the things most worth "
                "paying attention to as a reviewer, each with path:line. No quiz question this turn."
            )
        if action == "hint":
            text = f"Hint for {qid}" if qid else "Hint, please."
            self.append(number, "chat", by="you", role="user", text=text, action="hint", qid=qid)
            return f"Give me a hint for {qid}, without the answer." if qid else "Give me a hint, without the answer."
        if not text:
            raise ValueError("type a message first")
        self.append(number, "chat", by="you", role="user", text=text)
        return text

    def append(self, number: int, kind: str, *, by: str, **fields: Any) -> dict[str, Any]:
        """Append to the log, refresh the brief, and tell open pages. Assistants write through here too."""
        entry = study.append(self.cfg, number, kind, by=by, **fields)
        study.refresh_brief(self.cfg, number)
        entries = study.load(self.cfg, number)
        self.broadcast(
            "study",
            {"pr": number, "type": "entry", "entry": public_entry(entry, entries), "summary": study.summarize(entries)},
        )
        return entry

    async def _turn(self, turn: assistants.Turn) -> None:
        try:
            await self.assistant.turn(turn)
            self.broadcast("study", {"pr": turn.number, "type": "done"})
        except Exception as exc:
            log.exception("Study turn for #%d failed", turn.number)
            self.broadcast("study", {"pr": turn.number, "type": "error", "message": str(exc)[:500]})
        finally:
            self._busy.discard(turn.number)


def public_entry(entry: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Hide a question's expected points until the learner has answered it."""
    if entry["kind"] != "question":
        return entry
    answered = any(e["kind"] == "answer" and e.get("qid") == entry["qid"] for e in entries)
    return entry if answered else {k: v for k, v in entry.items() if k != "expected_points"}


def public_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [public_entry(e, entries) for e in entries]

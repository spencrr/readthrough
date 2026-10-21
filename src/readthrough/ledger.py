"""The review ledger: what you did with each PR, pinned to the head SHA you saw."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from readthrough.files import locked, write_atomic

STATUSES = ("understood", "reviewed", "commented", "approved", "changes-requested", "skipped")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(path: Path, ledger: dict[str, Any]) -> None:
    write_atomic(path, json.dumps(ledger, indent=1))


def record(
    path: Path,
    number: int,
    *,
    status: str | None = None,
    note: str | None = None,
    head: str = "",
    session: str | None = None,
) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    # Read, change, write as one step: two browser tabs, or the server and `readthrough ledger` in a terminal,
    # would otherwise each load the file and the slower one's write would drop the other's change.
    with locked(path):
        ledger = load(path)
        entry = ledger.setdefault(str(number), {})
        if status is not None:
            entry.update(status=status, date=dt.datetime.now(dt.UTC).isoformat(), head=head or entry.get("head", ""))
        if note is not None:
            entry["note"] = note
        if session is not None:
            entry["session"] = session
        _save(path, ledger)
    return entry


def clear_status(path: Path, number: int) -> dict[str, Any]:
    """Forget the status for a PR but keep its note and agent session."""
    with locked(path):
        ledger = load(path)
        entry = ledger.get(str(number), {})
        for key in ("status", "date", "head"):
            entry.pop(key, None)
        if entry:
            ledger[str(number)] = entry
        else:
            ledger.pop(str(number), None)
        _save(path, ledger)
    return entry

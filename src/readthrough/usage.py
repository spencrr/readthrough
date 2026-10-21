"""The usage ledger: what each model call cost, in the units the provider actually reports.

Every call a study assistant or the onboarding drafter makes to a model is appended here, one line per
call, keyed by the provider's call id so a replayed event never counts twice. The units are kept apart
and never converted into each other: tokens are tokens; ``multiplier`` is Copilot's premium-request
multiplier (not money); ``nano_aiu`` is Copilot's AI-credit consumption, which says nothing about whether
the call fell inside a plan's allowance; ``usd`` appears only when a price table priced it, and names the
table. A quantity the provider didn't report is absent, never zero.

``billed_to`` says whose account the call went to: ``copilot:<login>`` for GitHub Copilot through a
sign-in, ``copilot:org/<org>`` when an organization is billed, or ``<provider>:<host>`` for a model
provider of your own. ``purpose`` is what the call was for (``study``, ``draft``) and ``ref`` what it was
about (a PR number, or ``onboarding``). The study log doesn't repeat any of this; its entries name the
ledger ids they came from.

The ledger lives in the repo's data directory (``usage.jsonl``), next to the study logs.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from readthrough.config import Config

PURPOSES = ("study", "draft", "review")
_lock = threading.Lock()


def ledger_path(cfg: Config) -> Path:
    return cfg.repo_dir / "usage.jsonl"


def load(cfg: Config) -> list[dict[str, Any]]:
    path = ledger_path(cfg)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue  # a torn final line from a crash
    return entries


def record(
    cfg: Config,
    *,
    call_id: str,
    purpose: str,
    ref: str,
    model: str,
    billed_to: str,
    by: str,
    tokens: dict[str, int] | None = None,
    multiplier: float | None = None,
    nano_aiu: float | None = None,
    usd: float | None = None,
    price_table: str | None = None,
    via: str = "local",
) -> dict[str, Any] | None:
    """Append one model call. Returns the entry, or None when ``call_id`` was already recorded."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(PURPOSES)}")
    if usd is not None and not price_table:
        raise ValueError("a dollar amount must name the price table that produced it")
    entry: dict[str, Any] = {
        "id": call_id,
        "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "purpose": purpose,
        "ref": ref,
        "model": model,
        "billed_to": billed_to,
        "by": by,
        "via": via,
    }
    if tokens:
        entry["tokens"] = {k: int(v) for k, v in tokens.items() if v is not None}
    measured = {"multiplier": multiplier, "nano_aiu": nano_aiu, "usd": usd, "price_table": price_table}
    entry.update({k: v for k, v in measured.items() if v is not None})
    with _lock:
        if any(e.get("id") == call_id for e in load(cfg)):
            return None
        path = ledger_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    return entry


def from_sdk_usage(data: Any) -> dict[str, Any]:
    """The ledger fields an ``assistant.usage`` event from the Copilot SDK carries, in the ledger's names.

    Only what the event reports: a BYOK call has no ``nano_aiu`` and a Copilot call no dollar price, and
    the SDK's ``cost`` is the premium-request multiplier, recorded as such.
    """
    tokens = {
        name: getattr(data, attr, None)
        for name, attr in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("cache_read", "cache_read_tokens"),
            ("cache_write", "cache_write_tokens"),
            ("reasoning", "reasoning_tokens"),
        )
    }
    copilot_usage = getattr(data, "copilot_usage", None)
    byok = bool(getattr(data, "is_byok", False))
    return {
        "call_id": getattr(data, "api_call_id", None) or getattr(data, "provider_call_id", None),
        "model": getattr(data, "model", None) or "?",
        "tokens": {k: v for k, v in tokens.items() if v is not None},
        # The premium-request multiplier is Copilot's unit; a provider of your own has none, and the runtime
        # sends 0 for it, which would read as "free".
        "multiplier": None if byok else getattr(data, "cost", None),
        "nano_aiu": getattr(copilot_usage, "total_nano_aiu", None) if copilot_usage is not None else None,
        "is_byok": byok,
    }


def summarize(entries: list[dict[str, Any]], *, since: dt.datetime | None = None) -> dict[str, Any]:
    """Totals per purpose and per payer, in each unit separately. Calls that reported no tokens are counted
    but add nothing; the summary says how many such calls there were, so a low total isn't read as cheap."""
    picked = [e for e in entries if since is None or dt.datetime.fromisoformat(e["ts"]) >= since]
    tokens: Counter[str] = Counter()
    nano_aiu = 0.0
    usd = 0.0
    priced = unpriced = 0
    for e in picked:
        for k, v in (e.get("tokens") or {}).items():
            tokens[k] += v
        if "nano_aiu" in e:
            nano_aiu += e["nano_aiu"]
        if "usd" in e:
            usd += e["usd"]
            priced += 1
        elif "nano_aiu" not in e:
            unpriced += 1
    return {
        "calls": len(picked),
        "tokens": dict(tokens),
        "nano_aiu": nano_aiu if any("nano_aiu" in e for e in picked) else None,
        "usd": usd if priced else None,
        "unpriced_calls": unpriced,
        "by_purpose": dict(Counter(e["purpose"] for e in picked)),
        "by_payer": dict(Counter(e["billed_to"] for e in picked)),
        "by_model": dict(Counter(e["model"] for e in picked)),
    }


def describe(summary: dict[str, Any]) -> str:
    """One line for `doctor` and the brief, honest about what wasn't measured."""
    if not summary["calls"]:
        return "no model calls recorded"
    t = summary["tokens"]
    parts = [f"{summary['calls']} model calls", f"{t.get('input', 0) + t.get('output', 0):,} tokens"]
    if summary["nano_aiu"] is not None:
        parts.append(f"{summary['nano_aiu'] / 1e9:.2f} AIU")
    if summary["usd"] is not None:
        parts.append(f"${summary['usd']:.2f}")
    if summary["unpriced_calls"]:
        parts.append(f"{summary['unpriced_calls']} calls with no price reported")
    return ", ".join(parts)

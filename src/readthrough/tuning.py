"""Tunable settings: typed, range-checked, documented in one place.

The defaults and their explanations live in ``assets/defaults.toml``, the single source of truth. Your
``config.toml`` overrides them under ``[tuning.<section>]``, and some sections can be overridden per repo.
The dataclasses here only declare each setting's type and allowed range; a test checks they match the file.

Values that define file formats or protocols (CI state names, ledger statuses, the URL scheme, schema
versions) and security limits (see ``limits.py``) are deliberately not tunable.
"""

from __future__ import annotations

import difflib
import tomllib
from dataclasses import dataclass, field, fields, replace
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any, get_type_hints

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


def _r(low: float | None = None, high: float | None = None) -> Any:
    """Declare a field's allowed range."""
    return field(metadata={"range": (low, high)})


def _choice(*options: str) -> Any:
    return field(metadata={"choices": options})


@dataclass(frozen=True)
class ForgeTuning:
    timeout_seconds: float = _r(5, 3600)
    retries: int = _r(0, 10)
    retry_backoff_seconds: float = _r(0, 300)
    retry_max_backoff_seconds: float = _r(0, 3600)
    rate_limit_wait_seconds: float = _r(1, 3600)
    concurrency: int = _r(1, 32)
    pr_page_size: int = _r(1, 100)
    max_files_per_pr: int = _r(100, 3000)
    max_checks_per_pr: int = _r(100, 5000)
    max_review_threads_per_pr: int = _r(100, 5000)
    max_tree_files: int = _r(1000, 1_000_000)
    diff_cache_days: float = _r(0, 3650)


@dataclass(frozen=True)
class GitTuning:
    timeout_seconds: float = _r(10, 7200)


@dataclass(frozen=True)
class ServerTuning:
    watch_seconds: float = _r(0.5, 300)
    keepalive_seconds: float = _r(1, 300)
    start_stagger_seconds: float = _r(0, 3600)
    refresh_jitter: float = _r(0, 0.5)
    retry_after_failure_minutes: list[float] = _r(0.1, 1440)  # noqa: RUF009 - _r declares bounds, not a default


@dataclass(frozen=True)
class PlanningTuning:
    estimate_base_minutes: float = _r(0, 120)
    estimate_minutes_per_source_line: float = _r(0, 10)
    estimate_minutes_per_test_line: float = _r(0, 10)
    estimate_minutes_per_other_line: float = _r(0, 10)
    estimate_minutes_per_extra_layer: float = _r(0, 120)
    estimate_max_minutes: int = _r(5, 1440)
    priority_base: float = _r(-1000, 1000)
    priority_age_cap_days: float = _r(0, 3650)
    priority_review_requested: float = _r(-1000, 1000)
    priority_re_review: float = _r(-1000, 1000)
    priority_quick_minutes: int = _r(0, 1440)
    priority_quick_bonus: float = _r(-1000, 1000)
    priority_short_minutes: int = _r(0, 1440)
    priority_short_bonus: float = _r(-1000, 1000)
    priority_closes_issue: float = _r(-1000, 1000)
    priority_approved: float = _r(-1000, 1000)
    priority_ci_passing: float = _r(-1000, 1000)
    priority_ci_failing_on_main: float = _r(-1000, 1000)
    priority_ci_failing_optional: float = _r(-1000, 1000)
    priority_ci_not_run: float = _r(-1000, 1000)


@dataclass(frozen=True)
class OnboardingTuning:
    min_coverage: float = _r(0, 1)
    drift_coverage: float = _r(0, 1)
    merged_sample: int = _r(10, 100)
    facts_cache_minutes: float = _r(0, 1440)


@dataclass(frozen=True)
class FreshnessTuning:
    stale_live_hours: float = _r(0.1, 720)
    stale_snapshot_hours: float = _r(0.1, 720)
    doctor_build_hours: float = _r(0.1, 720)


@dataclass(frozen=True)
class PayloadTuning:
    description_chars: int = _r(100, 1_000_000)
    description_html_chars: int = _r(100, 2_000_000)
    thread_comment_chars: int = _r(50, 100_000)
    thread_comment_html_chars: int = _r(50, 200_000)
    line_heuristic_code_chars: int = _r(20, 2000)


@dataclass(frozen=True)
class StudyTuning:
    turn_timeout_seconds: float = _r(30, 7200)
    max_tool_output_chars: int = _r(1000, 1_000_000)
    onboarding_turn_timeout_seconds: float = _r(30, 7200)
    onboarding_tool_output_chars: int = _r(1000, 1_000_000)
    onboarding_doc_excerpt_chars: int = _r(500, 200_000)


@dataclass(frozen=True)
class SessionsTuning:
    scan_seconds: float = _r(0.5, 3600)
    first_message_bytes: int = _r(4096, 50_000_000)


@dataclass(frozen=True)
class AuthorsTuning:
    membership_cache_days: float = _r(0, 365)


@dataclass(frozen=True)
class LoggingTuning:
    level: str = _choice("debug", "info", "warning", "error")
    format: str = _choice("text", "json")
    file_max_kb: int = _r(16, 1_000_000)
    file_backups: int = _r(0, 100)


@dataclass(frozen=True)
class Tuning:
    forge: ForgeTuning
    git: GitTuning
    server: ServerTuning
    planning: PlanningTuning
    onboarding: OnboardingTuning
    freshness: FreshnessTuning
    payload: PayloadTuning
    study: StudyTuning
    sessions: SessionsTuning
    authors: AuthorsTuning
    logging: LoggingTuning


# Sections that make sense per repo. The rest apply to the whole process (the server, logging, GitHub limits).
REPO_SECTIONS = ("planning", "onboarding", "freshness", "payload", "study", "authors")


class TuningError(ValueError):
    pass


@cache
def defaults_text() -> str:
    return files("readthrough").joinpath("assets/defaults.toml").read_text(encoding="utf-8")


@cache
def _defaults_raw() -> dict[str, dict[str, Any]]:
    return tomllib.loads(defaults_text())


def _section_types() -> dict[str, type[DataclassInstance]]:
    return {f.name: get_type_hints(Tuning)[f.name] for f in fields(Tuning)}


def _suggest(name: str, options: Any) -> str:
    close = difflib.get_close_matches(name, list(options), n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


def _coerce(where: str, f: Any, hint: Any, value: Any) -> Any:
    low, high = f.metadata.get("range", (None, None))
    choices = f.metadata.get("choices")
    origin = getattr(hint, "__origin__", None)
    if origin is list:
        if not isinstance(value, list) or not value:
            raise TuningError(f"{where} must be a non-empty list of numbers")
        return [_coerce(f"{where}[{i}]", f, float, v) for i, v in enumerate(value)]
    if hint is bool or isinstance(value, bool):
        raise TuningError(f"{where} must be a number, not {value!r}")
    if hint is int:
        if not isinstance(value, int):
            raise TuningError(f"{where} must be a whole number, not {value!r}")
    elif hint is float:
        if not isinstance(value, int | float):
            raise TuningError(f"{where} must be a number, not {value!r}")
        value = float(value)
    elif hint is str:
        if not isinstance(value, str):
            raise TuningError(f"{where} must be text, not {value!r}")
        value = value.lower()
        if choices and value not in choices:
            raise TuningError(f"{where} must be one of {', '.join(choices)}, not {value!r}")
        return value
    if (low is not None and value < low) or (high is not None and value > high):
        raise TuningError(f"{where} must be between {low:g} and {high:g}, not {value!r}")
    return value


def _build_section(cls: type[DataclassInstance], raw: dict[str, Any], where: str) -> Any:
    hints = get_type_hints(cls)
    known = {f.name: f for f in fields(cls)}
    for key in raw:
        if key not in known:
            raise TuningError(f"Unknown setting {where}.{key}{_suggest(key, known)}")
    return cls(**{k: _coerce(f"{where}.{k}", f, hints[k], raw[k]) for k, f in known.items()})


def _check_overrides(raw: Any, where: str, allowed: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TuningError(f"{where} must be a table")
    for section, values in raw.items():
        if section not in allowed:
            hint = _suggest(section, allowed)
            extra = "" if section not in _section_types() else " (that section can't be set per repo)"
            raise TuningError(f"Unknown section {where}.{section}{hint}{extra}")
        if not isinstance(values, dict):
            raise TuningError(f"{where}.{section} must be a table")
    return raw


def load_tuning(overrides: Any = None, *, repo_overrides: Any = None, where: str = "tuning") -> Tuning:
    """Defaults, then the global ``[tuning]`` table, then a repo's own ``tuning`` table."""
    sections = _section_types()
    glob = _check_overrides(overrides, where, tuple(sections))
    per_repo = _check_overrides(repo_overrides, f"{where} (repo)", REPO_SECTIONS)
    built = {}
    for name, cls in sections.items():
        raw = {**_defaults_raw()[name], **glob.get(name, {}), **per_repo.get(name, {})}
        built[name] = _build_section(cls, raw, f"{where}.{name}")
    return Tuning(**built)


@cache
def default_tuning() -> Tuning:
    return load_tuning()


def with_section(tuning: Tuning, **sections: Any) -> Tuning:
    """A copy with whole sections replaced (for tests)."""
    return replace(tuning, **sections)


def changed_from_defaults(tuning: Tuning) -> dict[str, dict[str, Any]]:
    """Settings that differ from the defaults, for `readthrough config`."""
    base = default_tuning()
    out: dict[str, dict[str, Any]] = {}
    for f in fields(Tuning):
        mine, theirs = getattr(tuning, f.name), getattr(base, f.name)
        diff = {g.name: getattr(mine, g.name) for g in fields(mine) if getattr(mine, g.name) != getattr(theirs, g.name)}
        if diff:
            out[f.name] = diff
    return out

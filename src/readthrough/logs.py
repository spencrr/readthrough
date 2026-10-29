"""Logging that stays readable when several repos build at once.

Every record carries the repo and a short build id (from context variables, so they follow the work into
thread pools via ``pool_map``). The server and readthrough.log use one timestamped format; interactive commands
print plain messages. ``format = "json"`` writes one JSON object per line for log tools.

Expected failures (a network blip GitHub recovers from) are logged as one line. Unexpected ones get a
traceback. Each failure gets a short error id that also appears in the dashboard, so a screenshot leads
straight to the log lines.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import secrets
import sys
import time
from collections.abc import Callable, Generator, Iterable
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TypeVar

from readthrough.tuning import LoggingTuning

repo_var: contextvars.ContextVar[str] = contextvars.ContextVar("readthrough_repo", default="")
build_var: contextvars.ContextVar[str] = contextvars.ContextVar("readthrough_build", default="")

T = TypeVar("T")
R = TypeVar("R")
ROOT = "readthrough"
TEXT_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(ctx)s%(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def new_id() -> str:
    return secrets.token_hex(2)


class ContextFilter(logging.Filter):
    """Adds the repo and build context to every record, and strips every secret this process has seen from it.

    Redaction here, on the way to a handler, means no call site has to remember it: a token a program echoed into
    an error message never reaches readthrough.log, the console, or a ``doctor --bundle``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        repo, build = repo_var.get(), build_var.get()
        record.repo, record.build = repo, build
        record.ctx = f"[{repo}{' ' + build if build else ''}] " if repo else ""
        from readthrough.process import redact  # process logs through this module, so the import waits

        record.msg, record.args = redact(record.getMessage()), ()
        if record.exc_info and record.exc_text is None and record.exc_info[1] is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, DATE_FORMAT) + f".{int(record.msecs):03d}",
            "level": record.levelname.lower(),
            "logger": record.name,
            "repo": getattr(record, "repo", "") or None,
            "build": getattr(record, "build", "") or None,
            "msg": record.getMessage(),
        }
        for key in ("error_id", "step", "duration_s", "attempt"):
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in out.items() if v is not None}, ensure_ascii=False)


def _formatter(fmt: str, *, plain: bool = False) -> logging.Formatter:
    if fmt == "json":
        return JsonFormatter()
    if plain:
        return logging.Formatter("%(ctx)s%(message)s")
    return logging.Formatter(TEXT_FORMAT, DATE_FORMAT)


_configured = False


def setup(log_path: Path, tuning: LoggingTuning, *, verbose: bool = False, console: str = "plain") -> None:
    """Configure the ``readthrough`` logger once. ``console`` is "plain" (commands), "full" (the server), or "none"."""
    global _configured  # noqa: PLW0603 - the one process-wide "already configured" flag
    if _configured:
        return
    logger = logging.getLogger(ROOT)
    logger.setLevel(logging.DEBUG if verbose else getattr(logging, tuning.level.upper()))
    logger.propagate = False
    context = ContextFilter()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=tuning.file_max_kb * 1024, backupCount=tuning.file_backups, encoding="utf-8"
    )
    file_handler.setFormatter(_formatter(tuning.format))
    file_handler.addFilter(context)
    logger.addHandler(file_handler)
    if console != "none" and sys.stderr is not None:  # stderr is None under pythonw (scheduled task, URL handler)
        handler = logging.StreamHandler()
        handler.setFormatter(_formatter(tuning.format, plain=console == "plain"))
        handler.addFilter(context)
        logger.addHandler(handler)
    _configured = True


def reset() -> None:
    """Forget the configuration and close its handlers, so the next ``setup`` starts over (tests)."""
    global _configured  # noqa: PLW0603 - see setup
    logger = logging.getLogger(ROOT)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
    _configured = False


@contextlib.contextmanager
def context(repo: str = "", build: str = "") -> Generator[None]:
    """Tag every log line inside the block with this repo and build id."""
    tokens = [repo_var.set(repo)] if repo else []
    if build:
        tokens.append(build_var.set(build))
    try:
        yield
    finally:
        for token in reversed(tokens):
            token.var.reset(token)


def pool_map(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """``ThreadPoolExecutor.map`` that carries the caller's log context into the worker threads."""
    items = list(items)
    if not items:
        return []
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max(1, min(workers, len(items)))) as pool:
        return list(pool.map(lambda item: ctx.copy().run(fn, item), items))


class Steps:
    """Times the steps of a build, so a failure says which step failed and a success says where time went."""

    def __init__(self, log: logging.Logger) -> None:
        self.log = log
        self.current = "starting"
        self.timings: list[tuple[str, float]] = []

    @contextlib.contextmanager
    def step(self, name: str) -> Generator[None]:
        self.current = name
        started = time.monotonic()
        self.log.debug("step %s: started", name)
        yield
        took = time.monotonic() - started
        self.timings.append((name, took))
        self.log.debug("step %s: done in %.1fs", name, took, extra={"step": name, "duration_s": round(took, 2)})

    def summary(self) -> str:
        return ", ".join(f"{name} {took:.1f}s" for name, took in self.timings)

"""Model runtimes: the engines a study assistant runs on, behind one small protocol.

The study assistant's behaviour (the Socratic prompt, the eight tools, the evidence it writes) is the same
whoever the model is. What differs is mechanical: how a conversation is opened, how a prompt is sent, how
text, tool calls, and usage come back, and how a credential is found. That is a ``Runtime``. Each one is a
plugin in ``RUNTIMES``, with a dataclass of options the config loader validates:

* ``copilot`` runs on the Copilot SDK (the Copilot CLI as an engine). GitHub Copilot through its sign-in,
  or a model provider of your own, with no GitHub account involved.
* ``anthropic`` talks to the Messages API over ``urllib``, with the tool loop here. Needs only an API key.
* ``openai`` talks to the Responses API the same way (OpenAI, or Azure OpenAI and Foundry with Entra).

A runtime never writes to the study log and never sees the harness; it hands the assistant ``Event``s and
calls the tool handlers the assistant gave it. Everything that must be true of the log (who wrote what, which
model reached a verdict, what each call cost) is the assistant's and the ledger's business.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from readthrough.registry import Registry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    """One tool the assistant offers, in readthrough's own terms. Runtimes translate it to their wire format.

    ``schema`` is a JSON schema for the arguments (an object with properties). ``terminal`` tools end the
    assistant's turn once called (asking a quiz question); a runtime stops the loop after one.
    """

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Coroutine[Any, Any, str]]
    terminal: bool = False


@dataclass(frozen=True)
class Event:
    """What a runtime reports while a prompt runs. ``kind`` is one of:

    ``text``: ``text`` holds a delta of the reply.
    ``tool``: ``name`` holds a tool the model is calling (reported before the handler runs).
    ``usage``: ``usage`` holds one model call's accounting (see ``Usage``).
    ``done``: the turn ended; ``text`` holds the whole reply.
    ``error``: ``text`` holds why the turn failed.
    """

    kind: str
    text: str = ""
    name: str = ""
    usage: Usage | None = None


@dataclass(frozen=True)
class Usage:
    """One model call, in the provider's units. A quantity the provider didn't report is None, never 0."""

    call_id: str | None
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    multiplier: float | None = None  # Copilot's premium-request multiplier
    nano_aiu: float | None = None  # Copilot's AI-credit consumption
    billed_to: str = ""  # who paid: "copilot", "anthropic:api.anthropic.com", ...

    def tokens(self) -> dict[str, int]:
        pairs = {
            "input": self.input_tokens, "output": self.output_tokens, "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens, "reasoning": self.reasoning_tokens,
        }  # fmt: skip
        return {k: v for k, v in pairs.items() if v is not None}


@dataclass
class Conversation:
    """What a runtime needs to open a conversation about one PR."""

    key: str  # stable per PR, so a runtime that persists conversations can resume this one
    system: str
    tools: list[ToolSpec]
    working_directory: str
    model: str | None = None  # the repo's own choice; None means the runtime's configured default
    resume: bool = True  # whether an existing conversation under ``key`` should be continued


class Session(Protocol):
    """An open conversation. ``send`` yields events until the turn is done or fails."""

    @property
    def fresh(self) -> bool: ...  # True when this is a new conversation (the assistant then sends context)

    def send(self, prompt: str) -> AsyncIterator[Event]: ...
    async def close(self) -> None: ...


class Runtime(Protocol):
    name: str

    async def open(self, conversation: Conversation) -> Session: ...
    async def stop(self) -> None: ...
    def describe(self) -> str: ...  # for doctor: engine and payer, never a secret


RuntimeFactory = Callable[[Any], Runtime]
RUNTIMES: Registry[RuntimeFactory] = Registry("model runtime")
AUTO_ORDER = ("copilot", "anthropic", "openai")


def available(name: str) -> tuple[bool, str]:
    """Whether a runtime can run here, and why not."""
    if name == "copilot":
        try:
            import copilot  # noqa: F401
        except ImportError:
            return False, "the copilot runtime needs the Copilot SDK: install readthrough with the [study] extra"
        return True, ""
    if name in ("anthropic", "openai"):
        plugin = RUNTIMES.get(name)
        env = plugin.options.api_key_env  # type: ignore[attr-defined] - both option classes declare it
        if not os.environ.get(env, "").strip() and not getattr(plugin.options, "entra", False):
            return False, f"the {name} runtime needs an API key in {env}"
        return True, ""
    RUNTIMES.get(name)
    return True, ""


def resolve(name: str, options: dict[str, Any] | None = None) -> tuple[str, bool, str]:
    """Which runtime ``name`` means: itself, or for ``auto`` the first that can run. Returns (name, ok, reason)."""
    if name != "auto":
        ok, why = _available_with(name, options)
        return name, ok, why
    for candidate in AUTO_ORDER:
        ok, _ = _available_with(candidate, options)
        if ok:
            return candidate, True, ""
    return AUTO_ORDER[0], False, "no model runtime is available: install the [study] extra, or set an API key"


def _available_with(name: str, options: dict[str, Any] | None) -> tuple[bool, str]:
    opts = (options or {}).get(name)
    if opts is not None and hasattr(opts, "can_run"):
        return opts.can_run()
    return available(name)


def make(name: str, options: dict[str, Any] | None = None) -> Runtime:
    """Build the configured runtime. ``options`` maps runtime names to validated options objects."""
    resolved, ok, why = resolve(name, options)
    if not ok:
        raise RuntimeError(why)
    factory = RUNTIMES.get(resolved)
    opts = (options or {}).get(resolved) or RUNTIMES.defaults_for(resolved)
    return factory.impl(opts)


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """The JSON schema a wire format wants: an object with the tool's properties, nothing extra."""
    schema = dict(spec.schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})  # 529 is Anthropic's "overloaded"
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2.0
RETRY_MAX_WAIT_SECONDS = 30.0
JITTER_MAX = 0.25
jitter = random.Random()  # noqa: S311 - timing jitter, not security; its own generator so tests can seed it


def retry_wait(attempt: int, retry_after: str | None, rng: random.Random | None = None) -> float:
    """Seconds before retry ``attempt`` (from 1): what the provider asked for in ``Retry-After`` when it did,
    else exponential with jitter; never more than ``RETRY_MAX_WAIT_SECONDS``, so a turn can't stall on a bad
    header."""
    if retry_after:
        try:
            return min(RETRY_MAX_WAIT_SECONDS, max(0.0, float(retry_after)))
        except ValueError:
            pass  # an HTTP-date: fall back to backoff rather than parse clocks
    base = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
    return min(RETRY_MAX_WAIT_SECONDS, base * (1 + (rng or jitter).uniform(0, JITTER_MAX)))


def with_retries(
    what: str, call: Callable[[], Any], *, sleep: Callable[[float], None] | None = None, attempts: int = RETRY_ATTEMPTS
) -> Any:
    """Run one HTTP call, retrying the failures a provider says are temporary: rate limits, overload, gateway
    errors, and a dropped connection. Anything else (a bad key, a bad request) is raised at once; so is the
    last attempt, as the ``Retryable`` the caller turns into a message."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Retryable as exc:
            if attempt == attempts:
                raise
            wait = retry_wait(attempt, exc.retry_after)
            log.info("%s: %s; retrying in %.1fs (%d/%d)", what, exc, wait, attempt, attempts - 1)
            (sleep or time.sleep)(wait)
    raise AssertionError("unreachable")


class Retryable(RuntimeError):
    """A provider answered in a way that says "try again": the status and any ``Retry-After`` it sent."""

    def __init__(self, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments as a dict, whether the provider sent an object or a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _register_builtin() -> None:
    # Imported here so each runtime module can import this one.
    from readthrough.runtimes import anthropic, copilot, openai  # noqa: F401


_register_builtin()

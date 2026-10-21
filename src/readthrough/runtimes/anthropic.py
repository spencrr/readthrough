"""Anthropic's Messages API as a runtime: Claude, with nothing installed but an API key.

The tool loop lives here. A turn is a sequence of calls: the model answers with text and ``tool_use``
blocks; each tool runs and its result goes back as a ``tool_result``; this repeats until the model stops,
a terminal tool (asking a quiz question) is called, or the call cap is hit. Responses stream, so text
reaches the page as it is written. The conversation is kept in memory for the process's lifetime (the
assistant resends the brief on a fresh one, and the study log is the record either way).

Prompt caching: the system message and the tools are marked cacheable, since they repeat on every call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from readthrough.process import Secret, redact, remember
from readthrough.runtimes import (
    RETRY_ATTEMPTS,
    RETRY_STATUSES,
    RUNTIMES,
    Conversation,
    Event,
    Retryable,
    Usage,
    parse_arguments,
    tool_schema,
    with_retries,
)
from readthrough.tuning import default_tuning

log = logging.getLogger(__name__)

API_VERSION = "2023-06-01"


@dataclass(frozen=True)
class AnthropicOptions:
    model: str = "claude-sonnet-5-5"
    base_url: str = "https://api.anthropic.com"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 4096
    max_calls_per_turn: int = 12  # a turn is one prompt; each tool round is one more model call

    def can_run(self) -> tuple[bool, str]:
        if not os.environ.get(self.api_key_env, "").strip():
            return False, f"the anthropic runtime needs an API key in {self.api_key_env}"
        return True, ""

    def key(self) -> Secret:
        value = os.environ.get(self.api_key_env, "").strip()
        if not value:
            raise RuntimeError(f"The anthropic runtime needs an API key in {self.api_key_env} (never in config.toml).")
        return remember(Secret(value))

    def billed_to(self) -> str:
        return f"anthropic:{urlsplit(self.base_url).hostname or self.base_url}"


class AnthropicRuntime:
    name = "anthropic"

    def __init__(self, options: Any) -> None:
        self.options: AnthropicOptions = options if isinstance(options, AnthropicOptions) else AnthropicOptions()
        self._conversations: dict[str, AnthropicSession] = {}

    @property
    def model(self) -> str | None:
        return self.options.model

    def describe(self) -> str:
        return f"Anthropic Messages API at {self.options.base_url} ({self.options.model})"

    async def open(self, conversation: Conversation) -> AnthropicSession:
        if conversation.resume and conversation.key in self._conversations:
            return self._conversations[conversation.key]
        session = AnthropicSession(self.options, conversation)
        self._conversations[conversation.key] = session
        return session

    async def stop(self) -> None:
        self._conversations.clear()


class AnthropicSession:
    def __init__(self, options: AnthropicOptions, conversation: Conversation) -> None:
        self.options = options
        self.conversation = conversation
        self.model = conversation.model or options.model
        self.messages: list[dict[str, Any]] = []  # the transcript, in the API's shape
        self._tools = {t.name: t for t in conversation.tools}
        self._fresh = True

    @property
    def fresh(self) -> bool:
        return self._fresh

    async def close(self) -> None:
        return None

    def _request_body(self) -> dict[str, Any]:
        tools = [
            {"name": t.name, "description": t.description, "input_schema": tool_schema(t)}
            for t in self.conversation.tools
        ]
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return {
            "model": self.model,
            "max_tokens": self.options.max_tokens,
            "system": [{"type": "text", "text": self.conversation.system, "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": self.messages,
            "stream": True,
        }

    async def send(self, prompt: str) -> AsyncIterator[Event]:
        self._fresh = False
        self.messages.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
        reply: list[str] = []
        for _ in range(self.options.max_calls_per_turn):
            try:
                result = await asyncio.to_thread(self._call)
            except RuntimeError as exc:
                yield Event("error", text=str(exc))
                return
            text, tool_uses, used = result
            if text:
                reply.append(text)
                yield Event("text", text=text)
            yield Event("usage", usage=used)
            content: list[dict[str, Any]] = []
            if text:
                content.append({"type": "text", "text": text})
            content += [{"type": "tool_use", "id": u["id"], "name": u["name"], "input": u["input"]} for u in tool_uses]
            self.messages.append({"role": "assistant", "content": content})
            if not tool_uses:
                yield Event("done", text="".join(reply))
                return
            results: list[dict[str, Any]] = []
            terminal = False
            for use in tool_uses:
                spec = self._tools.get(use["name"])
                yield Event("tool", name=use["name"])
                if spec is None:
                    output = f"Unknown tool {use['name']!r}."
                else:
                    try:
                        output = await spec.handler(use["input"])
                    except Exception as exc:  # noqa: BLE001 - the model gets the error as a result, like any tool runner
                        output = f"Tool failed: {exc}"
                    terminal = terminal or spec.terminal
                results.append({"type": "tool_result", "tool_use_id": use["id"], "content": output})
            self.messages.append({"role": "user", "content": results})
            if terminal:
                yield Event("done", text="".join(reply))
                return
            if reply and not reply[-1].endswith("\n"):
                reply.append("\n\n")
        yield Event("error", text=f"the model made more than {self.options.max_calls_per_turn} calls in one turn")

    def _call(self) -> tuple[str, list[dict[str, Any]], Usage]:
        """One streamed Messages call. Returns (text, tool uses, usage)."""
        body = json.dumps(self._request_body()).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - an https API URL from config
            self.options.base_url.rstrip("/") + "/v1/messages",
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "anthropic-version": API_VERSION,
                "accept": "text/event-stream",
                "user-agent": "readthrough",
            },
        )
        req.add_unredirected_header("x-api-key", self.options.key().reveal())
        timeout = default_tuning().study.turn_timeout_seconds

        def once() -> tuple[str, list[dict[str, Any]], Usage]:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    request_id = resp.headers.get("request-id")
                    return self._read_stream(resp, request_id)
            except urllib.error.HTTPError as exc:
                text = redact(exc.read().decode("utf-8", "replace"))
                message = f"Anthropic refused the call: {_error_message(text) or f'HTTP {exc.code}'}"
                if exc.code in RETRY_STATUSES:
                    raise Retryable(message, retry_after=exc.headers.get("retry-after")) from exc
                raise RuntimeError(message) from exc
            except urllib.error.URLError as exc:
                raise Retryable(f"couldn't reach {self.options.base_url}: {exc.reason}") from exc

        try:
            return with_retries("Anthropic", once)
        except Retryable as exc:
            raise RuntimeError(f"{exc} (gave up after {RETRY_ATTEMPTS} attempts)") from exc

    def _read_stream(self, resp: Any, request_id: str | None) -> tuple[str, list[dict[str, Any]], Usage]:
        text: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}  # index -> tool_use block being assembled
        partial: dict[int, list[str]] = {}
        model = self.model
        message_id = None
        usage: dict[str, Any] = {}
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            kind = event.get("type")
            if kind == "message_start":
                message = event.get("message") or {}
                model = message.get("model") or model
                message_id = message.get("id")
                usage.update(message.get("usage") or {})
            elif kind == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "tool_use":
                    blocks[event["index"]] = {"id": block.get("id"), "name": block.get("name"), "input": {}}
                    partial[event["index"]] = []
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text.append(delta.get("text") or "")
                elif delta.get("type") == "input_json_delta":
                    partial.setdefault(event["index"], []).append(delta.get("partial_json") or "")
            elif kind == "message_delta":
                usage.update(event.get("usage") or {})  # output_tokens here is cumulative
            elif kind == "error":
                error = event.get("error") or {}
                raise RuntimeError(
                    f"Anthropic reported an error mid-stream: {error.get('message') or error.get('type')}"
                )
        tool_uses = []
        for index, block in blocks.items():
            block["input"] = parse_arguments("".join(partial.get(index) or []))
            tool_uses.append(block)
        used = Usage(
            call_id=request_id or message_id,
            model=model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_write_tokens=usage.get("cache_creation_input_tokens"),
            billed_to=self.options.billed_to(),
        )
        return "".join(text), tool_uses, used


def _error_message(text: str) -> str | None:
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()[:200] or None
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return f"{error.get('type') or 'error'}: {error.get('message') or ''}".strip(": ")
    return None


async def asyncio_to_thread(fn: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(fn)


RUNTIMES.add("anthropic", "Claude through Anthropic's Messages API; needs only an API key", options=AnthropicOptions)(
    AnthropicRuntime
)

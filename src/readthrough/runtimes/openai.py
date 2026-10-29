"""OpenAI's Responses API as a runtime: OpenAI itself, or Azure OpenAI / Foundry (with a key or an Entra sign-in).

The same wire format serves three hosts, so one runtime does. The conversation is continued through
``previous_response_id``, which the API keeps server-side (``store`` on), so each call carries only what is
new. Tool calls come back as ``function_call`` items; their outputs go back as ``function_call_output``
items; the loop here repeats until the model stops, a terminal tool is called, or the call cap is hit.

On Azure, ``model`` is the deployment name, which may differ from the model's; an Entra token is for the
``https://ai.azure.com/.default`` audience and comes from the signed-in ``az`` when ``entra`` is set.
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

from readthrough.process import Secret, quiet_output, redact, remember
from readthrough.runtimes import (
    RETRY_ATTEMPTS,
    RETRY_STATUSES,
    RUNTIMES,
    Conversation,
    Event,
    Retryable,
    Usage,
    check_arguments,
    parse_arguments,
    tool_schema,
    with_retries,
)
from readthrough.tuning import default_tuning

log = logging.getLogger(__name__)

AZURE_AI_AUDIENCE = "https://ai.azure.com"


@dataclass(frozen=True)
class OpenAIOptions:
    model: str = "gpt-5.5"  # on Azure, the deployment name
    base_url: str = "https://api.openai.com/v1"  # or https://<resource>.openai.azure.com/openai/v1
    api_key_env: str = "OPENAI_API_KEY"
    # Azure only: an Entra token from `az` instead of a key. Many Azure OpenAI resources have key auth disabled.
    entra: bool = False
    max_output_tokens: int = 4096
    max_calls_per_turn: int = 12

    @property
    def azure(self) -> bool:
        host = urlsplit(self.base_url).hostname or ""
        return host.endswith((".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com"))

    def __post_init__(self) -> None:
        if self.entra and not self.azure:
            raise ValueError("entra sign-in applies to Azure endpoints only")

    def can_run(self) -> tuple[bool, str]:
        if self.entra:
            return True, ""
        if not os.environ.get(self.api_key_env, "").strip():
            return False, f"the openai runtime needs an API key in {self.api_key_env}"
        return True, ""

    def key(self) -> Secret:
        value = os.environ.get(self.api_key_env, "").strip()
        if not value:
            raise RuntimeError(f"The openai runtime needs an API key in {self.api_key_env} (never in config.toml).")
        return remember(Secret(value))

    def billed_to(self) -> str:
        return f"{'azure' if self.azure else 'openai'}:{urlsplit(self.base_url).hostname or self.base_url}"


def entra_token() -> Secret:
    """A bearer token for the Azure AI data plane from a signed-in ``az``. Asked for per call; az caches it."""
    out, why = quiet_output(
        ["az", "account", "get-access-token", "--resource", AZURE_AI_AUDIENCE, "--query", "accessToken", "-o", "tsv"],
        with_failure=True,
    )
    if not out or not out.strip():
        first = why.splitlines()[0][:160] if why else "not signed in"
        raise RuntimeError(f"az has no token for Azure AI ({first}); run az login")
    return remember(Secret(out.strip()))


class OpenAIRuntime:
    name = "openai"

    def __init__(self, options: Any) -> None:
        self.options: OpenAIOptions = options if isinstance(options, OpenAIOptions) else OpenAIOptions()
        self._conversations: dict[str, OpenAISession] = {}

    @property
    def model(self) -> str | None:
        return self.options.model

    def describe(self) -> str:
        o = self.options
        where = "Azure OpenAI" if o.azure else "OpenAI"
        auth = ", Entra sign-in" if o.entra else ""
        return f"{where} Responses API at {o.base_url} ({o.model}{auth})"

    async def open(self, conversation: Conversation) -> OpenAISession:
        if conversation.resume and conversation.key in self._conversations:
            return self._conversations[conversation.key]
        session = OpenAISession(self.options, conversation)
        self._conversations[conversation.key] = session
        return session

    async def stop(self) -> None:
        self._conversations.clear()


class OpenAISession:
    def __init__(self, options: OpenAIOptions, conversation: Conversation) -> None:
        self.options = options
        self.conversation = conversation
        self.model = conversation.model or options.model
        self.previous_response_id: str | None = None
        self._tools = {t.name: t for t in conversation.tools}
        self._fresh = True

    @property
    def fresh(self) -> bool:
        return self._fresh

    async def close(self) -> None:
        return None

    def _request_body(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "instructions": self.conversation.system,
            "input": items,
            "tools": [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": tool_schema(t),
                    "strict": False,
                }
                for t in self.conversation.tools
            ],
            "max_output_tokens": self.options.max_output_tokens,
            "store": True,
            "stream": True,
        }
        if self.previous_response_id:
            body["previous_response_id"] = self.previous_response_id
        return body

    async def send(self, prompt: str) -> AsyncIterator[Event]:
        self._fresh = False
        items: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}]
        reply: list[str] = []
        for _ in range(self.options.max_calls_per_turn):
            try:
                text, calls, used, response_id = await asyncio.to_thread(self._call, items)
            except RuntimeError as exc:
                yield Event("error", text=str(exc))
                return
            self.previous_response_id = response_id or self.previous_response_id
            if text:
                reply.append(text)
                yield Event("text", text=text)
            yield Event("usage", usage=used)
            if not calls:
                yield Event("done", text="".join(reply))
                return
            items = []
            terminal = False
            for call in calls:
                spec = self._tools.get(call["name"])
                yield Event("tool", name=call["name"])
                if spec is None:
                    output = f"Unknown tool {call['name']!r}."
                else:
                    try:
                        output = await spec.handler(check_arguments(spec, parse_arguments(call["arguments"])))
                    except Exception as exc:  # noqa: BLE001 - the model gets the error as a result, like any tool runner
                        output = f"Tool failed: {exc}"
                    terminal = terminal or spec.terminal
                items.append({"type": "function_call_output", "call_id": call["call_id"], "output": output})
            if terminal:
                # The outputs still have to reach the conversation, or the next turn would start mid-call.
                try:
                    *_, settled_id = await asyncio.to_thread(self._call, items, settle_only=True)
                    self.previous_response_id = settled_id or self.previous_response_id
                except RuntimeError as exc:
                    log.warning("Couldn't settle the tool outputs after a terminal tool: %s", exc)
                yield Event("done", text="".join(reply))
                return
            if reply and not reply[-1].endswith("\n"):
                reply.append("\n\n")
        yield Event("error", text=f"the model made more than {self.options.max_calls_per_turn} calls in one turn")

    def _call(
        self, items: list[dict[str, Any]], *, settle_only: bool = False
    ) -> tuple[str, list[dict[str, Any]], Usage, str | None]:
        """One streamed Responses call. Returns (text, function calls, usage, response id)."""
        body = self._request_body(items)
        if settle_only:
            body["max_output_tokens"] = 16
            body["tool_choice"] = "none"
        req = urllib.request.Request(  # noqa: S310 - an https API URL from config
            self.options.base_url.rstrip("/") + "/responses",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"content-type": "application/json", "accept": "text/event-stream", "user-agent": "readthrough"},
        )
        if self.options.entra:
            req.add_unredirected_header("Authorization", f"Bearer {entra_token().reveal()}")
        elif self.options.azure:
            req.add_unredirected_header("api-key", self.options.key().reveal())
        else:
            req.add_unredirected_header("Authorization", f"Bearer {self.options.key().reveal()}")
        timeout = default_tuning().study.turn_timeout_seconds

        def once() -> tuple[str, list[dict[str, Any]], Usage, str | None]:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    return self._read_stream(resp)
            except urllib.error.HTTPError as exc:
                text = redact(exc.read().decode("utf-8", "replace"))
                message = f"the model endpoint refused the call: {_error_message(text) or f'HTTP {exc.code}'}"
                if exc.code in RETRY_STATUSES:
                    raise Retryable(message, retry_after=exc.headers.get("retry-after")) from exc
                raise RuntimeError(message) from exc
            except urllib.error.URLError as exc:
                raise Retryable(f"couldn't reach {self.options.base_url}: {exc.reason}") from exc

        try:
            return with_retries("the model endpoint", once)
        except Retryable as exc:
            raise RuntimeError(f"{exc} (gave up after {RETRY_ATTEMPTS} attempts)") from exc

    def _read_stream(self, resp: Any) -> tuple[str, list[dict[str, Any]], Usage, str | None]:
        text: list[str] = []
        calls: dict[str, dict[str, Any]] = {}  # item id -> function call being assembled
        arguments: dict[str, list[str]] = {}
        final: dict[str, Any] | None = None
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            kind = event.get("type")
            if kind == "response.output_text.delta":
                text.append(event.get("delta") or "")
            elif kind == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    calls[item["id"]] = {"call_id": item.get("call_id"), "name": item.get("name"), "arguments": ""}
                    arguments[item["id"]] = [item.get("arguments") or ""]
            elif kind == "response.function_call_arguments.delta":
                arguments.setdefault(event.get("item_id", ""), []).append(event.get("delta") or "")
            elif kind == "response.completed":
                final = event.get("response") or {}
            elif kind in ("response.failed", "error"):
                error = (event.get("response") or {}).get("error") or event
                raise RuntimeError(
                    f"the model reported an error mid-stream: {error.get('message') or error.get('code')}"
                )
        for item_id, call in calls.items():
            call["arguments"] = "".join(arguments.get(item_id) or [])
        usage = (final or {}).get("usage") or {}
        used = Usage(
            call_id=(final or {}).get("id"),
            model=(final or {}).get("model") or self.model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens"),
            cache_write_tokens=(usage.get("input_tokens_details") or {}).get("cache_write_tokens"),
            reasoning_tokens=(usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
            billed_to=self.options.billed_to(),
        )
        return "".join(text), list(calls.values()), used, (final or {}).get("id")


def _error_message(text: str) -> str | None:
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()[:200] or None
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return f"{error.get('code') or error.get('type') or 'error'}: {error.get('message') or ''}".strip(": ")
    return None


RUNTIMES.add(
    "openai",
    "OpenAI's Responses API: OpenAI, or Azure OpenAI / Foundry with a key or an Entra sign-in",
    options=OpenAIOptions,
)(OpenAIRuntime)

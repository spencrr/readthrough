"""The Copilot SDK as a runtime: GitHub Copilot through its sign-in, or a model provider of your own.

The SDK drives the Copilot CLI as an engine. By default it talks to GitHub Copilot as whoever the CLI is
signed in as; with ``provider`` set it talks to that provider instead (the SDK's "bring your own key"),
and the client is built so it never falls back to a GitHub login. Conversations persist in the CLI's
session store, so a PR's study conversation resumes across server restarts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from readthrough.process import quiet_output
from readthrough.runtimes import RUNTIMES, Conversation, Event, ToolSpec, Usage
from readthrough.tuning import default_tuning

log = logging.getLogger(__name__)

PROVIDER_TYPES = ("openai", "azure", "anthropic")
COGNITIVE_SERVICES = "https://cognitiveservices.azure.com"


@dataclass(frozen=True)
class CopilotOptions:
    """Where the model comes from: GitHub Copilot by default, else a provider of your own (no GitHub sign-in).
    The key is read from ``api_key_env``, never from the config file."""

    provider: str | None = None  # openai | azure | anthropic; unset means GitHub Copilot
    base_url: str | None = None  # the provider's endpoint, e.g. https://api.anthropic.com or an Azure resource
    model: str | None = None  # the provider's model name; a repo's `study_model` still wins when set
    api_key_env: str = "READTHROUGH_MODEL_API_KEY"
    # azure only: sign in with `az` (an Entra token for Cognitive Services) instead of a key. Many Azure
    # OpenAI resources have key auth disabled by policy, so this is often the only way in.
    entra: bool = False
    wire_api: str | None = None  # openai only: completions | responses (the SDK's default when unset)
    azure_api_version: str | None = None  # azure only

    def __post_init__(self) -> None:
        if self.provider is not None and self.provider not in PROVIDER_TYPES:
            raise ValueError(f"provider must be one of {', '.join(PROVIDER_TYPES)}, not {self.provider!r}")
        if self.provider is not None and not self.base_url:
            raise ValueError("a provider needs base_url")
        if self.provider is not None and not self.model:
            raise ValueError("a provider needs model: the model name it serves")
        if self.entra and self.provider != "azure":
            raise ValueError("entra sign-in applies to the azure provider only")

    @property
    def byok(self) -> bool:
        return self.provider is not None

    def can_run(self) -> tuple[bool, str]:
        try:
            import copilot  # noqa: F401
        except ImportError:
            return False, "the copilot runtime needs the Copilot SDK: install readthrough with the [study] extra"
        if self.byok and not self.entra and not os.environ.get(self.api_key_env, "").strip():
            return False, f"the copilot runtime is set to use {self.provider}, but {self.api_key_env} isn't set"
        return True, ""

    def provider_config(self) -> dict[str, Any] | None:
        """The SDK's ``ProviderConfig``: the key from the environment, or for Azure with ``entra`` a callback that
        fetches a fresh Entra token from ``az`` whenever the runtime asks."""
        if self.provider is None:
            return None
        config: dict[str, Any] = {"type": self.provider, "base_url": self.base_url}
        if self.entra:
            config["bearer_token_provider"] = lambda _args: entra_token()
        else:
            key = os.environ.get(self.api_key_env, "").strip()
            if not key:
                raise RuntimeError(
                    f"The copilot runtime is set to use {self.provider} at {self.base_url}, but {self.api_key_env} "
                    "isn't set. Put the provider's API key in that variable (never in config.toml)."
                )
            config["api_key"] = key
        if self.wire_api:
            config["wire_api"] = self.wire_api
        if self.provider == "azure" and self.azure_api_version:
            config["azure"] = {"api_version": self.azure_api_version}
        return config

    def billed_to(self) -> str:
        return f"{self.provider}:{urlsplit(self.base_url or '').hostname or self.base_url}" if self.byok else "copilot"


def entra_token() -> str:
    """A bearer token for Azure OpenAI from a signed-in ``az``. Asked for on demand; az caches and refreshes."""
    out, why = quiet_output(
        ["az", "account", "get-access-token", "--resource", COGNITIVE_SERVICES, "--query", "accessToken", "-o", "tsv"],
        with_failure=True,
    )
    if not out or not out.strip():
        first = why.splitlines()[0][:160] if why else "not signed in"
        raise RuntimeError(f"az has no token for Azure OpenAI ({first}); run az login")
    return out.strip()


class CopilotRuntime:
    name = "copilot"

    def __init__(self, options: Any) -> None:
        self.options: CopilotOptions = options if isinstance(options, CopilotOptions) else CopilotOptions()
        self._client: Any = None
        self._lock: asyncio.Lock | None = None
        self._sessions: list[CopilotSession] = []

    @property
    def model(self) -> str | None:
        return self.options.model  # None for GitHub Copilot: the service picks its default

    def describe(self) -> str:
        o = self.options
        return f"Copilot SDK, {o.provider} at {o.base_url} ({o.model})" if o.byok else "Copilot SDK, GitHub Copilot"

    async def _ensure_client(self) -> Any:
        from copilot import CopilotClient

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._client is None:
                # With a provider of our own, the runtime must not fall back to the Copilot CLI's GitHub sign-in.
                client = CopilotClient(use_logged_in_user=False) if self.options.byok else CopilotClient()
                await client.start()
                self._client = client
            return self._client

    def session_options(self, conversation: Conversation) -> dict[str, Any]:
        from copilot.generated.rpc import PermissionDecisionUserNotAvailable

        tools = [_define(spec) for spec in conversation.tools]
        options: dict[str, Any] = {
            "tools": tools,
            "available_tools": [t.name for t in tools],
            "on_permission_request": lambda _req, _ctx: PermissionDecisionUserNotAvailable(),
            "client_name": "readthrough",
            "working_directory": conversation.working_directory,
            "enable_skills": False,
            "streaming": True,
            "system_message": {"mode": "append", "content": conversation.system},
        }
        provider = self.options.provider_config()
        if provider is not None:
            options["provider"] = provider
        model = conversation.model or self.options.model
        if model:
            options["model"] = model
        return options

    async def open(self, conversation: Conversation) -> CopilotSession:
        client = await self._ensure_client()
        options = self.session_options(conversation)
        session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, conversation.key))
        exists = conversation.resume and _session_exists(session_id)
        sdk_session, fresh = await open_study_session(client, session_id, options, resume=exists)
        session = CopilotSession(sdk_session, fresh, self.options.billed_to())
        self._sessions.append(session)
        return session

    async def stop(self) -> None:
        for session in self._sessions:
            await session.close()
        self._sessions.clear()
        if self._client is not None:
            await self._client.stop()
            self._client = None


class CopilotSession:
    def __init__(self, sdk_session: Any, fresh: bool, billed_to: str) -> None:
        self.sdk = sdk_session
        self._fresh = fresh
        self.billed_to = billed_to
        self._closed = False

    @property
    def fresh(self) -> bool:
        return self._fresh

    async def send(self, prompt: str) -> AsyncIterator[Event]:
        """Forward the SDK's event stream as runtime events. Tool handlers run inside the SDK's dispatch."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        parts: list[str] = []

        def put(event: Event | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def on_event(event: Any) -> None:
            kind = getattr(event.type, "value", str(event.type))
            data = event.data
            if kind == "assistant.usage":
                put(Event("usage", usage=_usage(data, self.billed_to)))
            elif kind == "assistant.message_delta":
                delta = getattr(data, "delta_content", None) or ""
                parts.append(delta)
                put(Event("text", text=delta))
            elif kind == "assistant.message" and parts and not parts[-1].endswith("\n"):
                parts.append("\n\n")  # separate the text before and after a tool call
                put(Event("text", text="\n\n"))
            elif kind == "tool.execution_start":
                put(Event("tool", name=getattr(data, "tool_name", "?")))
            elif kind == "session.error":
                put(Event("error", text=getattr(data, "message", None) or "the model runtime reported an error"))
                put(None)
            elif kind == "session.idle":
                put(Event("done", text="".join(parts)))
                put(None)

        unsubscribe = self.sdk.on(on_event)
        try:
            await self.sdk.send(prompt)
            timeout = default_tuning().study.turn_timeout_seconds
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
                if event is None:
                    return
                yield event
        finally:
            unsubscribe()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self.sdk.disconnect()


def _define(spec: ToolSpec) -> Any:
    """A readthrough tool as the SDK wants it: a pydantic model for the arguments and an async handler."""
    from copilot import define_tool
    from pydantic import create_model

    fields: dict[str, Any] = {}
    required = set(spec.schema.get("required") or [])
    for name, prop in (spec.schema.get("properties") or {}).items():
        kind = _python_type(prop)
        fields[name] = (kind, ...) if name in required else (kind | None, prop.get("default"))
    params_model = create_model(f"{spec.name}_params", **fields)
    handler = spec.handler

    async def tool(params: Any, invocation: Any) -> str:  # noqa: ARG001 - the SDK passes it
        return await handler(params.model_dump(exclude_none=True))

    return define_tool(
        spec.name, description=spec.description, handler=tool, params_type=params_model, skip_permission=True,
        is_terminal=spec.terminal,
    )  # fmt: skip


def _python_type(prop: dict[str, Any]) -> Any:
    kind = prop.get("type")
    if kind == "array":
        item: Any = _python_type(prop.get("items") or {"type": "string"})
        return list.__class_getitem__(item)
    return {"string": str, "integer": int, "number": float, "boolean": bool}.get(kind, str)


def _usage(data: Any, billed_to: str) -> Usage:
    copilot_usage = getattr(data, "copilot_usage", None)
    byok = bool(getattr(data, "is_byok", False))
    return Usage(
        call_id=getattr(data, "api_call_id", None) or getattr(data, "provider_call_id", None),
        model=getattr(data, "model", None) or "?",
        input_tokens=getattr(data, "input_tokens", None),
        output_tokens=getattr(data, "output_tokens", None),
        cache_read_tokens=getattr(data, "cache_read_tokens", None),
        cache_write_tokens=getattr(data, "cache_write_tokens", None),
        reasoning_tokens=getattr(data, "reasoning_tokens", None),
        # The premium-request multiplier is Copilot's unit; the runtime sends 0 for a provider of your own,
        # which would read as "free", so it is left out there.
        multiplier=None if byok else getattr(data, "cost", None),
        nano_aiu=getattr(copilot_usage, "total_nano_aiu", None) if copilot_usage is not None else None,
        billed_to=billed_to,
    )


def _session_exists(session_id: str) -> bool:
    from readthrough.agents import Copilot

    return Copilot().session_info(session_id).exists  # the SDK shares the Copilot CLI's session store


async def open_study_session(
    client: Any, session_id: str, options: dict[str, Any], *, resume: bool
) -> tuple[Any, bool]:
    """Resume the conversation, or start one. Returns (session, whether it's new).

    A session whose history the CLI can't read back (say its folder was half-deleted) would otherwise break
    the Study tab for that PR for good. Fall back to a fresh session: the study log, not the chat history,
    is the record, and a new session is handed the brief with the log at its end.
    """
    if resume:
        try:
            return await client.resume_session(session_id, **options), False
        except Exception as exc:  # noqa: BLE001 - SDK errors have no common type
            log.warning("Couldn't resume study session %s (%s); starting a fresh one", session_id, str(exc)[:200])
            session_id = str(uuid.uuid4())
    return await client.create_session(session_id=session_id, **options), True


RUNTIMES.add(
    "copilot",
    "the Copilot SDK: GitHub Copilot through its sign-in, or a model provider of your own (needs [study])",
    options=CopilotOptions,
)(CopilotRuntime)

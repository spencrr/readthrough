"""The model runtimes behind the study assistant: each wire format against a scripted transport, no network.

The Anthropic and OpenAI runtimes speak their APIs over urllib, so ``urlopen`` is replaced with a fake that
serves server-sent events the way the documented streams look; the Copilot runtime is tested against a fake
SDK. What's checked is the contract every runtime must honour: the events it yields, the tool loop (results
go back, a terminal tool ends the turn, the cap holds), and usage in the provider's own units with nothing
unreported written as zero.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types
import urllib.error
from email.message import Message
from typing import Any

import pytest

from readthrough import runtimes
from readthrough.registry import RegistryError
from readthrough.runtimes import Conversation, Event, ToolSpec
from readthrough.runtimes.anthropic import AnthropicOptions, AnthropicRuntime
from readthrough.runtimes.copilot import CopilotOptions, CopilotRuntime
from readthrough.runtimes.openai import OpenAIOptions, OpenAIRuntime


def _tools(calls: list[tuple[str, dict[str, Any]]]) -> list[ToolSpec]:
    async def read(args: dict[str, Any]) -> str:
        calls.append(("read_file", args))
        return f"contents of {args['path']}"

    async def ask(args: dict[str, Any]) -> str:
        calls.append(("ask_quiz_question", args))
        return "Asked Q1."

    async def broken(args: dict[str, Any]) -> str:
        raise ValueError("outside the PR checkout")

    return [
        ToolSpec("read_file", "Read a file.", {"properties": {"path": {"type": "string"}}, "required": ["path"]}, read),
        ToolSpec("ask_quiz_question", "Ask.", {"properties": {"question": {"type": "string"}}}, ask, terminal=True),
        ToolSpec("get_diff", "Diff.", {"properties": {"path": {"type": "string"}}}, broken),
    ]


def _conversation(tools: list[ToolSpec], **kw: Any) -> Conversation:
    return Conversation(key="repo#7#study", system="Be Socratic.", tools=tools, working_directory="wt", **kw)


async def _collect(session: Any, prompt: str) -> list[Event]:
    return [event async for event in session.send(prompt)]


class FakeHttp:
    """Scripted ``urlopen``: each call pops the next (status, headers, SSE events) and records the request."""

    def __init__(self, answers: list[tuple[int, dict[str, str], list[dict[str, Any]]]]):
        self.answers = answers
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(self, req: Any, timeout: float | None = None) -> Any:
        headers = {k.lower(): v for k, v in (req.header_items() if hasattr(req, "header_items") else [])}
        self.requests.append((req.full_url, headers, json.loads(req.data.decode("utf-8"))))
        status, resp_headers, events = self.answers.pop(0)
        body = "".join(f"event: {e.get('type')}\ndata: {json.dumps(e)}\n\n" for e in events).encode("utf-8")
        if status >= 400:
            message = Message()
            for key, value in resp_headers.items():
                message[key] = value
            raise urllib.error.HTTPError(
                req.full_url,
                status,
                "refused",
                message,
                io.BytesIO(body if events else b'{"error": {"type": "rate_limit_error", "message": "slow down"}}'),
            )

        class Response(io.BytesIO):  # a BytesIO is already a context manager and iterable by line
            def __init__(self, data: bytes, hdrs: dict[str, str]) -> None:
                super().__init__(data)
                self.headers = hdrs
                self.status = status

        return Response(body, resp_headers)


# ---------------------------------------------------------------- the registry


def test_registry_lists_the_runtimes_and_resolves_auto(monkeypatch: pytest.MonkeyPatch):
    assert {p.name for p in runtimes.RUNTIMES} == {"anthropic", "copilot", "openai"}
    monkeypatch.setitem(sys.modules, "copilot", None)  # makes `import copilot` raise ImportError
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _, ok, why = runtimes.resolve("auto")
    assert not ok and "install the [study] extra, or set an API key" in why
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert runtimes.resolve("auto") == ("anthropic", True, "")
    assert runtimes.resolve("copilot")[1] is False
    with pytest.raises(RegistryError, match="did you mean 'openai'"):
        runtimes.resolve("openia")
    runtime = runtimes.make("auto")
    assert isinstance(runtime, AnthropicRuntime) and "api.anthropic.com" in runtime.describe()
    with pytest.raises(RuntimeError, match=r"needs the Copilot SDK"):
        runtimes.make("copilot")
    # Options the config loader built take precedence, and say for themselves whether they can run.
    opts = {"openai": OpenAIOptions(base_url="https://r.openai.azure.com/openai/v1", entra=True)}
    assert runtimes.resolve("openai", opts) == ("openai", True, "")
    assert runtimes.tool_schema(
        ToolSpec("t", "d", {"properties": {"a": {"type": "string"}}}, _tools([])[0].handler)
    ) == {"type": "object", "properties": {"a": {"type": "string"}}}
    assert (
        runtimes.parse_arguments('{"a": 1}') == {"a": 1}
        and runtimes.parse_arguments("not json") == {}
        and runtimes.parse_arguments({"b": 2}) == {"b": 2}
    )


def test_tool_arguments_are_held_to_the_schema_before_a_handler_sees_them():
    """What a model sends is shaped by what it read; the REST runtimes check it the way the SDK's pydantic
    models do: unknown properties dropped, types coerced or refused, required arguments present."""
    schema = {
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer"},
            "refs": {"type": "array", "items": {"type": "string"}},
            "ok": {"type": "boolean"},
            "score": {"type": "number"},
        },
        "required": ["path"],
    }
    spec = ToolSpec("t", "d", schema, _tools([])[0].handler)
    args = {"path": "a.py", "line": "12", "refs": ["x:1"], "ok": True, "score": 3, "id": 99, "ts": "then", "by": "you"}
    assert runtimes.check_arguments(spec, args) == {
        "path": "a.py",
        "line": 12,
        "refs": ["x:1"],
        "ok": True,
        "score": 3.0,
    }
    assert runtimes.check_arguments(spec, {"path": 7, "line": None}) == {"path": "7"}, (
        "None is absent; scalars are text"
    )
    for bad in (
        {},
        {"path": "a", "line": "<b>"},
        {"path": "a", "line": 1.5},
        {"path": "a", "line": True},
        {"path": "a", "refs": "x:1"},
        {"path": "a", "refs": [{"x": 1}]},
        {"path": "a", "ok": "yes"},
        {"path": "a", "score": "3"},
        {"path": {"nested": 1}},
    ):
        with pytest.raises((TypeError, ValueError), match=r"^t[.:]"):
            runtimes.check_arguments(spec, bad)


def test_retry_waits_are_bounded_jittered_and_honour_retry_after():
    """Seeded: the jitter has its own generator, so the distribution is checked with a stated margin."""
    import random
    import statistics

    rng = random.Random(7)
    waits = [runtimes.retry_wait(1, None, rng) for _ in range(2000)]
    assert all(2.0 <= w <= 2.5 for w in waits), "first wait: the backoff plus up to a quarter of itself"
    assert abs(statistics.mean(waits) - 2.25) < 0.02, "uniform jitter: the mean sits at the middle"
    assert runtimes.retry_wait(2, None, rng) >= 4.0 and runtimes.retry_wait(9, None, rng) == 30.0
    assert runtimes.retry_wait(1, "12", rng) == 12.0 and runtimes.retry_wait(1, "9999", rng) == 30.0
    assert 2.0 <= runtimes.retry_wait(1, "Wed, 21 Oct 2026 07:28:00 GMT", rng) <= 2.5, "an HTTP-date: backoff"
    assert runtimes.retry_wait(1, "-3", rng) == 0.0

    attempts: list[int] = []
    slept: list[float] = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise runtimes.Retryable("busy", retry_after="1")
        return "ok"

    assert runtimes.with_retries("x", flaky, sleep=slept.append) == "ok" and slept == [1.0, 1.0]
    attempts.clear()
    with pytest.raises(runtimes.Retryable, match="busy"):
        runtimes.with_retries("x", flaky, sleep=slept.append, attempts=2)
    assert len(slept) == 3, "the last attempt is raised, not slept on"


# ---------------------------------------------------------------- anthropic


def _anthropic_turn(
    text: str | None,
    tool_uses: list[tuple[str, str, dict[str, Any]]],
    *,
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int | None = None,
) -> list[dict[str, Any]]:
    usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": 1}
    if cache_read is not None:
        usage["cache_read_input_tokens"] = cache_read
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"id": "msg_1", "model": "claude-sonnet-5-5", "usage": usage}}
    ]
    index = 0
    if text:
        events += [{"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}]
        for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
            events.append(
                {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": piece}}
            )
        events.append({"type": "content_block_stop", "index": index})
        index += 1
    for use_id, name, args in tool_uses:
        raw = json.dumps(args)
        events += [
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "tool_use", "id": use_id, "name": name, "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": raw[:3]},
            },
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": raw[3:]},
            },
            {"type": "content_block_stop", "index": index},
        ]
        index += 1
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool_uses else "end_turn"},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]
    return events


def test_anthropic_runs_the_tool_loop_and_reports_usage_in_its_own_units(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    http = FakeHttp([
        (200, {"request-id": "req_1"}, _anthropic_turn("Let me look.", [("toolu_1", "read_file", {"path": "a.py"})], cache_read=80)),
        (200, {"request-id": "req_2"}, _anthropic_turn("Here is a question.", [("toolu_2", "ask_quiz_question", {"question": "Why?"})], input_tokens=140, output_tokens=30)),
    ])  # fmt: skip
    monkeypatch.setattr("readthrough.runtimes.anthropic.urllib.request.urlopen", http)
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime = AnthropicRuntime(AnthropicOptions(max_tokens=512))
    session = asyncio.run(runtime.open(_conversation(_tools(calls))))
    assert session.fresh

    events = asyncio.run(_collect(session, "Quiz me."))

    assert not session.fresh
    kinds = [(e.kind, e.text or e.name) for e in events if e.kind != "usage"]
    assert kinds == [
        ("text", "Let me look."),
        ("tool", "read_file"),
        ("text", "Here is a question."),
        ("tool", "ask_quiz_question"),
        ("done", "Let me look.\n\nHere is a question."),
    ]
    assert calls == [("read_file", {"path": "a.py"}), ("ask_quiz_question", {"question": "Why?"})]
    used = [e.usage for e in events if e.kind == "usage"]
    assert len(used) == 2 and used[0] is not None and used[1] is not None
    assert (used[0].call_id, used[0].model, used[0].input_tokens, used[0].output_tokens, used[0].cache_read_tokens) == (
        "req_1",
        "claude-sonnet-5-5",
        100,
        20,
        80,
    )
    assert used[1].cache_read_tokens is None and used[1].multiplier is None and used[1].nano_aiu is None, (
        "unreported means absent"
    )
    assert used[0].billed_to == "anthropic:api.anthropic.com" and used[0].tokens() == {
        "input": 100,
        "output": 20,
        "cache_read": 80,
    }

    url, headers, body = http.requests[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-ant-secret" and headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 512 and body["stream"] is True
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"} and body["tools"][-1]["cache_control"] == {
        "type": "ephemeral"
    }
    assert body["tools"][0] == {
        "name": "read_file",
        "description": "Read a file.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }
    second = http.requests[1][2]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["content"][-1] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "read_file",
        "input": {"path": "a.py"},
    }
    assert second[-1] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "contents of a.py"}],
    }
    # The terminal tool's result is in the transcript too, so the next turn starts clean.
    assert session.messages[-1]["content"][0]["tool_use_id"] == "toolu_2"
    assert asyncio.run(runtime.open(_conversation(_tools(calls)))) is session, "the same key resumes the conversation"


def test_anthropic_tool_failures_go_back_to_the_model_and_refusals_end_the_turn(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    http = FakeHttp([
        (200, {}, _anthropic_turn(None, [("toolu_1", "get_diff", {"path": "../x"}), ("toolu_2", "nope", {})])),
        (200, {}, _anthropic_turn("Fine.", [])),
        (429, {"retry-after": "7"}, []),
        (529, {}, []),
        (200, {}, _anthropic_turn("Back.", [])),
        (401, {}, []),
    ])  # fmt: skip
    monkeypatch.setattr("readthrough.runtimes.anthropic.urllib.request.urlopen", http)
    slept: list[float] = []
    monkeypatch.setattr(runtimes.time, "sleep", slept.append)
    runtime = AnthropicRuntime(AnthropicOptions())
    session = asyncio.run(runtime.open(_conversation(_tools([]))))
    events = asyncio.run(_collect(session, "go"))
    results = http.requests[1][2]["messages"][-1]["content"]
    assert (
        results[0]["content"] == "Tool failed: outside the PR checkout"
        and results[1]["content"] == "Unknown tool 'nope'."
    )
    assert events[-1] == Event("done", text="Fine.")

    # A rate limit and an overload are retried, waiting what the provider asked for when it said; a bad key
    # is not: there is no point asking again, and the learner should see why at once.
    events = asyncio.run(_collect(session, "again"))
    assert events[-1] == Event("done", text="Back.") and len(http.requests) == 5
    assert slept[0] == 7.0 and 4.0 <= slept[1] <= 5.0
    events = asyncio.run(_collect(session, "once more"))
    assert events == [Event("error", text="Anthropic refused the call: rate_limit_error: slow down")]
    assert len(http.requests) == 6 and len(slept) == 2
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert AnthropicOptions().can_run() == (False, "the anthropic runtime needs an API key in ANTHROPIC_API_KEY")


def test_anthropic_call_cap_and_mid_stream_errors(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    forever = [(200, {}, _anthropic_turn(None, [("t", "read_file", {"path": "a"})])) for _ in range(3)]
    http = FakeHttp(
        [
            *forever,
            (
                200,
                {},
                [
                    {"type": "message_start", "message": {"usage": {}}},
                    {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
                ],
            ),
        ]
    )
    monkeypatch.setattr("readthrough.runtimes.anthropic.urllib.request.urlopen", http)
    runtime = AnthropicRuntime(AnthropicOptions(max_calls_per_turn=2))
    session = asyncio.run(runtime.open(_conversation(_tools([]))))
    events = asyncio.run(_collect(session, "go"))
    assert events[-1] == Event("error", text="the model made more than 2 calls in one turn")
    assert len(http.requests) == 2
    fresh = asyncio.run(AnthropicRuntime(AnthropicOptions()).open(_conversation(_tools([]), resume=False)))
    http.answers = [http.answers[-1]]
    events = asyncio.run(_collect(fresh, "go"))
    assert events == [Event("error", text="Anthropic reported an error mid-stream: Overloaded")]


# ---------------------------------------------------------------- openai


def _openai_turn(
    text: str | None,
    calls: list[tuple[str, str, dict[str, Any]]],
    *,
    response_id: str = "resp_1",
    cached: int | None = None,
    reasoning: int | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [{"type": "response.created", "response": {"id": response_id}}]
    output: list[dict[str, Any]] = []
    if text:
        events += [
            {"type": "response.output_text.delta", "delta": text[:4], "item_id": "msg"},
            {"type": "response.output_text.delta", "delta": text[4:], "item_id": "msg"},
        ]
        output.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
    for n, (call_id, name, args) in enumerate(calls):
        raw = json.dumps(args)
        item_id = f"fc_{n}"
        events += [
            {
                "type": "response.output_item.added",
                "item": {"id": item_id, "type": "function_call", "call_id": call_id, "name": name, "arguments": ""},
            },
            {"type": "response.function_call_arguments.delta", "item_id": item_id, "delta": raw[:2]},
            {"type": "response.function_call_arguments.delta", "item_id": item_id, "delta": raw[2:]},
        ]
        output.append({"type": "function_call", "call_id": call_id, "name": name, "arguments": raw})
    usage: dict[str, Any] = {
        "input_tokens": 200,
        "output_tokens": 40,
        "total_tokens": 240,
        "input_tokens_details": {},
        "output_tokens_details": {},
    }
    if cached is not None:
        usage["input_tokens_details"]["cached_tokens"] = cached
    if reasoning is not None:
        usage["output_tokens_details"]["reasoning_tokens"] = reasoning
    events.append(
        {
            "type": "response.completed",
            "response": {"id": response_id, "model": "gpt-5.5-2026-07", "output": output, "usage": usage},
        }
    )
    return events


def test_openai_runs_the_tool_loop_through_previous_response_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    http = FakeHttp([
        (200, {}, _openai_turn("Looking.", [("call_1", "read_file", {"path": "a.py"})], response_id="resp_1", cached=150, reasoning=12)),
        (200, {}, _openai_turn("Question time.", [("call_2", "ask_quiz_question", {"question": "Why?"})], response_id="resp_2")),
        (200, {}, _openai_turn(None, [], response_id="resp_3")),  # settling the terminal tool's output
    ])  # fmt: skip
    monkeypatch.setattr("readthrough.runtimes.openai.urllib.request.urlopen", http)
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime = OpenAIRuntime(OpenAIOptions(model="gpt-5.5"))
    session = asyncio.run(runtime.open(_conversation(_tools(calls))))

    events = asyncio.run(_collect(session, "Quiz me."))

    kinds = [(e.kind, e.text or e.name) for e in events if e.kind != "usage"]
    assert kinds == [
        ("text", "Looking."),
        ("tool", "read_file"),
        ("text", "Question time."),
        ("tool", "ask_quiz_question"),
        ("done", "Looking.\n\nQuestion time."),
    ]
    assert calls == [("read_file", {"path": "a.py"}), ("ask_quiz_question", {"question": "Why?"})]
    used = [e.usage for e in events if e.kind == "usage"]
    assert used[0] is not None and (
        used[0].call_id,
        used[0].model,
        used[0].cache_read_tokens,
        used[0].reasoning_tokens,
    ) == ("resp_1", "gpt-5.5-2026-07", 150, 12)
    assert used[1] is not None and used[1].cache_read_tokens is None and used[1].billed_to == "openai:api.openai.com"

    url, headers, body = http.requests[0]
    assert url == "https://api.openai.com/v1/responses" and headers["authorization"] == "Bearer sk-secret"
    assert body["instructions"] == "Be Socratic." and body["store"] is True and "previous_response_id" not in body
    assert body["tools"][0] == {
        "type": "function",
        "name": "read_file",
        "description": "Read a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "strict": False,
    }
    second = http.requests[1][2]
    assert second["previous_response_id"] == "resp_1"
    assert second["input"] == [{"type": "function_call_output", "call_id": "call_1", "output": "contents of a.py"}]
    settle = http.requests[2][2]
    assert settle["previous_response_id"] == "resp_2" and settle["tool_choice"] == "none"
    assert settle["input"] == [{"type": "function_call_output", "call_id": "call_2", "output": "Asked Q1."}]
    assert session.previous_response_id == "resp_3"


def test_openai_on_azure_uses_the_deployment_and_entra_or_key(monkeypatch: pytest.MonkeyPatch):
    http = FakeHttp([(200, {}, _openai_turn("ok", [])), (200, {}, _openai_turn("ok", [])), (404, {}, [])])
    monkeypatch.setattr("readthrough.runtimes.openai.urllib.request.urlopen", http)
    monkeypatch.setattr(
        "readthrough.runtimes.openai.quiet_output", lambda cmd, with_failure=False: ("eyJ-entra-token\n", "")
    )
    azure = OpenAIOptions(base_url="https://r.openai.azure.com/openai/v1", model="my-deployment", entra=True)
    assert azure.azure and azure.can_run() == (True, "") and azure.billed_to() == "azure:r.openai.azure.com"
    session = asyncio.run(OpenAIRuntime(azure).open(_conversation(_tools([]))))
    asyncio.run(_collect(session, "hi"))
    url, headers, body = http.requests[0]
    assert (
        url == "https://r.openai.azure.com/openai/v1/responses" and headers["authorization"] == "Bearer eyJ-entra-token"
    )
    assert body["model"] == "my-deployment"

    monkeypatch.setenv("AZURE_KEY", "k-secret")
    keyed = OpenAIOptions(base_url="https://r.openai.azure.com/openai/v1", model="d", api_key_env="AZURE_KEY")
    session = asyncio.run(OpenAIRuntime(keyed).open(_conversation(_tools([]))))
    asyncio.run(_collect(session, "hi"))
    assert http.requests[1][1]["api-key"] == "k-secret" and "authorization" not in http.requests[1][1]
    events = asyncio.run(_collect(session, "again"))
    assert events[0].kind == "error" and "refused the call" in events[0].text
    assert len(http.requests) == 3, "a 404 is not retried"
    with pytest.raises(ValueError, match="Azure endpoints only"):
        OpenAIOptions(entra=True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert OpenAIOptions().can_run() == (False, "the openai runtime needs an API key in OPENAI_API_KEY")
    assert "Entra sign-in" in OpenAIRuntime(azure).describe()


# ---------------------------------------------------------------- copilot


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    def define_tool(
        name: str, *, description: str, handler: Any, params_type: Any, skip_permission: bool, is_terminal: bool
    ) -> Any:
        return types.SimpleNamespace(
            name=name, description=description, handler=handler, params_type=params_type, is_terminal=is_terminal
        )

    monkeypatch.setitem(
        sys.modules,
        "copilot",
        types.SimpleNamespace(define_tool=define_tool, CopilotClient=lambda **kw: client.built(kw)),
    )
    monkeypatch.setitem(sys.modules, "copilot.generated", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "copilot.generated.rpc",
        types.SimpleNamespace(PermissionDecisionUserNotAvailable=type("PermissionDecisionUserNotAvailable", (), {})),
    )


class SdkSession:
    def __init__(self, events: list[Any]):
        self.events = events
        self.sent: list[str] = []
        self.disconnected = False
        self.callback: Any = None

    def on(self, callback: Any) -> Any:
        self.callback = callback

        def unsubscribe() -> None:
            self.callback = None

        return unsubscribe

    async def send(self, prompt: str) -> None:
        self.sent.append(prompt)
        for event in self.events:
            self.callback(event)

    async def disconnect(self) -> None:
        self.disconnected = True


class SdkClient:
    def __init__(self, session: SdkSession):
        self.session = session
        self.construction: dict[str, Any] = {}
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.resumed: list[str] = []
        self.started = self.stopped = False

    def built(self, kwargs: dict[str, Any]) -> SdkClient:
        self.construction = kwargs
        return self

    async def start(self) -> None:
        self.started = True

    async def create_session(self, *, session_id: str, **options: Any) -> SdkSession:
        self.created.append((session_id, options))
        return self.session

    async def resume_session(self, session_id: str, **options: Any) -> SdkSession:
        self.resumed.append(session_id)
        return self.session

    async def stop(self) -> None:
        self.stopped = True


def _sdk_event(kind: str, **data: Any) -> Any:
    return types.SimpleNamespace(type=types.SimpleNamespace(value=kind), data=types.SimpleNamespace(**data))


def test_copilot_runtime_translates_tools_sessions_and_usage(monkeypatch: pytest.MonkeyPatch, tmp_path):
    usage = _sdk_event("assistant.usage", api_call_id="chatcmpl-1", model="gpt-5", input_tokens=10, output_tokens=2, cache_read_tokens=None,
                       cache_write_tokens=None, reasoning_tokens=None, cost=1.0, is_byok=False, copilot_usage=types.SimpleNamespace(total_nano_aiu=2.0e9))  # fmt: skip
    sdk_session = SdkSession([
        _sdk_event("assistant.message_delta", delta_content="Hello"),
        _sdk_event("tool.execution_start", tool_name="read_file"),
        _sdk_event("assistant.message", content="Hello"),
        _sdk_event("assistant.message_delta", delta_content="more"),
        usage,
        _sdk_event("session.idle"),
    ])  # fmt: skip
    client = SdkClient(sdk_session)
    _install_fake_sdk(monkeypatch, client)
    monkeypatch.setattr("readthrough.runtimes.copilot._session_exists", lambda sid: False)
    monkeypatch.setattr(
        "readthrough.runtimes.copilot.default_tuning",
        lambda: types.SimpleNamespace(study=types.SimpleNamespace(turn_timeout_seconds=5)),
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime = CopilotRuntime(CopilotOptions())
    session = asyncio.run(runtime.open(_conversation(_tools(calls), model="gpt-5")))

    assert client.started and client.construction == {} and session.fresh
    _, options = client.created[0]
    assert options["model"] == "gpt-5" and options["working_directory"] == "wt" and options["enable_skills"] is False
    assert options["system_message"] == {"mode": "append", "content": "Be Socratic."}
    assert options["available_tools"] == ["read_file", "ask_quiz_question", "get_diff"] and "provider" not in options
    tool = options["tools"][0]
    assert tool.is_terminal is False and options["tools"][1].is_terminal is True
    params = tool.params_type(path="a.py")
    assert asyncio.run(tool.handler(params, None)) == "contents of a.py" and calls == [("read_file", {"path": "a.py"})]

    events = asyncio.run(_collect(session, "hi"))
    assert sdk_session.sent == ["hi"] and sdk_session.callback is None, "unsubscribed afterwards"
    assert [(e.kind, e.text or e.name) for e in events if e.kind != "usage"] == [
        ("text", "Hello"),
        ("tool", "read_file"),
        ("text", "\n\n"),
        ("text", "more"),
        ("done", "Hello\n\nmore"),
    ]
    used = next(e.usage for e in events if e.kind == "usage")
    assert used is not None and (used.call_id, used.model, used.multiplier, used.nano_aiu, used.billed_to) == (
        "chatcmpl-1",
        "gpt-5",
        1.0,
        2.0e9,
        "copilot",
    )
    assert used.tokens() == {"input": 10, "output": 2}

    asyncio.run(runtime.stop())
    assert sdk_session.disconnected and client.stopped


def test_copilot_runtime_with_a_provider_of_your_own(monkeypatch: pytest.MonkeyPatch):
    client = SdkClient(SdkSession([_sdk_event("session.error", message="boom")]))
    _install_fake_sdk(monkeypatch, client)
    monkeypatch.setattr("readthrough.runtimes.copilot._session_exists", lambda sid: True)
    monkeypatch.setattr(
        "readthrough.runtimes.copilot.default_tuning",
        lambda: types.SimpleNamespace(study=types.SimpleNamespace(turn_timeout_seconds=5)),
    )
    monkeypatch.setenv("MY_KEY", "sk-provider")
    opts = CopilotOptions(
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-5-5", api_key_env="MY_KEY"
    )
    assert opts.byok and opts.billed_to() == "anthropic:api.anthropic.com"
    runtime = CopilotRuntime(opts)
    assert "anthropic at https://api.anthropic.com" in runtime.describe()
    session = asyncio.run(runtime.open(_conversation(_tools([]))))
    assert client.construction == {"use_logged_in_user": False}, "never the CLI's GitHub sign-in"
    assert client.resumed and not client.created and not session.fresh, "an existing conversation is resumed"
    events = asyncio.run(_collect(session, "hi"))
    assert events == [Event("error", text="boom")]

    byok_usage = _sdk_event("assistant.usage", api_call_id="c", model="claude-sonnet-5-5", input_tokens=1, output_tokens=1, cache_read_tokens=None,
                            cache_write_tokens=None, reasoning_tokens=None, cost=0.0, is_byok=True, copilot_usage=None)  # fmt: skip
    from readthrough.runtimes.copilot import _usage

    used = _usage(byok_usage.data, "anthropic:api.anthropic.com")
    assert used.multiplier is None, "the runtime sends 0 for a provider of your own, which would read as free"

    with pytest.raises(ValueError, match="provider must be one of"):
        CopilotOptions(provider="gemini", base_url="https://x", model="m")
    with pytest.raises(ValueError, match="needs base_url"):
        CopilotOptions(provider="anthropic", model="m")
    with pytest.raises(ValueError, match="entra sign-in applies"):
        CopilotOptions(provider="openai", base_url="https://x", model="m", entra=True)
    monkeypatch.delenv("MY_KEY")
    assert opts.can_run() == (False, "the copilot runtime is set to use anthropic, but MY_KEY isn't set")
    with pytest.raises(RuntimeError, match="MY_KEY isn't set"):
        opts.provider_config()
    azure = CopilotOptions(
        provider="azure",
        base_url="https://r.openai.azure.com/openai/v1",
        model="gpt-4.1",
        entra=True,
        azure_api_version="2025-04-01",
    )
    config = azure.provider_config() or {}
    assert (
        config["azure"] == {"api_version": "2025-04-01"}
        and callable(config["bearer_token_provider"])
        and "api_key" not in config
    )

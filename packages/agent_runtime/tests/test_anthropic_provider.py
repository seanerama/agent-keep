"""Anthropic adapter unit tests — httpx.MockTransport at the adapter boundary.

This is the ONE sanctioned mock seam (ADR 0004, alternatives note): the
hand-rolled adapter (ADR 0003, factory decision 3 — no SDK) is exercised
against a local transport that speaks the recorded Messages API shapes.
Nothing here touches the network, a key, or CI's composition path (which
stays on the static provider).
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

from agent_runtime.audit import AgentIdentity, AuditRecord, Trigger
from agent_runtime.components.anthropic_provider import (
    AnthropicProvider,
    AnthropicRequestError,
    MissingApiKeyError,
)
from agent_runtime.components.prompt_assembler import PromptAssembler
from agent_runtime.components.single_session import SingleSessionManager
from agent_runtime.core import AgentCore
from agent_runtime.provider import AssembledPrompt, PromptMessage, ToolDescriptor

KEY_ENV = "ANTHROPIC_API_KEY"
TEST_KEY = "test-key-not-real"  # unit-test fixture value; never a real credential
MODEL = "claude-test-fixture"  # model ids in production come FROM THE SPEC


def _ok_body(
    *,
    text: str = "hello from the api",
    tool_use: list[dict[str, Any]] | None = None,
    tokens_in: int = 11,
    tokens_out: int = 7,
) -> dict[str, Any]:
    """Recorded-shape Messages API success envelope."""
    content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    content += tool_use or []
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }


def _provider(
    handler: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_retries: int = 3,
) -> tuple[AnthropicProvider, list[float]]:
    """Adapter wired to a MockTransport; backoff sleeps are recorded, not slept."""
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = AnthropicProvider(
        model=MODEL,
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
        backoff_base=0.25,
        sleep=record_sleep,
    )
    return provider, sleeps


PROMPT = AssembledPrompt(
    system="You are the test persona.",
    messages=[
        PromptMessage(role="user", text="first user turn"),
        PromptMessage(role="assistant", text="[tool calls requested: local-demo.clock.now({})]"),
        PromptMessage(role="tool", text="[local-demo.clock.now -> ok] 12:00"),
        PromptMessage(role="user", text="and now?"),
    ],
    tools=(
        ToolDescriptor(
            name="local-demo.clock.now",
            description="Read the clock.",
            parameters={"timezone": {"type": "string"}},
        ),
    ),
)


def test_request_mapping_system_messages_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """system / history (incl. tool role) / tool defs -> Messages API shape."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    provider, _ = _provider(handler, monkeypatch)
    asyncio.run(provider.complete(PROMPT))

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == TEST_KEY
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    body = captured["body"]
    assert body["model"] == MODEL
    assert body["system"] == "You are the test persona."
    # tool turns ride as user turns (the core flattens tool rounds to text —
    # the API has no tool role); everything else maps 1:1.
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user", "user"]
    assert body["messages"][2]["content"] == "[local-demo.clock.now -> ok] 12:00"
    # tool names are sanitized onto the API grammar (no dots), schema wrapped.
    [tool] = body["tools"]
    assert tool["name"] == "local-demo_clock_now"
    assert tool["description"] == "Read the clock."
    assert tool["input_schema"] == {
        "type": "object",
        "properties": {"timezone": {"type": "string"}},
    }


def test_toolless_request_omits_tools_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    provider, _ = _provider(handler, monkeypatch)
    asyncio.run(
        provider.complete(
            AssembledPrompt(system="p", messages=[PromptMessage(role="user", text="hi")])
        )
    )
    assert "tools" not in captured["body"]


def test_response_mapping_text_tool_use_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """text + tool_use blocks -> ProviderReply text/tool_calls; usage -> cost."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                text="checking the clock",
                tool_use=[
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "local-demo_clock_now",
                        "input": {"timezone": "UTC"},
                    }
                ],
                tokens_in=42,
                tokens_out=13,
            ),
        )

    provider, _ = _provider(handler, monkeypatch)
    reply = asyncio.run(provider.complete(PROMPT))
    assert reply.text == "checking the clock"
    assert reply.tokens_in == 42 and reply.tokens_out == 13
    [call] = reply.tool_calls
    assert call.id == "toolu_01"
    # wire name maps BACK to the spec-qualified grant name the executor knows
    assert call.name == "local-demo.clock.now"
    assert call.arguments == {"timezone": "UTC"}


def test_429_retries_honoring_retry_after_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, json={"error": {}})
        return httpx.Response(200, json=_ok_body())

    provider, sleeps = _provider(handler, monkeypatch)
    reply = asyncio.run(provider.complete(PROMPT))
    assert reply.text == "hello from the api"
    assert len(calls) == 2
    assert sleeps == [3.0], "Retry-After (3s > backoff base) must be honored"


def test_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "9999"}, json={"error": {}})

    provider, sleeps = _provider(handler, monkeypatch, max_retries=1)
    with pytest.raises(AnthropicRequestError):
        asyncio.run(provider.complete(PROMPT))
    assert sleeps == [30.0], "server-requested delay is capped at BACKOFF_CAP_SECONDS"


def test_5xx_retries_with_backoff_then_exhausts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(529, json={"error": {"type": "overloaded_error", "message": "x"}})

    provider, sleeps = _provider(handler, monkeypatch, max_retries=2)
    with pytest.raises(AnthropicRequestError) as excinfo:
        asyncio.run(provider.complete(PROMPT))
    assert len(calls) == 3, "initial attempt + 2 retries"
    assert sleeps == [0.25, 0.5], "capped exponential backoff"
    assert excinfo.value.status_code == 529


def test_auth_4xx_fails_hard_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            401, json={"error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        )

    provider, sleeps = _provider(handler, monkeypatch)
    with pytest.raises(AnthropicRequestError) as excinfo:
        asyncio.run(provider.complete(PROMPT))
    assert len(calls) == 1, "auth errors must never be retried"
    assert sleeps == []
    assert excinfo.value.status_code == 401
    assert TEST_KEY not in str(excinfo.value), "the key never appears in errors/logs"


def test_missing_api_key_fails_loudly_naming_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(MissingApiKeyError, match=KEY_ENV):
        AnthropicProvider(model=MODEL)
    monkeypatch.delenv("MY_CUSTOM_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="MY_CUSTOM_KEY"):
        AnthropicProvider(model=MODEL, api_key_env="MY_CUSTOM_KEY")


def test_tool_name_sanitization_collision_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — never reached
        return httpx.Response(200, json=_ok_body())

    provider, _ = _provider(handler, monkeypatch)
    prompt = AssembledPrompt(
        system="p",
        messages=[PromptMessage(role="user", text="hi")],
        tools=(
            ToolDescriptor(name="srv.a.b", description="one", parameters={}),
            ToolDescriptor(name="srv.a_b", description="two", parameters={}),
        ),
    )
    with pytest.raises(ValueError, match="collide"):
        asyncio.run(provider.complete(prompt))


class _ListSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.records.append(record)


def test_adapter_failure_writes_stage5_error_audit_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure path composes with the stage-5 error-audit rule: a raising
    adapter is still a model_call — recorded (outcome error, digests only,
    action named after the SPEC's model), then re-raised unchanged."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"type": "permission_error", "message": "no"}})

    provider, _ = _provider(handler, monkeypatch)
    sink = _ListSink()
    core = AgentCore(
        identity=AgentIdentity(slug="t", spec_version="0.1.0", image_digest="sha256:test"),
        persona_identity="persona",
        queue=None,  # type: ignore[arg-type] — call_model does not touch the queue
        sessions=SingleSessionManager(),
        assembler=PromptAssembler(),
        provider=provider,
        audit_sink=sink,
    )
    trigger = Trigger(message_id="m-1", purpose="p")
    with pytest.raises(AnthropicRequestError):
        asyncio.run(core.call_model(PROMPT, trigger))
    [record] = sink.records
    assert record.event == "model_call"
    assert record.outcome.status == "error"
    assert record.action.name == f"anthropic:{MODEL}"
    assert record.cost is not None and record.cost.tokens_in == 0

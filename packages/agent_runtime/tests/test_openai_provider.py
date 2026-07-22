"""OpenAI adapter unit tests — hermetic, against a stdlib stub HTTP server.

The hand-rolled adapter (issue #15, the anthropic-shaped cloud variant — no
SDK) is driven against a real stdlib ``http.server`` in a background thread (the
ollama/egress stub pattern), so the whole httpx request/response path is
exercised without a network or a real OpenAI server. The API key is injected via
the environment (the anthropic posture — a missing key refuses construction);
the key value is a test fixture, never a real credential. Nothing here touches
CI's composition path (which stays on the static provider).
"""

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from agent_runtime.components.openai_provider import (
    MissingApiKeyError,
    OpenAIProvider,
    OpenAIRequestError,
)
from agent_runtime.provider import AssembledPrompt, PromptMessage

KEY_ENV = "OPENAI_API_KEY"
TEST_KEY = "test-key-not-real"  # unit-test fixture value; never a real credential
MODEL = "gpt-4o-mini-test-fixture"  # model ids in production come FROM THE SPEC

PROMPT = AssembledPrompt(
    system="You are the test persona.",
    messages=[
        PromptMessage(role="user", text="first user turn"),
        PromptMessage(role="assistant", text="[tool calls requested: local-demo.clock.now({})]"),
        PromptMessage(role="tool", text="[local-demo.clock.now -> ok] 12:00"),
        PromptMessage(role="user", text="and now?"),
    ],
)


class _StubState:
    """What the stub returns and what it last received (one request per test)."""

    def __init__(self) -> None:
        self.status = 200
        self.body: dict[str, Any] = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello from openai"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }
        self.raw_body: bytes | None = None  # override to send non-JSON
        self.captured: dict[str, Any] = {}

    def make_handler(self) -> type[BaseHTTPRequestHandler]:
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence the test log
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                payload = self.rfile.read(length)
                state.captured["path"] = self.path
                state.captured["authorization"] = self.headers.get("Authorization")
                state.captured["body"] = json.loads(payload)
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if state.raw_body is not None:
                    self.wfile.write(state.raw_body)
                else:
                    self.wfile.write(json.dumps(state.body).encode("utf-8"))

        return Handler


@pytest.fixture
def stub() -> Iterator[tuple[str, _StubState]]:
    state = _StubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), state.make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def _complete(base_url: str, prompt: AssembledPrompt, **kwargs: Any) -> Any:
    provider = OpenAIProvider(model=MODEL, base_url=base_url, **kwargs)
    try:
        return await provider.complete(prompt)
    finally:
        await provider.aclose()


def test_request_body_shape_message_mapping_and_bearer_header(
    stub: tuple[str, _StubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /v1/chat/completions with a Bearer key + {model, messages}; the tool
    turn rides as a user turn (the anthropic/ollama convention)."""
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    base_url, state = stub
    asyncio.run(_complete(base_url, PROMPT))

    assert state.captured["path"] == "/v1/chat/completions"
    assert state.captured["authorization"] == f"Bearer {TEST_KEY}"
    body = state.captured["body"]
    assert body["model"] == MODEL
    assert "max_tokens" not in body  # no maxTokens set -> omitted
    # a leading system turn, then the history; the tool turn rides as a user turn
    assert [m["role"] for m in body["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert body["messages"][0]["content"] == "You are the test persona."
    assert body["messages"][3]["content"] == "[local-demo.clock.now -> ok] 12:00"


def test_max_tokens_maps_to_max_tokens(
    stub: tuple[str, _StubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    base_url, state = stub
    asyncio.run(_complete(base_url, PROMPT, max_tokens=256))
    assert state.captured["body"]["max_tokens"] == 256


def test_response_parsing_content_and_token_counts(
    stub: tuple[str, _StubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """.choices[0].message.content -> text; usage.prompt/completion -> tokens."""
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    base_url, state = stub
    state.body = {
        "id": "chatcmpl-test",
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "the api replies"}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 13},
    }
    reply = asyncio.run(_complete(base_url, PROMPT))
    assert reply.text == "the api replies"
    assert reply.tokens_in == 42
    assert reply.tokens_out == 13
    assert reply.tool_calls == ()


def test_missing_token_counts_default_to_zero(
    stub: tuple[str, _StubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    base_url, state = stub
    state.body = {
        "id": "chatcmpl-test",
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
    reply = asyncio.run(_complete(base_url, PROMPT))
    assert reply.tokens_in == 0 and reply.tokens_out == 0


def test_non_200_raises_typed_error(
    stub: tuple[str, _StubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4xx (e.g. unknown model) fails HARD with a typed error, no retry."""
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    base_url, state = stub
    state.status = 404
    state.body = {"error": {"type": "invalid_request_error", "message": "model not found"}}
    with pytest.raises(OpenAIRequestError) as excinfo:
        asyncio.run(_complete(base_url, PROMPT))
    assert excinfo.value.status_code == 404
    assert "not found" in str(excinfo.value)


def test_malformed_200_body_raises_typed_error(
    stub: tuple[str, _StubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 whose body lacks 'choices' is malformed -> typed error."""
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    base_url, state = stub
    state.body = {"id": "chatcmpl-test", "model": MODEL}  # no 'choices'
    with pytest.raises(OpenAIRequestError, match="malformed"):
        asyncio.run(_complete(base_url, PROMPT))


def test_non_json_200_body_raises_typed_error(
    stub: tuple[str, _StubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    base_url, state = stub
    state.raw_body = b"<html>not json</html>"
    with pytest.raises(OpenAIRequestError, match="not JSON"):
        asyncio.run(_complete(base_url, PROMPT))


def test_missing_api_key_fails_loudly_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The anthropic posture: an empty/absent key refuses to construct, naming
    the env var — the eager boot guard, not a lazily-failing half-booted agent."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(MissingApiKeyError, match=KEY_ENV):
        OpenAIProvider(model=MODEL)
    monkeypatch.delenv("MY_OPENAI_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="MY_OPENAI_KEY"):
        OpenAIProvider(model=MODEL, api_key_env="MY_OPENAI_KEY")


def test_key_never_appears_in_request_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"type": "authentication_error", "message": "invalid key"}}
        )

    provider = OpenAIProvider(model=MODEL, transport=httpx.MockTransport(handler))
    with pytest.raises(OpenAIRequestError) as excinfo:
        asyncio.run(provider.complete(PROMPT))
    assert excinfo.value.status_code == 401
    assert TEST_KEY not in str(excinfo.value)
    asyncio.run(provider.aclose())


def test_5xx_retries_with_backoff_then_exhausts(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx is retried with capped exponential backoff (MockTransport keeps the
    backoff clock injectable and the retry count exact)."""
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = OpenAIProvider(
        model=MODEL,
        transport=httpx.MockTransport(handler),
        max_retries=2,
        backoff_base=0.25,
        sleep=record_sleep,
    )
    with pytest.raises(OpenAIRequestError) as excinfo:
        asyncio.run(provider.complete(PROMPT))
    assert len(calls) == 3, "initial attempt + 2 retries"
    assert sleeps == [0.25, 0.5], "capped exponential backoff"
    assert excinfo.value.status_code == 503
    asyncio.run(provider.aclose())


def test_429_retries_honoring_retry_after_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, json={"error": {}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = OpenAIProvider(
        model=MODEL, transport=httpx.MockTransport(handler), sleep=record_sleep
    )
    reply = asyncio.run(provider.complete(PROMPT))
    assert reply.text == "ok"
    assert len(calls) == 2
    assert sleeps == [3.0], "Retry-After (3s > backoff base) must be honored"
    asyncio.run(provider.aclose())


def test_4xx_fails_hard_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    provider = OpenAIProvider(model=MODEL, transport=httpx.MockTransport(handler), max_retries=3)
    with pytest.raises(OpenAIRequestError):
        asyncio.run(provider.complete(PROMPT))
    assert len(calls) == 1, "4xx must never be retried"
    asyncio.run(provider.aclose())

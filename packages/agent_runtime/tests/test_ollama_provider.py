"""Ollama adapter unit tests — hermetic, against a stdlib stub HTTP server.

The hand-rolled adapter (ADR 0006 — no SDK, no API key) is driven against a
real stdlib ``http.server`` in a background thread (the egress test's stub
pattern), so the whole httpx request/response path is exercised without a
network, a key, or a real Ollama server. Nothing here touches CI's composition
path (which stays on the static provider).
"""

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from agent_runtime.components.ollama_provider import OllamaProvider, OllamaRequestError
from agent_runtime.provider import AssembledPrompt, PromptMessage

MODEL = "llama3.2:test-fixture"  # model ids in production come FROM THE SPEC

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
            "model": MODEL,
            "message": {"role": "assistant", "content": "hello from ollama"},
            "done": True,
            "prompt_eval_count": 11,
            "eval_count": 7,
        }
        self.raw_body: bytes | None = None  # override to send non-JSON
        self.captured: dict[str, Any] = {}


def _make_handler(state: _StubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence the test log
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            payload = self.rfile.read(length)
            state.captured["path"] = self.path
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
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
    provider = OllamaProvider(model=MODEL, base_url=base_url, **kwargs)
    try:
        return await provider.complete(prompt)
    finally:
        await provider.aclose()


def test_request_body_shape_and_message_mapping(stub: tuple[str, _StubState]) -> None:
    """POST /api/chat with {model, messages (system + role/content), stream:false}."""
    base_url, state = stub
    asyncio.run(_complete(base_url, PROMPT))

    assert state.captured["path"] == "/api/chat"
    body = state.captured["body"]
    assert body["model"] == MODEL
    assert body["stream"] is False
    assert "options" not in body  # no maxTokens set -> no num_predict
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


def test_max_tokens_maps_to_num_predict(stub: tuple[str, _StubState]) -> None:
    base_url, state = stub
    asyncio.run(_complete(base_url, PROMPT, max_tokens=256))
    assert state.captured["body"]["options"] == {"num_predict": 256}


def test_response_parsing_content_and_token_counts(stub: tuple[str, _StubState]) -> None:
    """.message.content -> text; prompt_eval_count/eval_count -> token usage."""
    base_url, state = stub
    state.body = {
        "model": MODEL,
        "message": {"role": "assistant", "content": "the local model replies"},
        "done": True,
        "prompt_eval_count": 42,
        "eval_count": 13,
    }
    reply = asyncio.run(_complete(base_url, PROMPT))
    assert reply.text == "the local model replies"
    assert reply.tokens_in == 42
    assert reply.tokens_out == 13
    assert reply.tool_calls == ()


def test_missing_token_counts_default_to_zero(stub: tuple[str, _StubState]) -> None:
    base_url, state = stub
    state.body = {"model": MODEL, "message": {"role": "assistant", "content": "hi"}, "done": True}
    reply = asyncio.run(_complete(base_url, PROMPT))
    assert reply.tokens_in == 0 and reply.tokens_out == 0


def test_non_200_raises_typed_error(stub: tuple[str, _StubState]) -> None:
    """A 4xx (e.g. unknown model) fails HARD with a typed error, no retry."""
    base_url, state = stub
    state.status = 404
    state.body = {"error": 'model "llama3.2:test-fixture" not found'}
    with pytest.raises(OllamaRequestError) as excinfo:
        asyncio.run(_complete(base_url, PROMPT))
    assert excinfo.value.status_code == 404
    assert "not found" in str(excinfo.value)


def test_malformed_200_body_raises_typed_error(stub: tuple[str, _StubState]) -> None:
    """A 200 whose body lacks a 'message' object is malformed -> typed error."""
    base_url, state = stub
    state.body = {"model": MODEL, "done": True}  # no 'message'
    with pytest.raises(OllamaRequestError, match="malformed"):
        asyncio.run(_complete(base_url, PROMPT))


def test_non_json_200_body_raises_typed_error(stub: tuple[str, _StubState]) -> None:
    base_url, state = stub
    state.raw_body = b"<html>not json</html>"
    with pytest.raises(OllamaRequestError, match="not JSON"):
        asyncio.run(_complete(base_url, PROMPT))


def test_no_api_key_is_ever_required_or_sent(stub: tuple[str, _StubState]) -> None:
    """ADR 0006: Ollama takes no key — construction never reads the env and no
    auth header is sent (the stub would still answer regardless; this pins that
    the adapter carries no credential machinery)."""
    base_url, _state = stub
    # Constructs and completes with NO key in the environment — never raises a
    # missing-key error the way the anthropic adapter would.
    reply = asyncio.run(_complete(base_url, PROMPT))
    assert reply.text == "hello from ollama"


def test_5xx_retries_with_backoff_then_exhausts() -> None:
    """5xx is retried with capped exponential backoff (MockTransport keeps the
    backoff clock injectable and the retry count exact)."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": "overloaded"})

    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = OllamaProvider(
        model=MODEL,
        transport=httpx.MockTransport(handler),
        max_retries=2,
        backoff_base=0.25,
        sleep=record_sleep,
    )
    with pytest.raises(OllamaRequestError) as excinfo:
        asyncio.run(provider.complete(PROMPT))
    assert len(calls) == 3, "initial attempt + 2 retries"
    assert sleeps == [0.25, 0.5], "capped exponential backoff"
    assert excinfo.value.status_code == 503
    asyncio.run(provider.aclose())


def test_4xx_fails_hard_without_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": "bad request"})

    provider = OllamaProvider(model=MODEL, transport=httpx.MockTransport(handler), max_retries=3)
    with pytest.raises(OllamaRequestError):
        asyncio.run(provider.complete(PROMPT))
    assert len(calls) == 1, "4xx must never be retried"
    asyncio.run(provider.aclose())

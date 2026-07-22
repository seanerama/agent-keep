"""Ollama /api/chat adapter — hand-rolled on httpx, NO vendor SDK, NO API key.

ADR 0006: Ollama is the first provider-agnostic adapter (issue #15) — a LOCAL
inference server the worker reaches THROUGH the audited egress proxy, exactly
like the cloud (anthropic) path, so every model call stays allowlist-enforced
and audited even though the endpoint is local. This module mirrors
``components/anthropic_provider.py`` (ADR 0003, factory decision 3: the model
path is hand-rolled and auditable, not an SDK); httpx is the only dependency
and is version-pinned into the image by the composer.

Behavior notes:

- **No API key.** Ollama takes none; there is no ``apiKeyEnv``. The base host
  comes from the spec (``models.ollama.baseHost`` / ``models.tiers[].ollama``);
  the adapter builds ``http://<baseHost>`` and this module hardcodes no host or
  model name.
- **Streaming is NOT implemented.** Every call is a single non-streaming
  ``POST /api/chat`` with ``"stream": false``; long completions ride the
  request timeout. Streaming is a later, additive change behind the same seam.
- **Request mapping:** ``AssembledPrompt.system`` -> a leading ``system`` role
  message; prompt messages map role/content onto Ollama chat turns. The core's
  tool loop flattens tool rounds into TEXT turns (an assistant "[tool calls
  requested: ...]" turn and one ``role: "tool"`` turn per result); Ollama has
  no distinct tool role in this shape, so ``tool`` turns ride as ``user`` text
  turns (the anthropic adapter's convention). ``maxTokens`` maps to
  ``options.num_predict`` only when the spec sets it.
- **Response mapping:** the assistant text is ``.message.content``; token usage
  is ``.prompt_eval_count`` (input) / ``.eval_count`` (output) — recorded into
  the same ``ProviderReply`` shape the audit plane reads, so token accounting
  is identical to every other provider. Tool calls are not requested this
  stage; ``tool_calls`` is always empty.
- **Retries:** 5xx and transport errors are retried with capped exponential
  backoff, honoring ``Retry-After`` when present (still capped). Any 4xx fails
  HARD with no retry. A non-200 or a malformed 200 body raises a typed
  ``OllamaRequestError``; the core writes the stage-5 error audit record
  (model_call / outcome error) before re-raising.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent_runtime.provider import AssembledPrompt, ProviderReply

logger = logging.getLogger(__name__)

#: The worker's Ollama base URL. The host must match the spec's
#: models.ollama.baseHost (the egress cross-validation reads the host from the
#: CONFIG, not this constant — ADR 0006). Default is the host's Ollama over the
#: docker gateway, reached through the egress proxy.
DEFAULT_BASE_URL = "http://host.docker.internal:11434"
CHAT_PATH = "/api/chat"
DEFAULT_TIMEOUT_SECONDS = 120.0
#: Retry policy: up to MAX_RETRIES re-sends after the first attempt, delay =
#: min(max(BACKOFF_BASE * 2**attempt, Retry-After), BACKOFF_CAP).
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0


class OllamaRequestError(RuntimeError):
    """A non-retryable Ollama failure, retries exhausted, or a malformed reply."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"ollama API request failed: HTTP {status_code} — {detail}")
        self.status_code = status_code


def _error_detail(response: httpx.Response) -> str:
    """Best-effort error message from Ollama's error envelope (``{"error": ...}``)."""
    try:
        error = response.json().get("error")
        if error:
            return str(error)
    except ValueError:
        pass
    return response.text[:200]


class OllamaProvider:
    """ModelProvider implementation for the Ollama /api/chat endpoint.

    ``transport`` exists so unit tests can substitute ``httpx.MockTransport``
    at the adapter boundary; the hermetic unit test also drives it against a
    stdlib stub HTTP server. ``sleep`` injects the backoff clock for tests; it
    never alters what is sent or received.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        max_tokens: int | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        backoff_cap: float = BACKOFF_CAP_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        #: Audit action name — distinguishes tiers ("ollama:<spec model>").
        self.name = f"ollama:{model}"
        self._model = model
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout_seconds,
            headers={"content-type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- request side

    def _payload(self, prompt: AssembledPrompt) -> dict[str, Any]:
        messages: list[dict[str, str]] = [{"role": "system", "content": prompt.system}]
        messages += [
            # 'tool' turns (executor results, flattened to text by the core)
            # ride as user turns — see the module docstring.
            {"role": "user" if m.role == "tool" else m.role, "content": m.text}
            for m in prompt.messages
        ]
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if self._max_tokens is not None:
            payload["options"] = {"num_predict": self._max_tokens}
        return payload

    # ------------------------------------------------------------ response side

    @staticmethod
    def _reply(data: dict[str, Any]) -> ProviderReply:
        message = data.get("message")
        if not isinstance(message, dict):
            raise OllamaRequestError(200, "malformed response: missing or non-object 'message'")
        content = message.get("content", "")
        return ProviderReply(
            text=content if isinstance(content, str) else "",
            tokens_in=int(data.get("prompt_eval_count", 0) or 0),
            tokens_out=int(data.get("eval_count", 0) or 0),
        )

    # ------------------------------------------------------------- retry engine

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        delay: float = self._backoff_base * (2.0**attempt)
        if retry_after is not None:
            try:
                # Honor the server's ask (seconds form); the cap still applies.
                delay = max(delay, float(retry_after))
            except ValueError:
                pass  # HTTP-date form — keep the computed backoff
        return min(delay, self._backoff_cap)

    async def complete(self, prompt: AssembledPrompt) -> ProviderReply:
        payload = self._payload(prompt)
        attempt = 0
        while True:
            try:
                response = await self._client.post(CHAT_PATH, json=payload)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, None)
                    logger.warning(
                        "ollama transport error (%s) — retry %d/%d in %.1fs",
                        type(exc).__name__,
                        attempt + 1,
                        self._max_retries,
                        delay,
                    )
                    await self._sleep(delay)
                    attempt += 1
                    continue
                raise
            if response.status_code == 200:
                try:
                    data: dict[str, Any] = response.json()
                except ValueError as exc:
                    raise OllamaRequestError(200, "malformed response: not JSON") from exc
                return self._reply(data)
            retryable = response.status_code >= 500
            if retryable and attempt < self._max_retries:
                delay = self._retry_delay(attempt, response.headers.get("retry-after"))
                logger.warning(
                    "ollama API HTTP %d — retry %d/%d in %.1fs",
                    response.status_code,
                    attempt + 1,
                    self._max_retries,
                    delay,
                )
                await self._sleep(delay)
                attempt += 1
                continue
            # Hard failure: any 4xx immediately, or a retryable status with
            # retries exhausted. The core writes the error audit record when
            # this propagates (stage 5).
            raise OllamaRequestError(response.status_code, _error_detail(response))

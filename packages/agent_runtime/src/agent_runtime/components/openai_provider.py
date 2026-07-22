"""OpenAI Chat Completions API adapter — hand-rolled on httpx, NO vendor SDK.

ADR 0003, factory decision 3: the model path is the most security-sensitive
dependency surface, so the adapter is a thin, auditable, hand-rolled HTTP
client (blueprint model/llmrouter) rather than an SDK. httpx is the only
dependency and is version-pinned into the image by the composer. This module
is the second provider-agnostic cloud adapter (issue #15) — the anthropic-
shaped variant of the stage-8 ollama pattern: a cloud provider that needs an
API key and egress to `api.openai.com`, reached the same way every model call
is (allowlist-enforced + audited), just against a configurable `baseHost` so
OpenAI-compatible endpoints work too.

Behavior notes:

- **API key** comes from the env var NAMED by the spec
  (``models.openai.apiKeyEnv``, default ``OPENAI_API_KEY``) at construction
  time — a missing key fails the boot loudly, naming the var (the anthropic
  posture, NOT ollama's keyless one). The key value is never logged and never
  reaches the audit sink (the core records digests of prompts, never headers
  or payloads).
- **Base host comes from the spec** (``models.openai.baseHost`` /
  ``models.tiers[].openai``); the runner builds ``https://<baseHost>`` and this
  module hardcodes no host or model name. The default base URL below is only
  the adapter's own fallback and is kept in sync with the egress cross-check.
- **Streaming is NOT implemented this stage.** Every call is a single
  non-streaming ``POST /v1/chat/completions``; long completions ride the
  request timeout. Streaming is a later, additive change behind the same seam.
- **Request mapping:** ``AssembledPrompt.system`` -> a leading ``system`` role
  message; prompt messages map role/content onto OpenAI chat turns. The core's
  stage-6 tool loop flattens tool rounds into TEXT turns (an assistant "[tool
  calls requested: ...]" turn and one ``role: "tool"`` turn per result); an
  OpenAI ``tool`` role message requires a ``tool_call_id`` those flattened text
  turns do not carry, so ``tool`` turns ride as ``user`` text turns — exactly
  the anthropic/ollama convention (consecutive same-role turns are legal).
  Replaying structured tool turns requires the core to carry them and is
  deferred — documented, not silent. ``maxTokens`` maps to ``max_tokens`` only
  when the spec sets it.
- **Response mapping:** the assistant text is ``.choices[0].message.content``;
  token usage is ``.usage.prompt_tokens`` (input) / ``.usage.completion_tokens``
  (output) — recorded into the SAME ``ProviderReply`` shape the audit plane
  reads, so token accounting is identical to every other provider. Tool calls
  are not requested this stage; ``tool_calls`` is always empty.
- **Retries:** 429 and 5xx and transport errors are retried with capped
  exponential backoff, honoring ``Retry-After`` when the server sends one
  (still capped). Any other 4xx — auth errors above all — fails HARD with no
  retry. A non-200 or a malformed 200 body raises a typed
  ``OpenAIRequestError``; the core writes the stage-5 error audit record
  (model_call / outcome error) before re-raising.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent_runtime.provider import AssembledPrompt, ProviderReply

logger = logging.getLogger(__name__)

#: OpenAI REST API. The host must match the spec's models.openai.baseHost (the
#: egress cross-validation reads the host from the CONFIG, not this constant —
#: the ollama pattern, ADR 0006). Default is the public OpenAI API.
DEFAULT_BASE_URL = "https://api.openai.com"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
DEFAULT_TIMEOUT_SECONDS = 120.0
#: Retry policy: up to MAX_RETRIES re-sends after the first attempt, delay =
#: min(max(BACKOFF_BASE * 2**attempt, Retry-After), BACKOFF_CAP).
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0


class MissingApiKeyError(RuntimeError):
    """The spec-named API key env var is absent — refuse to construct/boot."""


class OpenAIRequestError(RuntimeError):
    """A non-retryable API failure, retries exhausted, or a malformed reply.

    Never carries the key.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"openai API request failed: HTTP {status_code} — {detail}")
        self.status_code = status_code


def _error_detail(response: httpx.Response) -> str:
    """Best-effort error type/message from the API error envelope (no payload echo)."""
    try:
        error = response.json().get("error", {})
        if isinstance(error, dict):
            detail = f"{error.get('type', 'unknown_error')}: {error.get('message', '')}".strip(": ")
            if detail:
                return detail
        elif error:
            return str(error)
    except ValueError:
        pass
    return response.text[:200]


class OpenAIProvider:
    """ModelProvider implementation for the OpenAI Chat Completions API.

    ``transport`` exists so unit tests can substitute ``httpx.MockTransport``
    at the adapter boundary — the ONE sanctioned mock seam (ADR 0004's
    alternatives note); CI composition paths never construct this class. The
    hermetic unit test also drives it against a stdlib stub HTTP server.
    ``sleep`` injects the backoff clock for tests; it never alters what is
    sent or received.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = DEFAULT_BASE_URL,
        max_tokens: int | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        backoff_cap: float = BACKOFF_CAP_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise MissingApiKeyError(
                f"model provider 'openai' requires the API key env var "
                f"'{api_key_env}' (spec: models.openai.apiKeyEnv) — inject it at "
                f"run time (e.g. docker run -e {api_key_env}=...). The key value "
                "never appears in the spec, the image, logs, or audit records."
            )
        #: Audit action name — distinguishes tiers ("openai:<spec model>").
        self.name = f"openai:{model}"
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
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- request side

    def _payload(self, prompt: AssembledPrompt) -> dict[str, Any]:
        messages: list[dict[str, str]] = [{"role": "system", "content": prompt.system}]
        messages += [
            # 'tool' turns (executor results, flattened to text by the core)
            # ride as user turns — see the module docstring (an OpenAI tool-role
            # message needs a tool_call_id these flattened text turns lack).
            {"role": "user" if m.role == "tool" else m.role, "content": m.text}
            for m in prompt.messages
        ]
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        return payload

    # ------------------------------------------------------------ response side

    @staticmethod
    def _reply(data: dict[str, Any]) -> ProviderReply:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenAIRequestError(200, "malformed response: missing or empty 'choices'")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise OpenAIRequestError(200, "malformed response: missing or non-object 'message'")
        content = message.get("content", "")
        usage = data.get("usage") or {}
        return ProviderReply(
            text=content if isinstance(content, str) else "",
            tokens_in=int(usage.get("prompt_tokens", 0) or 0),
            tokens_out=int(usage.get("completion_tokens", 0) or 0),
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
                response = await self._client.post(CHAT_COMPLETIONS_PATH, json=payload)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, None)
                    logger.warning(
                        "openai transport error (%s) — retry %d/%d in %.1fs",
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
                    raise OpenAIRequestError(200, "malformed response: not JSON") from exc
                return self._reply(data)
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._max_retries:
                delay = self._retry_delay(attempt, response.headers.get("retry-after"))
                logger.warning(
                    "openai API HTTP %d — retry %d/%d in %.1fs",
                    response.status_code,
                    attempt + 1,
                    self._max_retries,
                    delay,
                )
                await self._sleep(delay)
                attempt += 1
                continue
            # Hard failure: any other 4xx (401/403 auth above all) immediately,
            # or a retryable status with retries exhausted. The core writes the
            # error audit record when this propagates (stage 5).
            raise OpenAIRequestError(response.status_code, _error_detail(response))

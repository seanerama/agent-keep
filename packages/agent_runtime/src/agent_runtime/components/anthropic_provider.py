"""Anthropic Messages API adapter — hand-rolled on httpx, NO vendor SDK.

ADR 0003, factory decision 3: the model path is the most security-sensitive
dependency surface, so the adapter is a thin, auditable, hand-rolled HTTP
client (blueprint model/llmrouter) rather than an SDK. httpx is the only
dependency and is version-pinned into the image by the composer.

Behavior notes (stage 9):

- **Streaming is NOT implemented this stage.** Every call is a single
  non-streaming ``POST /v1/messages``; long completions ride the request
  timeout. Streaming is a later, additive change behind the same seam.
- **API key** comes from the env var NAMED by the spec
  (``models.anthropic.apiKeyEnv``, default ``ANTHROPIC_API_KEY``) at
  construction time — a missing key fails the boot loudly, naming the var.
  The key value is never logged and never reaches the audit sink (the core
  records digests of prompts, never headers or payloads).
- **Model names come from the spec** (``models.anthropic.model`` /
  ``models.tiers[].anthropic.model``); this module hardcodes none.
- **Request mapping:** ``AssembledPrompt.system`` -> ``system``; prompt
  messages -> Messages API turns. The core's stage-6 tool loop flattens tool
  rounds into TEXT turns (an assistant "[tool calls requested: ...]" turn and
  one ``role: "tool"`` turn per result), so this adapter sends ``tool`` turns
  as ``user`` text turns (the API has no tool role; consecutive same-role
  turns are legal and merged server-side). Replaying structured
  ``tool_use``/``tool_result`` blocks requires the core to carry structured
  turns and is deferred — documented, not silent.
- **Tool definitions:** ``AssembledPrompt.tools`` (granted tools only —
  absence semantics upstream) map to Anthropic ``tools`` entries. Spec tool
  names are '<server>.<tool>' and may contain dots the API's tool-name
  grammar rejects; a per-request bijective sanitization maps wire names back
  to spec names on the response's ``tool_use`` blocks.
- **Response mapping:** ``text`` blocks concatenate into ``ProviderReply.text``;
  ``tool_use`` blocks become ``ToolCallRequest``s (API-issued ids preserved);
  ``usage.input_tokens/output_tokens`` become the cost the core audits.
- **Retries:** 429 and 5xx (incl. 529 overloaded) and transport errors are
  retried with capped exponential backoff, honoring ``Retry-After`` when the
  server sends one (still capped). Any other 4xx — auth errors above all —
  fails HARD with no retry. A final failure propagates to the core, which
  writes the stage-5 error audit record (model_call / outcome error) before
  re-raising.
"""

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent_runtime.provider import AssembledPrompt, ProviderReply, ToolCallRequest

logger = logging.getLogger(__name__)

API_VERSION = "2023-06-01"
#: Anthropic REST API. The host must match wiring.ANTHROPIC_API_HOST (the
#: egress cross-validation constant, stage 19 #50) — keep the two in sync.
DEFAULT_BASE_URL = "https://api.anthropic.com"
MESSAGES_PATH = "/v1/messages"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 120.0
#: Retry policy: up to MAX_RETRIES re-sends after the first attempt, delay =
#: min(max(BACKOFF_BASE * 2**attempt, Retry-After), BACKOFF_CAP).
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0

#: Anthropic tool-name grammar: ^[a-zA-Z0-9_-]{1,64}$ — spec names
#: ('<server>.<tool>') are sanitized onto it bijectively per request.
_TOOL_NAME_BAD_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


class MissingApiKeyError(RuntimeError):
    """The spec-named API key env var is absent — refuse to construct/boot."""


class AnthropicRequestError(RuntimeError):
    """A non-retryable API failure, or retries exhausted. Never carries the key."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"anthropic API request failed: HTTP {status_code} — {detail}")
        self.status_code = status_code


def _error_detail(response: httpx.Response) -> str:
    """Best-effort error type/message from the API error envelope (no payload echo)."""
    try:
        error = response.json().get("error", {})
        detail = f"{error.get('type', 'unknown_error')}: {error.get('message', '')}".strip(": ")
        if detail:
            return detail
    except ValueError:
        pass
    return response.text[:200]


class AnthropicProvider:
    """ModelProvider implementation for the Anthropic Messages API.

    ``transport`` exists so unit tests can substitute ``httpx.MockTransport``
    at the adapter boundary — the ONE sanctioned mock seam (ADR 0004's
    alternatives note); CI composition paths never construct this class.
    ``sleep`` injects the backoff clock for tests; it never alters what is
    sent or received.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key_env: str = "ANTHROPIC_API_KEY",
        base_url: str = DEFAULT_BASE_URL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
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
                f"model provider 'anthropic' requires the API key env var "
                f"'{api_key_env}' (spec: models.anthropic.apiKeyEnv) — inject it at "
                f"run time (e.g. docker run -e {api_key_env}=...). The key value "
                "never appears in the spec, the image, logs, or audit records."
            )
        #: Audit action name — distinguishes tiers ("anthropic:<spec model>").
        self.name = f"anthropic:{model}"
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
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- request side

    def _wire_tools(self, prompt: AssembledPrompt) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Granted tools -> Anthropic tool definitions + wire-name -> spec-name map."""
        tools: list[dict[str, Any]] = []
        wire_to_spec: dict[str, str] = {}
        for tool in prompt.tools:
            wire = _TOOL_NAME_BAD_CHARS.sub("_", tool.name)[:64]
            if wire in wire_to_spec:
                raise ValueError(
                    f"tool names {wire_to_spec[wire]!r} and {tool.name!r} collide after "
                    f"API tool-name sanitization ({wire!r}) — rename one grant"
                )
            wire_to_spec[wire] = tool.name
            tools.append(
                {
                    "name": wire,
                    "description": tool.description,
                    "input_schema": {"type": "object", "properties": tool.parameters},
                }
            )
        return tools, wire_to_spec

    def _payload(self, prompt: AssembledPrompt) -> tuple[dict[str, Any], dict[str, str]]:
        messages = [
            # 'tool' turns (executor results, flattened to text by the core)
            # ride as user turns — see the module docstring.
            {"role": "user" if m.role == "tool" else m.role, "content": m.text}
            for m in prompt.messages
        ]
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": prompt.system,
            "messages": messages,
        }
        tools, wire_to_spec = self._wire_tools(prompt)
        if tools:
            payload["tools"] = tools
        return payload, wire_to_spec

    # ------------------------------------------------------------ response side

    @staticmethod
    def _reply(data: dict[str, Any], wire_to_spec: dict[str, str]) -> ProviderReply:
        text_parts: list[str] = []
        calls: list[ToolCallRequest] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                wire_name = block.get("name", "")
                calls.append(
                    ToolCallRequest(
                        id=block.get("id", ""),
                        name=wire_to_spec.get(wire_name, wire_name),
                        arguments=block.get("input") or {},
                    )
                )
        usage = data.get("usage") or {}
        return ProviderReply(
            text="\n".join(text_parts),
            tokens_in=int(usage.get("input_tokens", 0)),
            tokens_out=int(usage.get("output_tokens", 0)),
            tool_calls=tuple(calls),
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
        payload, wire_to_spec = self._payload(prompt)
        attempt = 0
        while True:
            try:
                response = await self._client.post(MESSAGES_PATH, json=payload)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, None)
                    logger.warning(
                        "anthropic transport error (%s) — retry %d/%d in %.1fs",
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
                data: dict[str, Any] = response.json()
                return self._reply(data, wire_to_spec)
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._max_retries:
                delay = self._retry_delay(attempt, response.headers.get("retry-after"))
                logger.warning(
                    "anthropic API HTTP %d — retry %d/%d in %.1fs",
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
            raise AnthropicRequestError(response.status_code, _error_detail(response))

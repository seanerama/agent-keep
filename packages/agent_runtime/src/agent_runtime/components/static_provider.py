"""Static model provider (ADR 0004) — deterministic, scriptable, hermetic.

A first-class implementation of the provider interface: call N returns
script[N % len(script)]. No network, no secrets, no nondeterminism — the
container that passes CI is honest about exercising the full message path.

Scriptable tool-call turns (stage 6, additive): a script entry beginning with
``TOOL_CALL `` followed by JSON — one object ``{"name": ..., "arguments": {...}}``
or a list of them — is returned as tool-call requests instead of text, with
deterministic call ids. The spec schema is untouched (entries stay strings);
plain entries behave exactly as before.
"""

import json
from typing import Any

from agent_runtime.provider import AssembledPrompt, ProviderReply, ToolCallRequest

TOOL_CALL_PREFIX = "TOOL_CALL "


def _parse_tool_calls(entry: str, turn: int) -> tuple[ToolCallRequest, ...]:
    payload = json.loads(entry.removeprefix(TOOL_CALL_PREFIX))
    items: list[Any] = payload if isinstance(payload, list) else [payload]
    calls: list[ToolCallRequest] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError(
                f"static script TOOL_CALL entry {turn}: each call needs a string 'name'"
            )
        arguments = item.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"static script TOOL_CALL entry {turn}: 'arguments' must be an object")
        calls.append(
            ToolCallRequest(id=f"call-{turn}-{index}", name=item["name"], arguments=arguments)
        )
    return tuple(calls)


class StaticProvider:
    name = "static"

    def __init__(self, script: list[str]) -> None:
        if not script:
            raise ValueError("static provider requires a non-empty script")
        self._script = list(script)
        self._calls = 0

    async def complete(self, prompt: AssembledPrompt) -> ProviderReply:
        turn = self._calls
        entry = self._script[turn % len(self._script)]
        self._calls += 1
        tokens_in = len(prompt.system.split()) + sum(len(m.text.split()) for m in prompt.messages)
        if entry.startswith(TOOL_CALL_PREFIX):
            return ProviderReply(
                text="",
                tokens_in=tokens_in,
                tokens_out=len(entry.split()),
                tool_calls=_parse_tool_calls(entry, turn),
            )
        return ProviderReply(text=entry, tokens_in=tokens_in, tokens_out=len(entry.split()))

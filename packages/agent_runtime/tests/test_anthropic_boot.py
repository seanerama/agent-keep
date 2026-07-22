"""Boot-time key guard (stage 9): an anthropic-selecting spec boots ONLY when
the spec-named API key env var is present — otherwise build_app fails loudly,
naming exactly that var. No half-booted agent that fails on first message.

Stage 13 adds the spec-driven max_tokens wiring tests: the optional
`models.anthropic.maxTokens` spec field must reach the request body, and its
absence must preserve the adapter default of 4096 (regression pin).
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from agent_runtime.components import anthropic_provider as anthropic_provider_module
from agent_runtime.components.anthropic_provider import MissingApiKeyError
from agent_runtime.provider import AssembledPrompt, PromptMessage
from agent_runtime.runner import build_app
from keep_spec import AgentSpec, validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
SKELETON_SPEC = REPO_ROOT / "examples" / "skeleton.yaml"
VALID_DIGEST = "sha256:" + "ab" * 32
MODEL = "claude-test-fixture"  # test fixture — production model names come from the spec


def _anthropic_spec(
    tmp_path: Path, *, api_key_env: str | None = None, max_tokens: int | None = None
) -> AgentSpec:
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data["spec"]["observability"]["audit"]["path"] = str(tmp_path / "audit.jsonl")
    anthropic: dict[str, Any] = {"model": MODEL}
    if api_key_env is not None:
        anthropic["apiKeyEnv"] = api_key_env
    if max_tokens is not None:
        anthropic["maxTokens"] = max_tokens
    data["spec"]["models"] = {"provider": "anthropic", "anthropic": anthropic}
    # selecting the anthropic provider requires its API host in egress (stage 19, #50)
    data["spec"]["sandbox"]["egress"] = ["api.anthropic.com:443"]
    return validate_spec_data(data)


def _capture_request_body(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Route the spec-built adapter through a MockTransport, capturing the body.

    build_app constructs the provider via load_component; patching the module's
    class attribute injects the ONE sanctioned mock seam (ADR 0004 — transport
    at the adapter boundary) without touching the composition path itself.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    real_provider = anthropic_provider_module.AnthropicProvider

    def build_with_mock_transport(**kwargs: Any) -> Any:
        return real_provider(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(anthropic_provider_module, "AnthropicProvider", build_with_mock_transport)
    return captured


def test_missing_default_key_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
        build_app(_anthropic_spec(tmp_path))


def test_missing_spec_named_key_refuses_to_boot_naming_that_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.delenv("MY_ANTHROPIC_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="MY_ANTHROPIC_KEY"):
        build_app(_anthropic_spec(tmp_path, api_key_env="MY_ANTHROPIC_KEY"))


def test_present_key_boots_and_names_the_spec_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    core, _adapter = build_app(_anthropic_spec(tmp_path))
    # construction makes NO network call; the audit action name carries the
    # spec's model so tier accounting is legible in the append-only log
    assert core._provider.name == f"anthropic:{MODEL}"


def test_spec_max_tokens_reaches_the_request_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stage 13: `models.anthropic.maxTokens` wins over the adapter default."""
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    captured = _capture_request_body(monkeypatch)
    core, _adapter = build_app(_anthropic_spec(tmp_path, max_tokens=1024))
    asyncio.run(
        core._provider.complete(
            AssembledPrompt(system="p", messages=[PromptMessage(role="user", text="hi")])
        )
    )
    assert captured["body"]["max_tokens"] == 1024


def test_spec_without_max_tokens_keeps_the_4096_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression pin: an untouched spec keeps DEFAULT_MAX_TOKENS (4096)."""
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    captured = _capture_request_body(monkeypatch)
    core, _adapter = build_app(_anthropic_spec(tmp_path))
    asyncio.run(
        core._provider.complete(
            AssembledPrompt(system="p", messages=[PromptMessage(role="user", text="hi")])
        )
    )
    assert captured["body"]["max_tokens"] == 4096


def test_anthropic_tier_also_requires_its_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data["spec"]["observability"]["audit"]["path"] = str(tmp_path / "audit.jsonl")
    data["spec"]["models"]["tiers"] = [
        {"name": "message", "provider": "anthropic", "anthropic": {"model": MODEL}}
    ]
    data["spec"]["sandbox"]["egress"] = ["api.anthropic.com:443"]
    with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
        build_app(validate_spec_data(data))

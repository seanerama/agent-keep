"""Boot-time wiring for the openai provider (issue #15, stage 10): an
openai-selecting spec boots ONLY when the spec-named API key env var is present
(the anthropic posture — a cloud provider needs its key), constructs the adapter
naming the spec's model, points it at `https://<baseHost>`, and forwards
`maxTokens` -> Chat Completions `max_tokens` only when the spec sets it.

Hermetic: build_app constructs the adapter but makes NO network call; the
request-body assertions drive the spec-built adapter through the ONE sanctioned
mock transport seam (ADR 0004), exactly like the anthropic boot test. The key is
a test fixture, never a real credential.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from agent_runtime.components import openai_provider as openai_provider_module
from agent_runtime.components.openai_provider import MissingApiKeyError
from agent_runtime.provider import AssembledPrompt, PromptMessage
from agent_runtime.runner import build_app
from keep_spec import AgentSpec, validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
SKELETON_SPEC = REPO_ROOT / "examples" / "skeleton.yaml"
VALID_DIGEST = "sha256:" + "ab" * 32
MODEL = "gpt-4o-mini-test-fixture"  # test fixture — production model names come from the spec


def _openai_spec(
    tmp_path: Path,
    *,
    base_host: str = "api.openai.com:443",
    api_key_env: str | None = None,
    max_tokens: int | None = None,
) -> AgentSpec:
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data["spec"]["observability"]["audit"]["path"] = str(tmp_path / "audit.jsonl")
    openai: dict[str, Any] = {"model": MODEL, "baseHost": base_host}
    if api_key_env is not None:
        openai["apiKeyEnv"] = api_key_env
    if max_tokens is not None:
        openai["maxTokens"] = max_tokens
    data["spec"]["models"] = {"provider": "openai", "openai": openai}
    # selecting the openai provider requires its baseHost in egress
    data["spec"]["sandbox"]["egress"] = [base_host]
    return validate_spec_data(data)


def _capture_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "model": MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    real_provider = openai_provider_module.OpenAIProvider

    def build_with_mock_transport(**kwargs: Any) -> Any:
        return real_provider(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(openai_provider_module, "OpenAIProvider", build_with_mock_transport)
    return captured


def test_missing_default_key_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="OPENAI_API_KEY"):
        build_app(_openai_spec(tmp_path))


def test_missing_spec_named_key_refuses_to_boot_naming_that_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.delenv("MY_OPENAI_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="MY_OPENAI_KEY"):
        build_app(_openai_spec(tmp_path, api_key_env="MY_OPENAI_KEY"))


def test_present_key_boots_and_names_the_spec_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    core, _adapter = build_app(_openai_spec(tmp_path))
    # construction makes NO network call; the audit action name carries the
    # spec's model so tier accounting is legible in the append-only log
    assert core._provider.name == f"openai:{MODEL}"


def test_base_host_reaches_the_request_url_with_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    captured = _capture_request(monkeypatch)
    core, _adapter = build_app(_openai_spec(tmp_path, base_host="api.openai.com:443"))
    asyncio.run(
        core._provider.complete(
            AssembledPrompt(system="p", messages=[PromptMessage(role="user", text="hi")])
        )
    )
    # httpx normalizes away the default https port (:443) from the request URL.
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-key-not-real"
    assert captured["body"]["model"] == MODEL


def test_spec_max_tokens_reaches_the_request_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    captured = _capture_request(monkeypatch)
    core, _adapter = build_app(_openai_spec(tmp_path, max_tokens=1024))
    asyncio.run(
        core._provider.complete(
            AssembledPrompt(system="p", messages=[PromptMessage(role="user", text="hi")])
        )
    )
    assert captured["body"]["max_tokens"] == 1024


def test_spec_without_max_tokens_omits_the_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    captured = _capture_request(monkeypatch)
    core, _adapter = build_app(_openai_spec(tmp_path))
    asyncio.run(
        core._provider.complete(
            AssembledPrompt(system="p", messages=[PromptMessage(role="user", text="hi")])
        )
    )
    assert "max_tokens" not in captured["body"]


def test_openai_tier_also_requires_its_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data["spec"]["observability"]["audit"]["path"] = str(tmp_path / "audit.jsonl")
    data["spec"]["models"]["tiers"] = [
        {
            "name": "message",
            "provider": "openai",
            "openai": {"model": MODEL, "baseHost": "api.openai.com:443"},
        }
    ]
    data["spec"]["sandbox"]["egress"] = ["api.openai.com:443"]
    with pytest.raises(MissingApiKeyError, match="OPENAI_API_KEY"):
        build_app(validate_spec_data(data))

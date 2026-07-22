"""Boot-time wiring for the ollama provider (ADR 0006, stage 8): an
ollama-selecting spec boots with NO API key (Ollama takes none), constructs the
adapter naming the spec's model, points it at `http://<baseHost>`, and forwards
`maxTokens` -> Ollama `options.num_predict` only when the spec sets it.

Hermetic: build_app constructs the adapter but makes NO network call; the
request-body assertions drive the spec-built adapter through the ONE sanctioned
mock transport seam (ADR 0004), exactly like the anthropic boot test.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from agent_runtime.components import ollama_provider as ollama_provider_module
from agent_runtime.provider import AssembledPrompt, PromptMessage
from agent_runtime.runner import build_app
from keep_spec import AgentSpec, validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
SKELETON_SPEC = REPO_ROOT / "examples" / "skeleton.yaml"
VALID_DIGEST = "sha256:" + "ab" * 32
MODEL = "llama3.2:test-fixture"  # test fixture — production model names come from the spec


def _ollama_spec(
    tmp_path: Path,
    *,
    base_host: str = "host.docker.internal:11434",
    max_tokens: int | None = None,
) -> AgentSpec:
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data["spec"]["observability"]["audit"]["path"] = str(tmp_path / "audit.jsonl")
    ollama: dict[str, Any] = {"model": MODEL, "baseHost": base_host}
    if max_tokens is not None:
        ollama["maxTokens"] = max_tokens
    data["spec"]["models"] = {"provider": "ollama", "ollama": ollama}
    # selecting the ollama provider requires its baseHost in egress (ADR 0006)
    data["spec"]["sandbox"]["egress"] = [base_host]
    return validate_spec_data(data)


def _capture_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
                "prompt_eval_count": 1,
                "eval_count": 1,
            },
        )

    real_provider = ollama_provider_module.OllamaProvider

    def build_with_mock_transport(**kwargs: Any) -> Any:
        return real_provider(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(ollama_provider_module, "OllamaProvider", build_with_mock_transport)
    return captured


def test_boots_without_any_key_and_names_the_spec_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    # No OLLAMA_* key exists or is needed — construction makes no network call
    # and raises no missing-key error (the anthropic path's sharp contrast).
    core, _adapter = build_app(_ollama_spec(tmp_path))
    assert core._provider.name == f"ollama:{MODEL}"


def test_base_host_reaches_the_request_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    captured = _capture_request(monkeypatch)
    core, _adapter = build_app(_ollama_spec(tmp_path, base_host="host.docker.internal:11434"))
    asyncio.run(
        core._provider.complete(
            AssembledPrompt(system="p", messages=[PromptMessage(role="user", text="hi")])
        )
    )
    assert captured["url"] == "http://host.docker.internal:11434/api/chat"
    assert captured["body"]["model"] == MODEL
    assert captured["body"]["stream"] is False


def test_spec_max_tokens_reaches_num_predict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    captured = _capture_request(monkeypatch)
    core, _adapter = build_app(_ollama_spec(tmp_path, max_tokens=1024))
    asyncio.run(
        core._provider.complete(
            AssembledPrompt(system="p", messages=[PromptMessage(role="user", text="hi")])
        )
    )
    assert captured["body"]["options"] == {"num_predict": 1024}


def test_spec_without_max_tokens_omits_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    captured = _capture_request(monkeypatch)
    core, _adapter = build_app(_ollama_spec(tmp_path))
    asyncio.run(
        core._provider.complete(
            AssembledPrompt(system="p", messages=[PromptMessage(role="user", text="hi")])
        )
    )
    assert "options" not in captured["body"]


def test_ollama_tier_also_boots_without_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data["spec"]["observability"]["audit"]["path"] = str(tmp_path / "audit.jsonl")
    data["spec"]["models"]["tiers"] = [
        {
            "name": "message",
            "provider": "ollama",
            "ollama": {"model": MODEL, "baseHost": "host.docker.internal:11434"},
        }
    ]
    data["spec"]["sandbox"]["egress"] = ["host.docker.internal:11434"]
    core, _adapter = build_app(validate_spec_data(data))
    # the router ships (a tier is declared) and the tier provider is the adapter
    assert core._provider.name.startswith("static")  # default provider unchanged

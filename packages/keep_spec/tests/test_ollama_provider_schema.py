"""Schema tests for the additive `ollama` model provider (ADR 0006, stage 8).

Additive-only surface: a new `models.ollama` config block plus `ollama` in the
provider enum of BOTH `models` and `models.tiers[]`. These tests pin the
cross-field rules (`_check_provider_config` + baseHost grammar) exactly as the
anthropic/static provider tests pin theirs.
"""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from keep_spec import load_spec, validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
OLLAMA_SPEC = REPO_ROOT / "specs" / "default-chatbot.ollama.yaml"


@pytest.fixture
def ollama_data() -> dict[str, Any]:
    with open(OLLAMA_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def _rejects(data: dict[str, Any], *message_parts: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_spec_data(data)
    text = str(excinfo.value)
    for part in message_parts:
        assert part in text, f"expected {part!r} in validation error:\n{text}"


def test_ollama_spec_validates_strictly() -> None:
    spec = load_spec(OLLAMA_SPEC)
    models = spec.spec.models
    assert models.provider == "ollama"
    assert models.ollama is not None
    assert models.ollama.model == "llama3.2:latest"
    assert models.ollama.baseHost == "host.docker.internal:11434"
    assert models.ollama.maxTokens is None
    assert models.ollama.pricing is None  # local compute — no USD pricing
    # No cloud provider config is selected anywhere.
    assert models.anthropic is None and models.static is None


def test_base_host_defaults_when_omitted(ollama_data: dict[str, Any]) -> None:
    ollama_data["spec"]["models"]["ollama"].pop("baseHost", None)
    spec = validate_spec_data(ollama_data)
    assert spec.spec.models.ollama is not None
    assert spec.spec.models.ollama.baseHost == "host.docker.internal:11434"


def test_ollama_selected_without_config_rejected(ollama_data: dict[str, Any]) -> None:
    del ollama_data["spec"]["models"]["ollama"]
    _rejects(ollama_data, "requires a 'ollama' config block")


def test_ollama_config_for_unselected_provider_rejected(ollama_data: dict[str, Any]) -> None:
    # Flip the selected provider to static (with a matching block) but LEAVE the
    # ollama block present — an unselected-provider config the exhaustive
    # positive declaration must reject.
    ollama_data["spec"]["models"]["provider"] = "static"
    ollama_data["spec"]["models"]["static"] = {"script": ["hi"]}
    _rejects(ollama_data, "unselected provider", "ollama")


def test_ollama_base_host_bad_grammar_rejected(ollama_data: dict[str, Any]) -> None:
    ollama_data["spec"]["models"]["ollama"]["baseHost"] = "http://not a host/path"
    _rejects(ollama_data, "baseHost", "host[:port]")


def test_ollama_base_host_port_out_of_range_rejected(ollama_data: dict[str, Any]) -> None:
    ollama_data["spec"]["models"]["ollama"]["baseHost"] = "host.docker.internal:99999"
    _rejects(ollama_data, "baseHost", "outside 1-65535")


def test_ollama_empty_model_rejected(ollama_data: dict[str, Any]) -> None:
    ollama_data["spec"]["models"]["ollama"]["model"] = ""
    _rejects(ollama_data, "model")


def test_ollama_max_tokens_accepted_and_bounded(ollama_data: dict[str, Any]) -> None:
    ollama_data["spec"]["models"]["ollama"]["maxTokens"] = 2048
    spec = validate_spec_data(ollama_data)
    assert spec.spec.models.ollama is not None
    assert spec.spec.models.ollama.maxTokens == 2048


def test_ollama_max_tokens_zero_rejected(ollama_data: dict[str, Any]) -> None:
    ollama_data["spec"]["models"]["ollama"]["maxTokens"] = 0
    _rejects(ollama_data, "maxTokens")


def test_ollama_as_a_tier_provider_validates(ollama_data: dict[str, Any]) -> None:
    """`ollama` is a first-class tier provider too, not only a default."""
    ollama_data["spec"]["models"]["tiers"] = [
        {
            "name": "reasoning",
            "provider": "ollama",
            "ollama": {"model": "llama3.2:latest", "baseHost": "host.docker.internal:11434"},
        }
    ]
    spec = validate_spec_data(ollama_data)
    (tier,) = spec.spec.models.tiers
    assert tier.provider == "ollama"
    assert tier.ollama is not None and tier.ollama.model == "llama3.2:latest"


def test_ollama_tier_without_config_rejected(ollama_data: dict[str, Any]) -> None:
    ollama_data["spec"]["models"]["tiers"] = [{"name": "reasoning", "provider": "ollama"}]
    _rejects(ollama_data, "models.tiers['reasoning']", "requires a 'ollama' config block")


def test_ollama_unknown_field_rejected(ollama_data: dict[str, Any]) -> None:
    ollama_data["spec"]["models"]["ollama"]["apiKeyEnv"] = "OLLAMA_KEY"  # Ollama takes no key
    _rejects(ollama_data, "apiKeyEnv")


def test_usd_budget_over_unpriced_ollama_rejected(ollama_data: dict[str, Any]) -> None:
    """A USD budget enforces against declared pricing; an unpriced ollama path
    under a USD budget is the decorative cost control the pricing amendment
    forbids (naming the path)."""
    ollama_data["spec"]["models"]["budgets"]["maxUsdPerSession"] = 2.0
    _rejects(ollama_data, "maxUsdPerSession", "models.pricing")


def test_usd_budget_with_priced_ollama_accepted(ollama_data: dict[str, Any]) -> None:
    ollama_data["spec"]["models"]["budgets"]["maxUsdPerSession"] = 2.0
    ollama_data["spec"]["models"]["ollama"]["pricing"] = {
        "usdPerMillionInputTokens": 0.0001,
        "usdPerMillionOutputTokens": 0.0002,
    }
    spec = validate_spec_data(ollama_data)
    assert spec.spec.models.ollama is not None
    assert spec.spec.models.ollama.pricing is not None

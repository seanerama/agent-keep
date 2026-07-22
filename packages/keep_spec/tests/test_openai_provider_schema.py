"""Schema tests for the additive `openai` model provider (issue #15, stage 10).

Additive-only surface: a new `models.openai` config block plus `openai` in the
provider enum of BOTH `models` and `models.tiers[]`. These tests pin the
cross-field rules (`_check_provider_config` + baseHost grammar + apiKeyEnv
pattern) exactly as the anthropic/ollama/static provider tests pin theirs.
"""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from keep_spec import load_spec, validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
OPENAI_SPEC = REPO_ROOT / "specs" / "default-chatbot.openai.yaml"


@pytest.fixture
def openai_data() -> dict[str, Any]:
    with open(OPENAI_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def _rejects(data: dict[str, Any], *message_parts: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_spec_data(data)
    text = str(excinfo.value)
    for part in message_parts:
        assert part in text, f"expected {part!r} in validation error:\n{text}"


def test_openai_spec_validates_strictly() -> None:
    spec = load_spec(OPENAI_SPEC)
    models = spec.spec.models
    assert models.provider == "openai"
    assert models.openai is not None
    assert models.openai.model == "gpt-4o-mini"
    assert models.openai.baseHost == "api.openai.com:443"
    assert models.openai.apiKeyEnv == "OPENAI_API_KEY"
    assert models.openai.maxTokens is None
    assert models.openai.pricing is None
    # No other provider config is selected anywhere.
    assert models.anthropic is None and models.ollama is None and models.static is None


def test_base_host_defaults_when_omitted(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["openai"].pop("baseHost", None)
    spec = validate_spec_data(openai_data)
    assert spec.spec.models.openai is not None
    assert spec.spec.models.openai.baseHost == "api.openai.com:443"


def test_api_key_env_defaults_when_omitted(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["openai"].pop("apiKeyEnv", None)
    spec = validate_spec_data(openai_data)
    assert spec.spec.models.openai is not None
    assert spec.spec.models.openai.apiKeyEnv == "OPENAI_API_KEY"


def test_openai_selected_without_config_rejected(openai_data: dict[str, Any]) -> None:
    del openai_data["spec"]["models"]["openai"]
    _rejects(openai_data, "requires a 'openai' config block")


def test_openai_config_for_unselected_provider_rejected(openai_data: dict[str, Any]) -> None:
    # Flip the selected provider to static (with a matching block) but LEAVE the
    # openai block present — an unselected-provider config the exhaustive
    # positive declaration must reject.
    openai_data["spec"]["models"]["provider"] = "static"
    openai_data["spec"]["models"]["static"] = {"script": ["hi"]}
    _rejects(openai_data, "unselected provider", "openai")


def test_openai_base_host_bad_grammar_rejected(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["openai"]["baseHost"] = "https://not a host/path"
    _rejects(openai_data, "baseHost", "host[:port]")


def test_openai_base_host_port_out_of_range_rejected(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["openai"]["baseHost"] = "api.openai.com:99999"
    _rejects(openai_data, "baseHost", "outside 1-65535")


def test_openai_bad_api_key_env_rejected(openai_data: dict[str, Any]) -> None:
    # apiKeyEnv is an env var NAME (ENV_VAR_NAME grammar) — a lowercase value fails.
    openai_data["spec"]["models"]["openai"]["apiKeyEnv"] = "openai_api_key"
    _rejects(openai_data, "apiKeyEnv")


def test_openai_empty_model_rejected(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["openai"]["model"] = ""
    _rejects(openai_data, "model")


def test_openai_max_tokens_accepted_and_bounded(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["openai"]["maxTokens"] = 2048
    spec = validate_spec_data(openai_data)
    assert spec.spec.models.openai is not None
    assert spec.spec.models.openai.maxTokens == 2048


def test_openai_max_tokens_zero_rejected(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["openai"]["maxTokens"] = 0
    _rejects(openai_data, "maxTokens")


def test_openai_as_a_tier_provider_validates(openai_data: dict[str, Any]) -> None:
    """`openai` is a first-class tier provider too, not only a default."""
    openai_data["spec"]["models"]["tiers"] = [
        {
            "name": "reasoning",
            "provider": "openai",
            "openai": {"model": "gpt-4o", "baseHost": "api.openai.com:443"},
        }
    ]
    spec = validate_spec_data(openai_data)
    (tier,) = spec.spec.models.tiers
    assert tier.provider == "openai"
    assert tier.openai is not None and tier.openai.model == "gpt-4o"


def test_openai_tier_without_config_rejected(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["tiers"] = [{"name": "reasoning", "provider": "openai"}]
    _rejects(openai_data, "models.tiers['reasoning']", "requires a 'openai' config block")


def test_openai_unknown_field_rejected(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["openai"]["organization"] = "org-123"
    _rejects(openai_data, "organization")


def test_usd_budget_over_unpriced_openai_rejected(openai_data: dict[str, Any]) -> None:
    """A USD budget enforces against declared pricing; an unpriced openai path
    under a USD budget is the decorative cost control the pricing amendment
    forbids (naming the path)."""
    openai_data["spec"]["models"]["budgets"]["maxUsdPerSession"] = 2.0
    _rejects(openai_data, "maxUsdPerSession", "models.pricing")


def test_usd_budget_with_priced_openai_accepted(openai_data: dict[str, Any]) -> None:
    openai_data["spec"]["models"]["budgets"]["maxUsdPerSession"] = 2.0
    openai_data["spec"]["models"]["openai"]["pricing"] = {
        "usdPerMillionInputTokens": 0.15,
        "usdPerMillionOutputTokens": 0.6,
    }
    spec = validate_spec_data(openai_data)
    assert spec.spec.models.openai is not None
    assert spec.spec.models.openai.pricing is not None

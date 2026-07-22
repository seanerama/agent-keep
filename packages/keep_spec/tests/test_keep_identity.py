"""Agent Keep stage-1 identity tests (transplant acceptance).

The spec version identifier under the new identity is `keep/v1` — the ONLY
apiVersion the schema admits — and a minimal chatbot spec (dev-http channel +
static provider + jsonl audit) validates strictly.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from keep_spec import validate_spec_data
from keep_spec.models import AgentSpec


def _minimal_chatbot_data() -> dict[str, Any]:
    """A minimal chatbot: dev-http in, static provider replies, jsonl audit."""
    return {
        "apiVersion": "keep/v1",
        "kind": "AgentSpec",
        "metadata": {
            "name": "Minimal Chatbot",
            "slug": "minimal-chatbot",
            "description": "Smallest keep/v1 chatbot: dev-http + static + jsonl.",
            "specVersion": "0.1.0",
        },
        "spec": {
            "persona": {"identity": "You are a minimal chatbot."},
            "channels": [{"type": "dev-http", "port": 8000}],
            "gateway": {"queue": "in-process"},
            "sessions": {"mode": "single"},
            "approval": {},
            "sandbox": {"egress": []},
            "models": {"provider": "static", "static": {"script": ["hello"]}},
            "observability": {
                "audit": {"sink": "jsonl", "path": "/var/lib/agent-keep/audit.jsonl"}
            },
            "persistence": {"tier": "sqlite"},
        },
    }


def test_spec_version_identifier_is_keep_v1() -> None:
    """`keep/v1` is the declared spec version identifier of this schema."""
    spec = validate_spec_data(_minimal_chatbot_data())
    assert spec.apiVersion == "keep/v1"
    # The schema itself admits ONLY the keep/v1 literal.
    assert AgentSpec.model_fields["apiVersion"].annotation.__args__ == ("keep/v1",)  # type: ignore[union-attr]


def test_predecessor_identifier_is_rejected() -> None:
    """The predecessor's `foundry`-namespaced identifier no longer validates."""
    data = _minimal_chatbot_data()
    data["apiVersion"] = "/".join(("foundry", "v1"))  # avoid the literal in-repo
    with pytest.raises(ValidationError):
        validate_spec_data(data)


def test_minimal_chatbot_spec_validates() -> None:
    """dev-http + static provider + jsonl audit is a complete, valid spec."""
    spec = validate_spec_data(_minimal_chatbot_data())
    assert spec.spec.channels[0].type == "dev-http"
    assert spec.spec.models.provider == "static"
    assert spec.spec.observability.audit.sink == "jsonl"

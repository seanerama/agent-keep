"""docs/spec-schema.json must stay in sync with the Pydantic models."""

import json
from pathlib import Path

from keep_spec.schema_export import export_schema, schema_json

REPO_ROOT = Path(__file__).parents[3]
COMMITTED_SCHEMA = REPO_ROOT / "docs" / "spec-schema.json"


def test_committed_schema_matches_models() -> None:
    committed_text = COMMITTED_SCHEMA.read_text(encoding="utf-8")
    assert json.loads(committed_text) == export_schema(), (
        "docs/spec-schema.json is stale — regenerate with "
        "`uv run python -m keep_spec.schema_export docs/spec-schema.json`"
    )
    # Byte-for-byte too, so the committed artifact stays canonical.
    assert committed_text == schema_json()


def test_schema_declares_envelope() -> None:
    schema = export_schema()
    assert schema["title"] == "AgentSpec"
    assert set(schema["required"]) == {"apiVersion", "kind", "metadata", "spec"}
    assert schema["additionalProperties"] is False

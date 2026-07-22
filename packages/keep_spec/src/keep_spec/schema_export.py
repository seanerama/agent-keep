"""JSON Schema export for the keep/v1 AgentSpec envelope.

The committed artifact is docs/spec-schema.json; a test asserts it stays in
sync with the models. Regenerate with:

    uv run python -m keep_spec.schema_export docs/spec-schema.json
"""

import json
import sys
from pathlib import Path
from typing import Any

from keep_spec.models import AgentSpec


def export_schema() -> dict[str, Any]:
    """Return the JSON Schema for the keep/v1 AgentSpec envelope."""
    return AgentSpec.model_json_schema()


def schema_json() -> str:
    """Canonical serialized form of the schema (what docs/spec-schema.json holds)."""
    return json.dumps(export_schema(), indent=2, sort_keys=True) + "\n"


def write_schema(path: str | Path) -> None:
    """Write the canonical schema JSON to `path`."""
    Path(path).write_text(schema_json(), encoding="utf-8")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "docs/spec-schema.json"
    write_schema(target)
    print(f"wrote {target}")

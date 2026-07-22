"""Human-readable field reference for keep/v1, GENERATED from the models.

The committed artifact is docs/spec-reference.md; a test asserts it stays in
sync with the models (like docs/spec-schema.json). Regenerate with:

    uv run python -m keep_spec.reference_export docs/spec-reference.md
"""

import json
import sys
import types
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from keep_spec.models import AgentSpec

HEADER = """\
# keep/v1 AgentSpec — field reference

GENERATED from the `keep_spec` Pydantic models — do not edit by hand.
Regenerate with `uv run python -m keep_spec.reference_export docs/spec-reference.md`.

Envelope contract: `contracts/agent-spec.md` (frozen v1; strict validation —
unknown fields are an error). Decision coverage: every agent-level decision of
ADR 0003 maps to fields below (`keep_spec.decision_coverage`).
"""


def _referenced_models(annotation: Any) -> list[type[BaseModel]]:
    """All BaseModel classes reachable directly from an annotation."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _referenced_models(get_args(annotation)[0])
    if origin in (Union, types.UnionType, list):
        found: list[type[BaseModel]] = []
        for arg in get_args(annotation):
            found.extend(_referenced_models(arg))
        return found
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return []


def _type_str(annotation: Any) -> str:
    """Render an annotation as compact, table-safe text (no raw '|')."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _type_str(get_args(annotation)[0])
    if origin is Literal:
        return "one of: " + ", ".join(f"`{value}`" for value in get_args(annotation))
    if origin is list:
        return f"list of {_type_str(get_args(annotation)[0])}"
    if origin is dict:
        key, value = get_args(annotation)
        return f"mapping of {_type_str(key)} to {_type_str(value)}"
    if origin in (Union, types.UnionType):
        parts = [_type_str(arg) for arg in get_args(annotation)]
        return " or ".join(parts)
    if annotation is type(None):
        return "null"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return f"[{annotation.__name__}](#{annotation.__name__.lower()})"
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _default_str(field: Any) -> str:
    if field.default_factory is not None:
        return f"`{json.dumps(_jsonable(field.default_factory()))}`"
    if field.default is PydanticUndefined:
        return "—"
    return f"`{json.dumps(_jsonable(field.default))}`"


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _collect_models(root: type[BaseModel]) -> list[type[BaseModel]]:
    """Depth-first, field-order traversal from the root model, deduped."""
    ordered: list[type[BaseModel]] = []
    seen: set[type[BaseModel]] = set()

    def visit(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        ordered.append(model)
        for field in model.model_fields.values():
            for referenced in _referenced_models(field.annotation):
                visit(referenced)

    visit(root)
    return ordered


def reference_markdown() -> str:
    """The full generated reference document."""
    lines: list[str] = [HEADER]
    for model in _collect_models(AgentSpec):
        lines.append(f"## {model.__name__}")
        lines.append("")
        doc = " ".join((model.__doc__ or "").split())
        if doc:
            lines.append(doc)
            lines.append("")
        if not model.model_fields:
            lines.append("*No fields.*")
            lines.append("")
            continue
        lines.append("| Field | Type | Required | Default | Description |")
        lines.append("| --- | --- | --- | --- | --- |")
        for name, field in model.model_fields.items():
            required = "yes" if field.is_required() else "no"
            constraints = []
            for meta in field.metadata:
                for attr in ("pattern", "min_length", "max_length", "ge", "le", "gt", "lt"):
                    value = getattr(meta, attr, None)
                    if value is not None:
                        constraints.append(f"{attr}: `{value}`")
            description = field.description or ""
            if constraints:
                description = f"{description} ({'; '.join(constraints)})".strip()
            type_cell = _escape_cell(_type_str(field.annotation))
            default_cell = _escape_cell(_default_str(field))
            description_cell = _escape_cell(description)
            lines.append(
                f"| `{name}` | {type_cell} | {required} | {default_cell} | {description_cell} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_reference(path: str | Path) -> None:
    """Write the generated reference markdown to `path`."""
    Path(path).write_text(reference_markdown(), encoding="utf-8")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "docs/spec-reference.md"
    write_reference(target)
    print(f"wrote {target}")

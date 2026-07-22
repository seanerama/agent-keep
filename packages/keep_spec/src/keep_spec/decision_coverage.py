"""Machine-checkable decision coverage: ADR 0003's 18 agent-level decisions
mapped to concrete keep/v1 schema field paths.

ADR 0003 classifies the blueprint's 25 decisions into 9 factory-level (ADRs)
and 16 agent-level (spec fields), plus two additions the blueprint lacks:
triggers and egress. This module is the authoritative map from each of the 18
agent-level decisions to the schema fields that answer it; the test suite
fails if any decision lacks a home or cites a field path that does not exist
in the models (`resolve_field_path`).

`blueprint_component` cites the owning component as `<layer>/<component>` from
docs/blueprint-data.json, or "adr-0003" for the two additions.
"""

import types
from dataclasses import dataclass
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel

from keep_spec.models import AgentSpec


@dataclass(frozen=True)
class Decision:
    """One agent-level decision and its schema home(s)."""

    blueprint_component: str  # "<layer>/<component>" in blueprint-data.json, or "adr-0003"
    question: str
    field_paths: tuple[str, ...]  # dot paths from the document root; "[]" = list element


DECISIONS: dict[str, Decision] = {
    "channel-transport": Decision(
        blueprint_component="channels/adapters",
        question="How does each platform deliver messages to you?",
        field_paths=("spec.channels[].transport", "spec.channels[].verification"),
    ),
    "allowlist-policy": Decision(
        blueprint_component="gateway/identity",
        question="Who is allowed to talk to the agent?",
        field_paths=("spec.gateway.allowlist.policy", "spec.gateway.allowlist.roster"),
    ),
    "identity-unification": Decision(
        blueprint_component="gateway/identity",
        question="How do you unify one human across channels?",
        field_paths=("spec.gateway.identityUnification",),
    ),
    "queue-weight": Decision(
        blueprint_component="gateway/queue",
        question="How heavy should the queue be?",
        field_paths=("spec.gateway.queue",),
    ),
    "session-concurrency": Decision(
        blueprint_component="gateway/queue",
        question="Serial or concurrent handling per conversation?",
        field_paths=("spec.gateway.concurrency",),
    ),
    "session-definition": Decision(
        blueprint_component="core/sessions",
        question="What is a session, exactly?",
        field_paths=("spec.sessions.definition",),
    ),
    "history-strategy": Decision(
        blueprint_component="core/sessions",
        question="How do you fit history into the context window?",
        field_paths=("spec.sessions.history",),
    ),
    "personalization-source": Decision(
        blueprint_component="core/persona",
        question="Where does personalization live?",
        field_paths=("spec.persona.source", "spec.persona.precedence"),
    ),
    "memory-structure": Decision(
        blueprint_component="core/memorysys",
        question="Structured memory or embeddings-first?",
        # corpus (stage-4 amendment) refines WHAT gets embedded within this
        # decision — a refinement, not a 19th decision.
        field_paths=(
            "spec.memory.structure",
            "spec.memory.writePolicy",
            "spec.memory.structure.corpus",
        ),
    ),
    "vector-store": Decision(
        blueprint_component="core/memorysys",
        question="Which vector store, when you get there?",
        field_paths=("spec.memory.structure.store",),
    ),
    "skill-selection": Decision(
        blueprint_component="capabilities/skills",
        question="How are skills selected per request?",
        field_paths=("spec.skills[].selection", "spec.skills[].keywords"),
    ),
    "mcp-allowlist": Decision(
        blueprint_component="capabilities/mcpmgr",
        question="Which MCP servers get attached to which agent?",
        # constraints (stage-4 amendment) refines a grant with hard parameter
        # pins within this decision — a refinement, not a 19th decision.
        field_paths=(
            "spec.tools[].allow[].name",
            "spec.tools[].allow[].scope",
            "spec.tools[].allow[].constraints",
        ),
    ),
    "approval-policy": Decision(
        blueprint_component="capabilities/executor",
        question="What requires human approval?",
        field_paths=("spec.approval.policy", "spec.approval.autoApprove"),
    ),
    "isolation-profile": Decision(
        blueprint_component="capabilities/executor",
        question="How isolated is code/shell execution?",
        field_paths=("spec.sandbox.profile",),
    ),
    "model-routing": Decision(
        blueprint_component="model/llmrouter",
        question="Where does cost control live (routing tiers, budgets)?",
        field_paths=(
            "spec.models.provider",
            "spec.models.tiers[].provider",
            "spec.models.budgets",
        ),
    ),
    "persistence-tier": Decision(
        blueprint_component="persistence/stores",
        question="Files, SQLite, or Postgres?",
        field_paths=("spec.persistence.tier",),
    ),
    "triggers": Decision(
        blueprint_component="adr-0003",
        question="What activates the agent besides a human message?",
        field_paths=("spec.triggers.activations",),
    ),
    "egress": Decision(
        blueprint_component="adr-0003",
        question="Network allowlist for the container?",
        field_paths=("spec.sandbox.egress",),
    ),
}

EXPECTED_DECISION_COUNT = 18


def _candidate_models(annotation: Any) -> list[type[BaseModel]]:
    """Unwrap Annotated/Optional/Union down to the BaseModel members."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _candidate_models(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        models: list[type[BaseModel]] = []
        for arg in get_args(annotation):
            models.extend(_candidate_models(arg))
        return models
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return []


def resolve_field_path(path: str, root: type[BaseModel] = AgentSpec) -> None:
    """Assert `path` names a real field in the models; raise LookupError if not.

    Grammar: dot-separated field names; a "[]" suffix steps into the list's
    element type. At a union (e.g. a discriminated union), the next segment
    must exist on at least one member.
    """
    candidates: list[type[BaseModel]] = [root]
    segments = path.split(".")
    for index, segment in enumerate(segments):
        name = segment.removesuffix("[]")
        is_list = segment.endswith("[]")
        next_annotations: list[Any] = []
        for model in candidates:
            field = model.model_fields.get(name)
            if field is None:
                continue
            annotation = field.annotation
            if is_list:
                origin = get_origin(annotation)
                if origin is Annotated:
                    annotation = get_args(annotation)[0]
                    origin = get_origin(annotation)
                if origin is not list:
                    raise LookupError(
                        f"{path}: segment '{segment}' expects a list, "
                        f"but {model.__name__}.{name} is {field.annotation!r}"
                    )
                annotation = get_args(annotation)[0]
            next_annotations.append(annotation)
        if not next_annotations:
            names = sorted({m.__name__ for m in candidates})
            raise LookupError(f"{path}: no field '{name}' on any of {names}")
        candidates = [m for ann in next_annotations for m in _candidate_models(ann)]
        if not candidates and index < len(segments) - 1:
            # non-terminal segment must lead somewhere traversable
            raise LookupError(f"{path}: segment '{segment}' is a leaf; cannot descend further")


def assert_full_coverage() -> None:
    """Raise if the 18 agent-level decisions do not all have real schema homes."""
    if len(DECISIONS) != EXPECTED_DECISION_COUNT:
        raise LookupError(
            f"expected {EXPECTED_DECISION_COUNT} agent-level decisions (ADR 0003), "
            f"found {len(DECISIONS)}"
        )
    for decision_id, decision in DECISIONS.items():
        if not decision.field_paths:
            raise LookupError(f"decision '{decision_id}' has no schema home")
        for field_path in decision.field_paths:
            try:
                resolve_field_path(field_path)
            except LookupError as exc:
                raise LookupError(f"decision '{decision_id}': {exc}") from None

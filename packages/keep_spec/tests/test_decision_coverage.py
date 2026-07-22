"""18/18 decision coverage (stage-2 acceptance condition).

ADR 0003 classifies 18 agent-level decisions (16 blueprint + triggers +
egress). Every one must have a concrete schema home; this test fails if any
lacks one, if a cited field path does not exist in the models, or if a cited
blueprint component id is not in docs/blueprint-data.json.
"""

import json
from pathlib import Path

import pytest

from keep_spec.decision_coverage import (
    DECISIONS,
    EXPECTED_DECISION_COUNT,
    assert_full_coverage,
    resolve_field_path,
)

REPO_ROOT = Path(__file__).parents[3]
BLUEPRINT = REPO_ROOT / "docs" / "blueprint-data.json"

# ADR 0003's agent-level list, in its own order, mapped to our decision ids.
ADR_0003_AGENT_LEVEL = [
    "channel-transport",  # channel transport per platform
    "allowlist-policy",  # allowlist policy
    "identity-unification",  # identity unification
    "queue-weight",  # queue weight
    "session-concurrency",  # serial/concurrent sessions
    "session-definition",  # session definition
    "history-strategy",  # history strategy
    "personalization-source",  # personalization source (+ precedence)
    "memory-structure",  # memory structure
    "vector-store",  # vector store choice
    "skill-selection",  # skill selection strategy
    "mcp-allowlist",  # per-agent MCP allowlist
    "approval-policy",  # approval policy
    "isolation-profile",  # execution isolation profile
    "model-routing",  # model routing/cost policy
    "persistence-tier",  # persistence tier
    "triggers",  # ADR 0003 addition
    "egress",  # ADR 0003 addition
]


def test_18_of_18_decisions_have_schema_homes() -> None:
    assert len(DECISIONS) == EXPECTED_DECISION_COUNT == 18
    assert sorted(DECISIONS) == sorted(ADR_0003_AGENT_LEVEL)
    assert_full_coverage()  # raises LookupError on any missing home


@pytest.mark.parametrize("decision_id", ADR_0003_AGENT_LEVEL)
def test_every_field_path_exists_in_models(decision_id: str) -> None:
    decision = DECISIONS[decision_id]
    assert decision.field_paths, f"decision '{decision_id}' has no schema home"
    for field_path in decision.field_paths:
        resolve_field_path(field_path)  # LookupError = no such field


def test_blueprint_component_citations_are_real() -> None:
    data = json.loads(BLUEPRINT.read_text(encoding="utf-8"))
    component_ids = {
        f"{layer['id']}/{component['id']}"
        for layer in data["layers"]
        for component in layer["components"]
    }
    for decision_id, decision in DECISIONS.items():
        if decision.blueprint_component != "adr-0003":  # the two additions the blueprint lacks
            assert decision.blueprint_component in component_ids, (
                f"decision '{decision_id}' cites unknown blueprint component "
                f"'{decision.blueprint_component}'"
            )
    additions = [d for d in DECISIONS.values() if d.blueprint_component == "adr-0003"]
    assert len(additions) == 2  # triggers + egress


def test_resolver_rejects_fabricated_paths() -> None:
    with pytest.raises(LookupError):
        resolve_field_path("spec.persona.nonexistent")
    with pytest.raises(LookupError):
        resolve_field_path("spec.imaginary.section")
    with pytest.raises(LookupError):
        resolve_field_path("spec.persona[].identity")  # persona is not a list

"""The component-not-implemented guard: schema-valid selections the stage-1
component library cannot build fail loudly (at compose time AND boot), never
silently degrade.
"""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_runtime.wiring import (
    ComponentNotImplementedError,
    EgressCrossValidationError,
    egress_violations,
    ensure_buildable,
    mcp_egress_violations,
    select_components,
    unimplemented_selections,
)
from keep_spec import validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
SKELETON_SPEC = REPO_ROOT / "examples" / "skeleton.yaml"


@pytest.fixture
def skeleton_data() -> dict[str, Any]:
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def test_skeleton_is_fully_buildable(skeleton_data: dict[str, Any]) -> None:
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    ensure_buildable(spec)  # must not raise
    assert "dev-http-channel" in select_components(spec)


@pytest.mark.parametrize(
    ("mutate_path", "value", "expected_fragment"),
    [
        (["spec", "channels"], [{"type": "discord"}], "channel adapter 'discord'"),
        (["spec", "gateway", "concurrency"], "concurrent-locked", "concurrency"),
        (
            # owner-only / tiered are ENFORCED as of stage 10; 'pairing'
            # (runtime roster growth) still has no enforcer and stays guarded.
            ["spec", "gateway", "allowlist"],
            {"policy": "pairing"},
            "gateway allowlist policy 'pairing'",
        ),
        (["spec", "gateway", "identityUnification"], "challenge", "identity unification"),
        (["spec", "sessions", "definition"], "hybrid", "session definition"),
        (
            ["spec", "sessions", "history"],
            {"strategy": "summarization"},
            "history strategy 'summarization'",
        ),
        (
            # stage 24 flips facts + user-command buildable, so guard a facts
            # spec whose writePolicy this library does NOT build instead.
            ["spec", "memory"],
            {"structure": {"kind": "facts"}, "writePolicy": "agent-autonomous"},
            "memory writePolicy 'agent-autonomous' for facts",
        ),
        (
            ["spec", "skills"],
            [{"name": "house-style"}],
            "skill registry",
        ),
        # postgres flipped buildable in stage 15; 'files' still has no component.
        (["spec", "persistence", "tier"], "files", "persistence tier 'files'"),
        (
            ["spec", "observability", "health"],
            {"path": "/status"},
            "health surface at custom path '/status'",
        ),
        (["spec", "sandbox", "profile"], "restricted-user", "sandbox profile 'restricted-user'"),
        (
            ["spec", "approval", "policy"],
            "everything",
            "approval enforcement for policy 'everything'",
        ),
        (
            ["spec", "approval", "policy"],
            "autonomous",
            "approval enforcement for policy 'autonomous'",
        ),
    ],
)
def test_unimplemented_selection_fails_loudly(
    skeleton_data: dict[str, Any],
    mutate_path: list[str],
    value: Any,
    expected_fragment: str,
) -> None:
    target = skeleton_data
    for key in mutate_path[:-1]:
        target = target[key]
    target[mutate_path[-1]] = value
    spec = validate_spec_data(skeleton_data)  # still schema-valid...
    with pytest.raises(ComponentNotImplementedError) as excinfo:
        select_components(spec)  # ...but not buildable
    message = str(excinfo.value)
    assert "component not implemented" in message
    assert expected_fragment in message


_CONSTRAINED_TOOLS: list[dict[str, Any]] = [
    {
        "name": "pager",
        "transport": {"kind": "stdio", "command": "mcp-pager"},
        "allow": [
            {
                "name": "send_page",
                "scope": "read-write",
                "constraints": {"room": "noc-outages"},
            }
        ],
    }
]


def test_stdio_mcp_grants_are_buildable(skeleton_data: dict[str, Any]) -> None:
    """Stage 7 flips the MCP guard: stdio MCP grants — constraints, scopes and
    autoApprove included — select the mcp-manager component and build. The
    grants themselves are validated against the LIVE server at boot."""
    skeleton_data["spec"]["tools"] = copy.deepcopy(_CONSTRAINED_TOOLS)
    skeleton_data["spec"]["approval"] = {
        "policy": "allowlist-confirm-rest",
        "autoApprove": ["pager.send_page"],
    }
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    assert "mcp-manager" in select_components(spec)
    assert "local-tools" not in select_components(spec)


def test_http_mcp_without_egress_entry_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage-7 egress cross-validation: an HTTP MCP server whose host is not
    covered by sandbox.egress fails ensure_buildable (and with it foundry
    build AND boot) with a message naming the server, the host, and the fix."""
    skeleton_data["spec"]["tools"] = [
        {
            "name": "home-assistant",
            "transport": {"kind": "http", "url": "https://ha.internal:8123/mcp"},
            "allow": [{"name": "get_state"}],
        }
    ]
    skeleton_data["spec"]["approval"] = {
        "policy": "allowlist-confirm-rest",
        "autoApprove": ["home-assistant.get_state"],
    }
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []  # the component gap is GONE...
    [violation] = mcp_egress_violations(spec)  # ...but the spec is inconsistent
    assert "home-assistant" in violation
    assert "ha.internal:8123" in violation
    assert "sandbox.egress" in violation
    with pytest.raises(EgressCrossValidationError, match="ha.internal:8123"):
        ensure_buildable(spec)


def test_http_mcp_without_egress_entry_fails_at_boot(skeleton_data: dict[str, Any]) -> None:
    """Boot-time honesty: build_app applies the same cross-validation."""
    from agent_runtime.runner import build_app

    skeleton_data["spec"]["tools"] = [
        {
            "name": "home-assistant",
            "transport": {"kind": "http", "url": "https://ha.internal:8123/mcp"},
            "allow": [{"name": "get_state"}],
        }
    ]
    spec = validate_spec_data(skeleton_data)
    with pytest.raises(EgressCrossValidationError, match="sandbox.egress"):
        build_app(spec)


@pytest.mark.parametrize(
    ("egress_entry", "url", "covered"),
    [
        ("ha.internal:8123", "https://ha.internal:8123/mcp", True),
        ("ha.internal", "https://ha.internal:8123/mcp", True),  # portless entry: any port
        ("ha.internal:9000", "https://ha.internal:8123/mcp", False),  # wrong port
        ("*.internal", "https://ha.internal/mcp", True),  # wildcard subdomain (443 default)
        ("*.internal", "https://internal/mcp", False),  # wildcard excludes the apex
        ("other.host", "http://ha.internal/mcp", False),
    ],
)
def test_egress_cross_validation_matching_rules(
    skeleton_data: dict[str, Any], egress_entry: str, url: str, covered: bool
) -> None:
    skeleton_data["spec"]["tools"] = [
        {
            "name": "remote",
            "transport": {"kind": "http", "url": url},
            "allow": [{"name": "get_state"}],
        }
    ]
    skeleton_data["spec"]["sandbox"]["egress"] = [egress_entry]
    spec = validate_spec_data(skeleton_data)
    assert (mcp_egress_violations(spec) == []) is covered
    if covered:
        # Stage 10 retired the blanket egress-ENFORCEMENT guard: a covered HTTP
        # MCP server is now fully buildable (the allowlist's real job is this
        # cross-validation, which it passes).
        assert unimplemented_selections(spec) == []


def test_stdio_mcp_grants_need_no_egress(skeleton_data: dict[str, Any]) -> None:
    """stdio servers are local children — no egress entry, no violation."""
    skeleton_data["spec"]["tools"] = copy.deepcopy(_CONSTRAINED_TOOLS)
    spec = validate_spec_data(skeleton_data)
    assert mcp_egress_violations(spec) == []
    ensure_buildable(spec)  # must not raise


def test_grant_without_constraints_is_buildable_too(
    skeleton_data: dict[str, Any],
) -> None:
    """Absent constraints (the default) trip no guard either."""
    tools = copy.deepcopy(_CONSTRAINED_TOOLS)
    del tools[0]["allow"][0]["constraints"]
    skeleton_data["spec"]["tools"] = tools
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []


_LOCAL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "local-demo",
        "transport": {"kind": "local"},
        "allow": [
            {"name": "clock.now"},
            {"name": "echo.repeat", "constraints": {"times": 2}},
        ],
    }
]


def test_local_tool_grants_are_buildable(skeleton_data: dict[str, Any]) -> None:
    """Stage 6: a spec granting local tools — constraints and autoApprove
    included — is fully buildable and selects the local-tools component."""
    skeleton_data["spec"]["tools"] = copy.deepcopy(_LOCAL_TOOLS)
    skeleton_data["spec"]["approval"] = {
        "policy": "allowlist-confirm-rest",
        "autoApprove": ["local-demo.clock.now"],
    }
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    assert "local-tools" in select_components(spec)
    assert "mcp-manager" not in select_components(spec)  # local grants need no MCP client


def test_unknown_local_tool_fails_loudly(skeleton_data: dict[str, Any]) -> None:
    """A local grant naming a tool the registry lacks fails the buildable check."""
    skeleton_data["spec"]["tools"] = [
        {
            "name": "local-demo",
            "transport": {"kind": "local"},
            "allow": [{"name": "shell.exec"}],
        }
    ]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == [
        "local tool 'shell.exec' (spec.tools['local-demo'].allow)"
    ]
    with pytest.raises(ComponentNotImplementedError, match="local tool 'shell.exec'"):
        ensure_buildable(spec)


def _pgvector_memory_section() -> dict[str, Any]:
    """The one memory selection stage 16 implements (the outage spec's)."""
    return {
        "structure": {"kind": "vectors", "store": "pgvector", "corpus": "agent-summaries"},
        "writePolicy": "agent-autonomous",
    }


def test_pgvector_memory_selection_is_buildable(skeleton_data: dict[str, Any]) -> None:
    """Stage 16 flip: vectors+pgvector over agent-summaries with an
    agent-autonomous writePolicy builds, and the pgvector_memory module ships.
    Conversely `memory:` absent (the skeleton itself) ships NO memory module —
    absence semantics are the kill-switch."""
    from agent_runtime.wiring import component_module_names

    skeleton_data["spec"]["memory"] = _pgvector_memory_section()
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    assert "pgvector-memory" in select_components(spec)
    assert "pgvector_memory" in component_module_names(spec)


def test_memory_absent_means_memory_module_absent(skeleton_data: dict[str, Any]) -> None:
    """The kill-switch is spec-opt-in: no `memory:` section, no memory module."""
    from agent_runtime.wiring import component_module_names

    spec = validate_spec_data(skeleton_data)
    assert "pgvector-memory" not in select_components(spec)
    assert "pgvector_memory" not in component_module_names(spec)


def test_readonly_write_policy_is_buildable(skeleton_data: dict[str, Any]) -> None:
    """writePolicy 'off' is trivially enforceable (read-only store) and builds."""
    memory = _pgvector_memory_section()
    memory["writePolicy"] = "off"
    skeleton_data["spec"]["memory"] = memory
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []


def test_facts_memory_selection_is_buildable_and_ships_the_module(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage 24 flip: facts + user-command builds on the skeleton's own sqlite
    tier and ships facts_memory (NOT pgvector_memory — facts is a different
    structure). The store lives on the persistence tier, so no store/corpus."""
    from agent_runtime.wiring import component_module_names

    skeleton_data["spec"]["memory"] = {
        "structure": {"kind": "facts", "store": "none"},
        "writePolicy": "user-command",
    }
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    ensure_buildable(spec)  # must not raise
    assert "facts-memory" in select_components(spec)
    assert "pgvector-memory" not in select_components(spec)
    modules = component_module_names(spec)
    assert "facts_memory" in modules
    assert "pgvector_memory" not in modules


def test_facts_memory_builds_on_both_persistence_tiers(skeleton_data: dict[str, Any]) -> None:
    """The facts table lives on the ACTIVE tier — sqlite AND postgres both
    build (no tier constraint: both durably persist as of stages 15/20)."""
    for tier in ("sqlite", "postgres"):
        data = copy.deepcopy(skeleton_data)
        data["spec"]["memory"] = {
            "structure": {"kind": "facts", "store": "none"},
            "writePolicy": "user-command",
        }
        data["spec"]["persistence"]["tier"] = tier
        spec = validate_spec_data(data)
        assert unimplemented_selections(spec) == [], tier
        ensure_buildable(spec)  # must not raise
        assert "facts-memory" in select_components(spec), tier


@pytest.mark.parametrize(
    ("structure", "write_policy", "expected_fragment"),
    [
        (  # facts + agent-autonomous: facts builds ONLY under user-command
            # (stage 24 — the command surface IS the write path); a non-command
            # writePolicy stays guarded on the FACTS branch (the reviewer's
            # restructure — the check no longer nests under the vectors branch).
            {"kind": "facts"},
            "agent-autonomous",
            "memory writePolicy 'agent-autonomous' for facts (spec.memory.writePolicy)",
        ),
        (  # facts + off: same — no facts writer, guarded on the facts branch
            {"kind": "facts"},
            "off",
            "memory writePolicy 'off' for facts (spec.memory.writePolicy)",
        ),
        (  # layered: no component at all
            {"kind": "layered", "store": "pgvector", "corpus": "agent-summaries"},
            "agent-autonomous",
            "memory structure 'layered' (spec.memory)",
        ),
        (  # sqlite-vec: the store has no component (stage-4 corpus scoping
            # keeps riding a guard — now the store-specific one)
            {"kind": "vectors", "store": "sqlite-vec", "corpus": "agent-summaries"},
            "agent-autonomous",
            "memory structure 'vectors' store 'sqlite-vec' (spec.memory.structure.store)",
        ),
        (  # transcripts corpus: no writer embeds raw transcripts — guarded
            {"kind": "vectors", "store": "pgvector", "corpus": "transcripts"},
            "agent-autonomous",
            "memory corpus 'transcripts' (spec.memory.structure.corpus)",
        ),
        (  # documents corpus: no writer — guarded
            {"kind": "vectors", "store": "pgvector", "corpus": "documents"},
            "agent-autonomous",
            "memory corpus 'documents' (spec.memory.structure.corpus)",
        ),
        (  # corpus ABSENT defaults to transcripts (schema) — still guarded,
            # and the message says so
            {"kind": "vectors", "store": "pgvector"},
            "agent-autonomous",
            "memory corpus 'transcripts (the schema default for absent corpus)' "
            "(spec.memory.structure.corpus)",
        ),
        (  # user-command: no command path exists — guarded. NOTE: this is
            # the schema DEFAULT writePolicy, so a bare pgvector selection
            # without an explicit writePolicy is guarded too.
            {"kind": "vectors", "store": "pgvector", "corpus": "agent-summaries"},
            "user-command",
            "memory writePolicy 'user-command' (spec.memory.writePolicy)",
        ),
    ],
)
def test_unbuildable_memory_selections_stay_guarded(
    skeleton_data: dict[str, Any],
    structure: dict[str, Any],
    write_policy: str,
    expected_fragment: str,
) -> None:
    """Thin-slice discipline: stage 16 flipped vectors+pgvector+agent-summaries
    (agent-autonomous/off), stage 24 flips facts+user-command; every other
    memory selection keeps a loud, specific guard."""
    skeleton_data["spec"]["memory"] = {"structure": structure, "writePolicy": write_policy}
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == [expected_fragment]
    with pytest.raises(ComponentNotImplementedError, match="component not implemented"):
        ensure_buildable(spec)


def test_outage_correlation_is_fully_buildable() -> None:
    """Phase 2 EXIT STATE (stage 25): the outage-correlation reference spec has
    NO remaining unimplemented selections — every prior stage (15-24 sessions,
    memory, history, trigger, channel, budgets) plus this stage's USD-budget
    enforcement closes the last gap. Pinned to EMPTY exactly so a regression in
    EITHER direction (a guard back, or another guard silently dropped) fails
    here. The spec now declares pricing on every anthropic path, so the USD
    budget is enforceable by construction — no gap."""
    from keep_spec import load_spec

    spec = load_spec(REPO_ROOT / "examples" / "outage-correlation.yaml")
    remaining = unimplemented_selections(spec)
    assert remaining == []
    ensure_buildable(spec)  # must not raise
    assert "model-router" in select_components(spec)


def test_client_tracking_is_fully_buildable() -> None:
    """Phase 2 EXIT STATE: the client-tracking reference spec has NO remaining
    unimplemented selections either — stage 24 (facts memory) closes the memory
    gap and stage 25 (spec-declared pricing) closes the USD-budget gap, so with
    both merged the spec builds END TO END. Pinned to EMPTY exactly (mirrors
    test_outage_correlation_is_fully_buildable) so a regression in EITHER
    direction fails here. With both reference specs empty, Phase 2 is done."""
    from keep_spec import load_spec

    spec = load_spec(REPO_ROOT / "examples" / "client-tracking.yaml")
    remaining = unimplemented_selections(spec)
    assert remaining == []
    ensure_buildable(spec)  # must not raise
    assert "model-router" in select_components(spec)


# ------------------------------------------------------ ollama provider (stage 8)


def _ollama_models(base_host: str = "host.docker.internal:11434") -> dict[str, Any]:
    return {
        "provider": "ollama",
        "ollama": {"model": "llama3.2:latest", "baseHost": base_host},
    }


def test_ollama_provider_is_buildable_and_ships_its_module(
    skeleton_data: dict[str, Any],
) -> None:
    """A spec selecting `ollama` builds (the adapter exists) and the
    ollama_provider module ships — the anthropic/static provider pattern."""
    from agent_runtime.wiring import component_module_names

    skeleton_data["spec"]["models"] = _ollama_models()
    skeleton_data["spec"]["sandbox"]["egress"] = ["host.docker.internal:11434"]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    ensure_buildable(spec)  # must not raise
    modules = component_module_names(spec)
    assert "ollama_provider" in modules
    # absence: the other providers are NOT in a ollama-only image
    assert "anthropic_provider" not in modules
    assert "static_provider" not in modules


def test_ollama_without_base_host_in_egress_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """ADR 0006: the ollama baseHost — read from the CONFIG, not a constant —
    must be covered by sandbox.egress, or the model call is unreachable by
    construction (the anthropic cross-check pattern, host from the spec)."""
    skeleton_data["spec"]["models"] = _ollama_models()
    skeleton_data["spec"]["sandbox"]["egress"] = []  # baseHost NOT allowlisted
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []  # the component gap is GONE...
    [violation] = egress_violations(spec)  # ...but the spec is inconsistent
    assert "host.docker.internal:11434" in violation
    assert "sandbox.egress" in violation
    with pytest.raises(EgressCrossValidationError, match="host.docker.internal:11434"):
        ensure_buildable(spec)


def test_ollama_wrong_port_in_egress_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """The port matters: a baseHost of :11434 is not covered by an egress entry
    pinned to a different port."""
    skeleton_data["spec"]["models"] = _ollama_models()
    skeleton_data["spec"]["sandbox"]["egress"] = ["host.docker.internal:1234"]
    spec = validate_spec_data(skeleton_data)
    assert egress_violations(spec) != []
    with pytest.raises(EgressCrossValidationError):
        ensure_buildable(spec)


def test_ollama_with_base_host_in_egress_is_consistent(
    skeleton_data: dict[str, Any],
) -> None:
    skeleton_data["spec"]["models"] = _ollama_models()
    skeleton_data["spec"]["sandbox"]["egress"] = ["host.docker.internal:11434"]
    spec = validate_spec_data(skeleton_data)
    assert egress_violations(spec) == []
    ensure_buildable(spec)  # must not raise


def test_ollama_default_port_covered_by_portless_egress_entry(
    skeleton_data: dict[str, Any],
) -> None:
    """A portless egress entry covers the baseHost's :11434 (the anthropic
    portless-entry rule); the default-port host is reachable."""
    skeleton_data["spec"]["models"] = _ollama_models()
    skeleton_data["spec"]["sandbox"]["egress"] = ["host.docker.internal"]
    spec = validate_spec_data(skeleton_data)
    assert egress_violations(spec) == []


# ------------------------------------------------------ openai provider (stage 10)


def _openai_models(base_host: str = "api.openai.com:443") -> dict[str, Any]:
    return {
        "provider": "openai",
        "openai": {"model": "gpt-4o-mini", "baseHost": base_host},
    }


def test_openai_provider_is_buildable_and_ships_its_module(
    skeleton_data: dict[str, Any],
) -> None:
    """A spec selecting `openai` builds (the adapter exists) and the
    openai_provider module ships — the anthropic/ollama provider pattern."""
    from agent_runtime.wiring import component_module_names

    skeleton_data["spec"]["models"] = _openai_models()
    skeleton_data["spec"]["sandbox"]["egress"] = ["api.openai.com:443"]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    ensure_buildable(spec)  # must not raise
    modules = component_module_names(spec)
    assert "openai_provider" in modules
    # absence: the other providers are NOT in an openai-only image
    assert "anthropic_provider" not in modules
    assert "ollama_provider" not in modules
    assert "static_provider" not in modules


def test_openai_without_base_host_in_egress_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """The openai baseHost — read from the CONFIG, not a constant — must be
    covered by sandbox.egress, or the model call is unreachable by construction
    (the ollama cross-check pattern, host from the spec)."""
    skeleton_data["spec"]["models"] = _openai_models()
    skeleton_data["spec"]["sandbox"]["egress"] = []  # baseHost NOT allowlisted
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []  # the component gap is GONE...
    [violation] = egress_violations(spec)  # ...but the spec is inconsistent
    assert "api.openai.com:443" in violation
    assert "sandbox.egress" in violation
    assert mcp_egress_violations(spec) == []  # the MCP-only view stays MCP-only
    with pytest.raises(EgressCrossValidationError, match="api.openai.com:443"):
        ensure_buildable(spec)


def test_openai_wrong_port_in_egress_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """The port matters: a baseHost of :443 is not covered by an egress entry
    pinned to a different port."""
    skeleton_data["spec"]["models"] = _openai_models()
    skeleton_data["spec"]["sandbox"]["egress"] = ["api.openai.com:8443"]
    spec = validate_spec_data(skeleton_data)
    assert egress_violations(spec) != []
    with pytest.raises(EgressCrossValidationError):
        ensure_buildable(spec)


def test_openai_with_base_host_in_egress_is_consistent(
    skeleton_data: dict[str, Any],
) -> None:
    skeleton_data["spec"]["models"] = _openai_models()
    skeleton_data["spec"]["sandbox"]["egress"] = ["api.openai.com:443"]
    spec = validate_spec_data(skeleton_data)
    assert egress_violations(spec) == []
    ensure_buildable(spec)  # must not raise


def test_openai_tier_without_egress_entry_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """A tier-level openai selection needs its host too; selecting it in several
    places yields one violation per selecting site's baseHost."""
    skeleton_data["spec"]["models"] = {
        "provider": "openai",
        "openai": {"model": "gpt-4o-mini", "baseHost": "api.openai.com:443"},
        "tiers": [
            {
                "name": "reasoning",
                "provider": "openai",
                "openai": {"model": "gpt-4o", "baseHost": "api.openai.com:443"},
            }
        ],
    }
    spec = validate_spec_data(skeleton_data)
    violations = egress_violations(spec)
    assert violations != []
    assert all("api.openai.com:443" in v for v in violations)
    with pytest.raises(EgressCrossValidationError, match="api.openai.com:443"):
        ensure_buildable(spec)


def test_openai_egress_default_host_matches_the_adapter_constant() -> None:
    """The openai schema-default baseHost host resolves to the adapter's
    DEFAULT_BASE_URL host (kept in sync, the anthropic constant convention)."""
    from urllib.parse import urlsplit

    from agent_runtime.components import openai_provider

    parts = urlsplit(openai_provider.DEFAULT_BASE_URL)
    assert parts.hostname == "api.openai.com"


def _sessions_variant(
    skeleton_data: dict[str, Any],
    *,
    definition: str | None = "per-channel",
    history: dict[str, Any] | None = None,
    tier: str = "postgres",
) -> dict[str, Any]:
    """The outage spec's session block (stage 17) grafted onto the skeleton."""
    sessions: dict[str, Any] = {"mode": "single"}
    if definition is not None:
        sessions["definition"] = definition
    if history is not None:
        sessions["history"] = history
    skeleton_data["spec"]["sessions"] = sessions
    skeleton_data["spec"]["persistence"]["tier"] = tier
    return skeleton_data


def test_per_channel_definition_and_retrieval_history_are_buildable(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage 17 flip: `definition: per-channel` + `history: {strategy:
    retrieval, topK: N}` on the postgres tier build, and the retrieval_history
    module ships alongside postgres_persistence."""
    from agent_runtime.wiring import component_module_names

    data = _sessions_variant(skeleton_data, history={"strategy": "retrieval", "topK": 5})
    spec = validate_spec_data(data)
    assert unimplemented_selections(spec) == []
    assert "retrieval-history" in select_components(spec)
    modules = component_module_names(spec)
    assert "retrieval_history" in modules
    assert "postgres_persistence" in modules


def test_per_channel_definition_builds_without_a_history_strategy(
    skeleton_data: dict[str, Any],
) -> None:
    """The two flips are independent: per-channel alone builds (on either
    tier — the in-memory manager keys by channel too) and ships NO retrieval
    module (absence semantics)."""
    from agent_runtime.wiring import component_module_names

    for tier in ("sqlite", "postgres"):
        data = _sessions_variant(skeleton_data, history=None, tier=tier)
        spec = validate_spec_data(data)
        assert unimplemented_selections(spec) == []
        assert "retrieval-history" not in select_components(spec)
        assert "retrieval_history" not in component_module_names(spec)


def test_history_absent_means_retrieval_module_absent(skeleton_data: dict[str, Any]) -> None:
    """The kill-switch is spec-opt-in: no `sessions.history`, no retrieval
    module in the image (the skeleton itself — full-transcript replay)."""
    from agent_runtime.wiring import component_module_names

    spec = validate_spec_data(skeleton_data)
    assert "retrieval-history" not in select_components(spec)
    assert "retrieval_history" not in component_module_names(spec)


def test_retrieval_history_on_sqlite_tier_stays_guarded(skeleton_data: dict[str, Any]) -> None:
    """The retrieval index retrieves over DURABLY stored turns — on the
    in-process 'sqlite' tier the combination is refused loudly, naming both
    sections."""
    data = _sessions_variant(
        skeleton_data, definition=None, history={"strategy": "retrieval"}, tier="sqlite"
    )
    spec = validate_spec_data(data)
    [problem] = unimplemented_selections(spec)
    assert "history strategy 'retrieval' on persistence tier 'sqlite'" in problem
    assert "spec.persistence.tier" in problem
    with pytest.raises(ComponentNotImplementedError, match="persistence tier 'sqlite'"):
        ensure_buildable(spec)


@pytest.mark.parametrize(
    ("definition", "history", "expected_fragment"),
    [
        ("hybrid", None, "session definition 'hybrid' (spec.sessions.definition)"),
        (
            None,
            {"strategy": "summarization"},
            "history strategy 'summarization' (spec.sessions.history)",
        ),
        (
            None,
            {"strategy": "layered"},
            "history strategy 'layered' (spec.sessions.history)",
        ),
    ],
)
def test_other_session_selections_stay_guarded(
    skeleton_data: dict[str, Any],
    definition: str | None,
    history: dict[str, Any] | None,
    expected_fragment: str,
) -> None:
    """Thin-slice discipline: per-channel + retrieval flipped in stage 17,
    per-user + sliding-window in stage 23; hybrid and summarization/layered
    keep loud, specific guards."""
    data = _sessions_variant(skeleton_data, definition=definition, history=history)
    spec = validate_spec_data(data)
    assert unimplemented_selections(spec) == [expected_fragment]
    with pytest.raises(ComponentNotImplementedError, match="component not implemented"):
        ensure_buildable(spec)


def test_per_user_definition_and_sliding_window_are_buildable_on_both_tiers(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage 23 flip: the client-tracking session block — `definition:
    per-user` + `history: {strategy: sliding-window, maxTurns: 40}` — builds
    on BOTH persistence tiers (the window reads the session transcript, which
    both tiers durably persist as of stages 15/20 — deliberately NO tier
    constraint, unlike retrieval), and ships NO extra module: sliding-window
    is a truncation inside the always-selected prompt assembler, so neither
    retrieval_history nor any embedding-bearing component rides along."""
    from agent_runtime.wiring import component_module_names

    for tier in ("sqlite", "postgres"):
        data = _sessions_variant(
            copy.deepcopy(skeleton_data),
            definition="per-user",
            history={"strategy": "sliding-window", "maxTurns": 40},
            tier=tier,
        )
        spec = validate_spec_data(data)
        assert unimplemented_selections(spec) == [], tier
        ensure_buildable(spec)  # must not raise
        assert "retrieval-history" not in select_components(spec)
        assert "retrieval_history" not in component_module_names(spec)


def test_sliding_window_alone_builds_without_a_definition(
    skeleton_data: dict[str, Any],
) -> None:
    """The two stage-23 flips are independent: sliding-window without a
    definition builds too (on the skeleton's own sqlite tier)."""
    data = _sessions_variant(
        skeleton_data, definition=None, history={"strategy": "sliding-window"}, tier="sqlite"
    )
    spec = validate_spec_data(data)
    assert unimplemented_selections(spec) == []


def test_client_tracking_sessions_gap_is_closed() -> None:
    """The stage-23 acceptance line: the client-tracking reference spec's
    `unimplemented_selections` no longer lists `session definition` or
    `history strategy`. Pinned exactly (the stage-17 outage-correlation
    pattern) so a regression in EITHER direction fails here. Stage 24 (facts
    memory) AND stage 25 (USD-budget pricing) are both merged now, so NOTHING
    remains — this is the Phase 2 exit state (client-tracking builds end to
    end); see test_client_tracking_is_fully_buildable."""
    from keep_spec import load_spec

    spec = load_spec(REPO_ROOT / "examples" / "client-tracking.yaml")
    remaining = unimplemented_selections(spec)
    assert not any("session" in item or "history" in item for item in remaining)
    assert not any("facts" in item or "memory" in item for item in remaining)
    assert remaining == []


def test_persona_learned_source_not_implemented(skeleton_data: dict[str, Any]) -> None:
    skeleton_data["spec"]["persona"]["source"] = "learned"
    spec = validate_spec_data(skeleton_data)
    with pytest.raises(ComponentNotImplementedError, match="persona source 'learned'"):
        ensure_buildable(spec)


def test_guard_reports_all_problems_at_once(skeleton_data: dict[str, Any]) -> None:
    skeleton_data["spec"]["persistence"]["tier"] = "files"
    skeleton_data["spec"]["sessions"]["definition"] = "hybrid"
    spec = validate_spec_data(skeleton_data)
    problems = unimplemented_selections(spec)
    assert len(problems) == 2


def test_redis_queue_selection_is_buildable(skeleton_data: dict[str, Any]) -> None:
    """Stage 8 flip: `gateway.queue: redis` is a real component now — buildable,
    and the selection swaps memory_queue OUT of the image (absence semantics
    cut both ways)."""
    from agent_runtime.wiring import component_module_names

    skeleton_data["spec"]["gateway"]["queue"] = "redis"
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    assert "redis-queue" in select_components(spec)
    assert "in-process-queue" not in select_components(spec)
    modules = component_module_names(spec)
    assert "redis_queue" in modules
    assert "memory_queue" not in modules


def test_message_only_triggers_are_buildable(skeleton_data: dict[str, Any]) -> None:
    """Explicitly declaring the default (message activation) stays buildable —
    and selects NO event intake (kill-switch: no event trigger declared = the
    component is absent from the image)."""
    skeleton_data["spec"]["triggers"] = {"activations": [{"kind": "message"}]}
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    from agent_runtime.wiring import component_module_names

    assert "event-intake" not in select_components(spec)
    assert "event_intake" not in component_module_names(spec)


def test_event_trigger_is_buildable_and_selects_the_intake(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage 18 flip: `kind: event-subscription` builds and ships the
    event-intake receiver alongside the channel adapter."""
    from agent_runtime.wiring import component_module_names, event_activations

    skeleton_data["spec"]["triggers"] = {
        "activations": [
            {"kind": "message"},
            {
                "kind": "event-subscription",
                "source": "alertmanager",
                "event": "alarm.raised",
                "prompt": "Triage this alarm.",
            },
        ]
    }
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    assert "event-intake" in select_components(spec)
    assert "event_intake" in component_module_names(spec)
    [activation] = event_activations(spec)
    assert activation.source == "alertmanager"
    assert activation.secretEnv == "EVENT_WEBHOOK_SECRET"  # the defaulted convention


def test_schedule_trigger_is_buildable_and_selects_the_scheduler(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage 22 flip: `kind: schedule` builds and ships the schedule-trigger
    clock loop — including beside an event trigger (the spec that previously
    pinned the guard); message-only specs select NO scheduler module
    (kill-switch is spec opt-in, absence semantics)."""
    from agent_runtime.wiring import component_module_names, schedule_activations

    skeleton_data["spec"]["triggers"] = {
        "activations": [
            {"kind": "event-subscription", "source": "alertmanager"},
            {"kind": "schedule", "cron": "0 7 * * *", "prompt": "report"},
        ]
    }
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    assert "schedule-trigger" in select_components(spec)
    assert "schedule_trigger" in component_module_names(spec)
    [activation] = schedule_activations(spec)
    assert activation.cron == "0 7 * * *"

    # message-only: the scheduler module is ABSENT, not disabled
    skeleton_data["spec"]["triggers"] = {"activations": [{"kind": "message"}]}
    bare = validate_spec_data(skeleton_data)
    assert schedule_activations(bare) == []
    assert "schedule-trigger" not in select_components(bare)
    assert "schedule_trigger" not in component_module_names(bare)


def test_client_tracking_trigger_gap_is_closed() -> None:
    """Stage-22 acceptance: the client-tracking reference spec's schedule
    activation (the weekly digest, `0 8 * * 1`) is no longer an unimplemented
    selection. Pinned exactly (stage-16/17 style) so a regression in EITHER
    direction fails here — with stages 24 (facts memory) and 25 (USD-budget
    pricing) both merged, NOTHING remains (Phase 2 exit state)."""
    from keep_spec import load_spec

    spec = load_spec(REPO_ROOT / "examples" / "client-tracking.yaml")
    remaining = unimplemented_selections(spec)
    assert not any("trigger" in gap for gap in remaining), remaining
    assert remaining == []


def test_outage_correlation_no_longer_lists_the_trigger() -> None:
    """Stage-18 acceptance: the worked example's event-subscription activation
    is no longer an unimplemented selection (stages 16/17 close the remaining
    gaps in parallel; this stage's contribution is exactly the trigger)."""
    from keep_spec import load_spec

    spec = load_spec(REPO_ROOT / "examples" / "outage-correlation.yaml")
    gaps = unimplemented_selections(spec)
    assert not any("trigger" in gap for gap in gaps), gaps
    # select_components refuses specs with ANY remaining gap, so the intake
    # selection itself is asserted on a fully-buildable variant above — the
    # guard flip is exactly this stage's claim.


def test_default_health_path_is_buildable(skeleton_data: dict[str, Any]) -> None:
    """Declaring health at /healthz stays buildable — dev_http serves exactly that."""
    skeleton_data["spec"]["observability"]["health"] = {"path": "/healthz"}
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []


def test_default_approval_with_no_tools_is_buildable(skeleton_data: dict[str, Any]) -> None:
    """The default approval policy with zero tools declared must not trip the
    guard — the skeleton (approval: {}, no tools) relies on this."""
    skeleton_data["spec"]["approval"] = {"policy": "allowlist-confirm-rest"}
    skeleton_data["spec"]["tools"] = []
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []


def test_anthropic_provider_selection_is_buildable(skeleton_data: dict[str, Any]) -> None:
    """Stage 9 flip: `models.provider: anthropic` is a real component now —
    buildable (with its API host allowlisted — stage 19, #50), and the
    selection swaps static_provider OUT of the image (absence semantics cut
    both ways)."""
    from agent_runtime.wiring import component_module_names

    skeleton_data["spec"]["models"] = {
        "provider": "anthropic",
        "anthropic": {"model": "claude-test-fixture"},
    }
    skeleton_data["spec"]["sandbox"]["egress"] = ["api.anthropic.com:443"]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    assert egress_violations(spec) == []
    assert "anthropic-provider" in select_components(spec)
    assert "static-provider" not in select_components(spec)
    modules = component_module_names(spec)
    assert "anthropic_provider" in modules
    assert "static_provider" not in modules


def test_model_tiers_are_buildable_and_select_the_router(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage 9: tiers build; the router ships with them, plus every tier's provider."""
    skeleton_data["spec"]["models"] = {
        "provider": "static",
        "static": {"script": ["ok"]},
        "tiers": [
            {
                "name": "message",
                "provider": "anthropic",
                "anthropic": {"model": "claude-test-fixture"},
            }
        ],
    }
    skeleton_data["spec"]["sandbox"]["egress"] = ["api.anthropic.com:443"]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    selected = select_components(spec)
    assert {"model-router", "static-provider", "anthropic-provider"} <= set(selected)


def test_anthropic_provider_without_egress_entry_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage 19 (#50): the anthropic provider is a spec-SELECTED component with
    a constant API host (the webex pattern) — a spec selecting it without
    covering api.anthropic.com:443 in sandbox.egress must refuse to build,
    naming the host."""
    skeleton_data["spec"]["models"] = {
        "provider": "anthropic",
        "anthropic": {"model": "claude-test-fixture"},
    }
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []  # no component gap...
    [violation] = egress_violations(spec)  # ...but the spec is inconsistent
    assert "api.anthropic.com:443" in violation
    assert "sandbox.egress" in violation
    assert "spec.models.provider" in violation
    assert mcp_egress_violations(spec) == []  # the MCP-only view stays MCP-only
    with pytest.raises(EgressCrossValidationError, match="api.anthropic.com:443"):
        ensure_buildable(spec)


def test_anthropic_tier_without_egress_entry_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """A tier-level anthropic selection needs the host too; selecting it in
    several places still yields ONE violation naming every selection site."""
    skeleton_data["spec"]["models"] = {
        "provider": "anthropic",
        "anthropic": {"model": "claude-test-fixture"},
        "tiers": [
            {
                "name": "reasoning",
                "provider": "anthropic",
                "anthropic": {"model": "claude-test-fixture"},
            }
        ],
    }
    spec = validate_spec_data(skeleton_data)
    [violation] = egress_violations(spec)
    assert "api.anthropic.com:443" in violation
    assert "spec.models.provider" in violation
    assert "spec.models.tiers['reasoning']" in violation
    with pytest.raises(EgressCrossValidationError, match="api.anthropic.com:443"):
        ensure_buildable(spec)


def test_static_provider_needs_no_anthropic_egress(skeleton_data: dict[str, Any]) -> None:
    """The static provider is local — the skeleton's empty egress stays valid."""
    spec = validate_spec_data(skeleton_data)
    assert spec.spec.models.provider == "static"
    assert egress_violations(spec) == []
    ensure_buildable(spec)  # must not raise


def test_anthropic_egress_host_matches_the_adapter_constant() -> None:
    """Keep wiring's ANTHROPIC_API_HOST synced with the adapter's
    DEFAULT_BASE_URL, the way WEBEX_API_HOST mirrors webex_channel's."""
    from urllib.parse import urlsplit

    from agent_runtime.components import anthropic_provider
    from agent_runtime.wiring import ANTHROPIC_API_HOST, ANTHROPIC_API_PORT

    parts = urlsplit(anthropic_provider.DEFAULT_BASE_URL)
    assert parts.hostname == ANTHROPIC_API_HOST
    assert (parts.port or 443) == ANTHROPIC_API_PORT


def test_reference_specs_stay_egress_consistent() -> None:
    """Both reference specs already allowlist api.anthropic.com:443 — the new
    provider target introduces no violation (build-time tightening must admit
    every in-repo spec)."""
    from keep_spec import load_spec

    for name in ("outage-correlation.yaml", "client-tracking.yaml"):
        spec = load_spec(REPO_ROOT / "examples" / name)
        assert egress_violations(spec) == [], name


def test_token_budget_is_buildable_and_selects_the_router(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage 9: a per-session token cap builds (both onExceed modes)."""
    for on_exceed in ("block", "warn"):
        skeleton_data["spec"]["models"] = {
            "provider": "static",
            "static": {"script": ["ok"]},
            "budgets": {"maxTokensPerSession": 500, "onExceed": on_exceed},
        }
        spec = validate_spec_data(skeleton_data)
        assert unimplemented_selections(spec) == []
        assert "model-router" in select_components(spec)


def test_router_is_absent_without_tiers_or_budgets(skeleton_data: dict[str, Any]) -> None:
    """A single-provider, unbudgeted spec ships NO router module (absence)."""
    spec = validate_spec_data(skeleton_data)
    assert "model-router" not in select_components(spec)


def test_static_provider_usd_budget_refused_at_load(skeleton_data: dict[str, Any]) -> None:
    """Stage 25 static-provider decision: pricing is anthropic-only. The static
    provider has no declarable price, so a USD budget over a static path is
    refused at LOAD (cross-validation) naming the path — NOT a buildable but
    decorative selection. (Pricing a hermetic fixture at 0.0 would let a USD
    budget 'build' and never trigger — exactly the decorative state this stage
    eliminates.)"""
    import pydantic

    skeleton_data["spec"]["models"]["budgets"] = {"maxUsdPerSession": 2.0}
    with pytest.raises(pydantic.ValidationError, match="maxUsdPerSession") as excinfo:
        validate_spec_data(skeleton_data)
    assert "static" in str(excinfo.value)


def test_usd_budget_with_pricing_is_buildable(skeleton_data: dict[str, Any]) -> None:
    """Stage 25 flips the USD-budget guard: a USD budget over an all-anthropic,
    fully-priced model section validates AND builds — `model-router` ships and
    there is no unimplemented selection for the budget."""
    skeleton_data["spec"]["models"] = {
        "provider": "anthropic",
        "anthropic": {
            "model": "claude-sonnet-4-5",
            "pricing": {"usdPerMillionInputTokens": 3.0, "usdPerMillionOutputTokens": 15.0},
        },
        "budgets": {"maxUsdPerSession": 2.0, "maxTokensPerSession": 500},
    }
    skeleton_data["spec"]["sandbox"] = {"profile": "container", "egress": ["api.anthropic.com"]}
    spec = validate_spec_data(skeleton_data)
    assert spec.spec.models.budgets is not None
    assert spec.spec.models.budgets.maxUsdPerSession == 2.0
    assert unimplemented_selections(spec) == []
    ensure_buildable(spec)  # must not raise
    assert "model-router" in select_components(spec)


def _webex_channel() -> dict[str, Any]:
    return {
        "type": "webex",
        "transport": "webhook",
        "verification": {"method": "signature", "secretEnv": "WEBEX_WEBHOOK_SECRET"},
    }


def test_webex_channel_is_buildable_with_egress_and_roster(skeleton_data: dict[str, Any]) -> None:
    """Stage 10 flip: a webex channel + a tiered roster + egress covering the
    WebEx API host is fully buildable, and the webex_channel module ships."""
    from agent_runtime.wiring import component_module_names

    skeleton_data["spec"]["channels"] = [_webex_channel()]
    skeleton_data["spec"]["gateway"]["allowlist"] = {
        "policy": "tiered",
        "roster": [{"id": "webex:nina@example.com", "tier": "owner"}],
    }
    skeleton_data["spec"]["sandbox"]["egress"] = ["webexapis.com:443"]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    ensure_buildable(spec)  # must not raise
    assert "webex-channel" in select_components(spec)
    assert "webex_channel" in component_module_names(spec)
    assert "dev_http" not in component_module_names(spec)  # absence cuts both ways


def test_webex_channel_without_egress_entry_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """The WebEx reply host must be covered by sandbox.egress — an empty egress
    fails ensure_buildable naming the host and the fix (like an HTTP MCP host)."""
    skeleton_data["spec"]["channels"] = [_webex_channel()]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []  # the component gap is gone...
    [violation] = egress_violations(spec)  # ...but the spec is inconsistent
    assert "webexapis.com:443" in violation
    assert "sandbox.egress" in violation
    assert mcp_egress_violations(spec) == []  # generalization left MCP-only view empty
    with pytest.raises(EgressCrossValidationError, match="webexapis.com:443"):
        ensure_buildable(spec)


def test_webex_token_verification_stays_guarded(skeleton_data: dict[str, Any]) -> None:
    """Only signature verification is implemented for webex; token/none stay guarded."""
    channel = _webex_channel()
    channel["verification"] = {"method": "token", "secretEnv": "WEBEX_BOT_TOKEN"}
    skeleton_data["spec"]["channels"] = [channel]
    skeleton_data["spec"]["sandbox"]["egress"] = ["webexapis.com:443"]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == [
        "webex verification method 'token' (spec.channels[0].verification.method)"
    ]


def _slack_channel() -> dict[str, Any]:
    return {
        "type": "slack",
        "transport": "webhook",
        "verification": {"method": "signature", "secretEnv": "SLACK_SIGNING_SECRET"},
    }


def test_slack_channel_is_buildable_with_egress_and_roster(skeleton_data: dict[str, Any]) -> None:
    """Stage 21 flip: a slack channel (webhook + signature) + a roster + egress
    covering the Slack Web API host is fully buildable, and the slack_channel
    module ships — while dev_http and webex_channel do NOT (absence cuts both
    ways)."""
    from agent_runtime.wiring import component_module_names

    skeleton_data["spec"]["channels"] = [_slack_channel()]
    skeleton_data["spec"]["gateway"]["allowlist"] = {
        "policy": "owner-only",
        "roster": [{"id": "slack:U024E0WNER", "tier": "owner"}],
    }
    skeleton_data["spec"]["sandbox"]["egress"] = ["slack.com:443"]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    ensure_buildable(spec)  # must not raise
    assert "slack-channel" in select_components(spec)
    modules = component_module_names(spec)
    assert "slack_channel" in modules
    assert "dev_http" not in modules
    assert "webex_channel" not in modules


def test_slack_channel_without_egress_entry_fails_cross_validation(
    skeleton_data: dict[str, Any],
) -> None:
    """The Slack reply host must be covered by sandbox.egress — an empty egress
    fails ensure_buildable naming the host and the fix (the WebEx precedent)."""
    skeleton_data["spec"]["channels"] = [_slack_channel()]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []  # the component gap is gone...
    [violation] = egress_violations(spec)  # ...but the spec is inconsistent
    assert "slack.com:443" in violation
    assert "sandbox.egress" in violation
    assert mcp_egress_violations(spec) == []  # the MCP-only view stays MCP-only
    with pytest.raises(EgressCrossValidationError, match="slack.com:443"):
        ensure_buildable(spec)


@pytest.mark.parametrize("transport", ["websocket", "polling"])
def test_slack_non_webhook_transports_stay_guarded(
    skeleton_data: dict[str, Any], transport: str
) -> None:
    """Scope guard: exactly `transport: webhook` flipped; Socket Mode and
    polling keep loud, specific guards."""
    channel = _slack_channel()
    channel["transport"] = transport
    skeleton_data["spec"]["channels"] = [channel]
    skeleton_data["spec"]["sandbox"]["egress"] = ["slack.com:443"]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == [
        f"slack transport '{transport}' (spec.channels[0].transport)"
    ]
    with pytest.raises(ComponentNotImplementedError, match=f"slack transport '{transport}'"):
        ensure_buildable(spec)


@pytest.mark.parametrize(
    "verification",
    [
        {"method": "token", "secretEnv": "SLACK_BOT_TOKEN"},
        {"method": "none"},
    ],
)
def test_slack_non_signature_verification_stays_guarded(
    skeleton_data: dict[str, Any], verification: dict[str, Any]
) -> None:
    """Only signature verification is implemented for slack; token/none stay
    guarded (an unverified Events endpoint is an unenforced declaration)."""
    channel = _slack_channel()
    channel["verification"] = verification
    skeleton_data["spec"]["channels"] = [channel]
    skeleton_data["spec"]["sandbox"]["egress"] = ["slack.com:443"]
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == [
        f"slack verification method '{verification['method']}' "
        "(spec.channels[0].verification.method)"
    ]


def test_non_slack_spec_excludes_slack_module(skeleton_data: dict[str, Any]) -> None:
    """Absence: the skeleton (dev-http) ships no slack_channel module."""
    from agent_runtime.wiring import component_module_names

    spec = validate_spec_data(skeleton_data)
    assert "slack-channel" not in select_components(spec)
    assert "slack_channel" not in component_module_names(spec)


def test_client_tracking_channel_gap_is_closed() -> None:
    """The stage-21 acceptance line: the client-tracking reference spec's
    `unimplemented_selections` no longer lists the channel adapter. The
    remainder is pinned EXACTLY (stage-17 precedent) so a regression in either
    direction — the channel guard back, or another guard silently dropped —
    fails here. With stages 24 (facts memory) and 25 (USD-budget pricing) both
    merged, NOTHING remains (Phase 2 exit state)."""
    from keep_spec import load_spec

    spec = load_spec(REPO_ROOT / "examples" / "client-tracking.yaml")
    remaining = unimplemented_selections(spec)
    assert not any("channel" in item for item in remaining)
    assert remaining == []


@pytest.mark.parametrize("policy", ["owner-only", "tiered"])
def test_allowlist_policies_are_buildable(skeleton_data: dict[str, Any], policy: str) -> None:
    """owner-only and tiered rosters build (stage 10); the gate is generic, so a
    dev-http channel gains enforcement without any platform channel."""
    skeleton_data["spec"]["gateway"]["allowlist"] = {
        "policy": policy,
        "roster": [{"id": "dev-http:owner-1", "tier": "owner"}],
    }
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    ensure_buildable(spec)  # must not raise


def test_non_webex_spec_excludes_webex_module(skeleton_data: dict[str, Any]) -> None:
    """Absence: the skeleton (dev-http) ships no webex_channel module."""
    from agent_runtime.wiring import component_module_names

    spec = validate_spec_data(skeleton_data)
    assert "webex-channel" not in select_components(spec)
    assert "webex_channel" not in component_module_names(spec)


def test_runner_refuses_unbuildable_spec_at_boot(skeleton_data: dict[str, Any]) -> None:
    """Boot-time honesty: build_app must refuse a schema-valid spec the library
    cannot build. Removing ensure_buildable from runner.build_app fails this test
    (the 'files' persistence tier is not consulted while wiring components, so
    only the guard can catch it)."""
    from agent_runtime.runner import build_app

    skeleton_data["spec"]["persistence"]["tier"] = "files"
    spec = validate_spec_data(skeleton_data)
    with pytest.raises(ComponentNotImplementedError, match="persistence tier 'files'"):
        build_app(spec)


def test_postgres_persistence_selection_is_buildable(skeleton_data: dict[str, Any]) -> None:
    """Stage 15 flip: `persistence.tier: postgres` is a real component now —
    buildable, and the selection swaps the in-memory single_session module OUT
    of the image (absence semantics cut both ways, like the redis flip).
    Stage 20 extends the absence: a postgres image carries no sqlite component
    either."""
    from agent_runtime.wiring import component_module_names

    skeleton_data["spec"]["persistence"]["tier"] = "postgres"
    spec = validate_spec_data(skeleton_data)
    assert unimplemented_selections(spec) == []
    assert "postgres-persistence" in select_components(spec)
    assert "single-session" not in select_components(spec)
    assert "sqlite-persistence" not in select_components(spec)
    modules = component_module_names(spec)
    assert "postgres_persistence" in modules
    assert "single_session" not in modules
    assert "sqlite_persistence" not in modules


def test_sqlite_persistence_selection_is_real_and_flips_absence(
    skeleton_data: dict[str, Any],
) -> None:
    """Stage 20 flip: `persistence.tier: sqlite` (the skeleton's own selection)
    now selects the FILE-BACKED sqlite component — and neither the in-process
    single_session module nor the postgres component rides along (absence both
    ways). After this stage NO buildable spec silently gets memory-only
    sessions: the durability declaration in the spec is honored or the boot
    refuses."""
    from agent_runtime.wiring import component_module_names

    spec = validate_spec_data(skeleton_data)  # tier: sqlite is the skeleton default
    assert unimplemented_selections(spec) == []
    selected = select_components(spec)
    assert "sqlite-persistence" in selected
    assert "single-session" not in selected
    assert "postgres-persistence" not in selected
    modules = component_module_names(spec)
    assert "sqlite_persistence" in modules
    assert "single_session" not in modules
    assert "postgres_persistence" not in modules

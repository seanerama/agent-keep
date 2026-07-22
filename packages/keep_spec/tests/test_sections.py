"""Per-section unit tests for the stage-2 keep/v1 schema.

For each section: a valid fixture passes; strict-validation failures (unknown
field, bad enum value, broken cross-field rule) fail loudly with a useful
message.
"""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from keep_spec import load_spec, validate_spec_data

FIXTURES = Path(__file__).parent / "fixtures"
FULL_FEATURED = FIXTURES / "full-featured.yaml"
SCHEDULED_REPORTER = FIXTURES / "scheduled-reporter.yaml"


@pytest.fixture
def full_data() -> dict[str, Any]:
    with open(FULL_FEATURED, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def _rejects(data: dict[str, Any], *message_parts: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_spec_data(data)
    text = str(excinfo.value)
    for part in message_parts:
        assert part in text, f"expected {part!r} in validation error:\n{text}"


# ---------------------------------------------------------------- fixtures validate


def test_full_featured_fixture_validates() -> None:
    spec = load_spec(FULL_FEATURED)
    assert spec.metadata.slug == "full-featured"
    assert spec.spec.triggers is not None
    assert [a.kind for a in spec.spec.triggers.activations] == [
        "message",
        "schedule",
        "event-subscription",
    ]
    assert [c.type for c in spec.spec.channels] == ["dev-http", "discord", "slack", "webex", "sms"]
    assert spec.spec.memory is not None
    assert spec.spec.memory.structure.kind == "layered"
    assert spec.spec.memory.structure.store == "pgvector"
    assert spec.spec.models.provider == "anthropic"
    assert [t.name for t in spec.spec.models.tiers] == ["triage", "reasoning"]


def test_scheduled_reporter_fixture_validates() -> None:
    spec = load_spec(SCHEDULED_REPORTER)
    assert spec.spec.sessions.definition == "per-user"
    assert spec.spec.sessions.history is not None
    assert spec.spec.sessions.history.strategy == "sliding-window"
    assert spec.spec.approval.policy == "everything"
    assert spec.spec.persistence.tier == "files"


# ---------------------------------------------------------------------------- persona


def test_persona_both_requires_precedence(full_data: dict[str, Any]) -> None:
    del full_data["spec"]["persona"]["precedence"]
    _rejects(full_data, "persona.precedence is required")


def test_persona_precedence_only_with_both(full_data: dict[str, Any]) -> None:
    full_data["spec"]["persona"]["source"] = "static"
    _rejects(full_data, "only meaningful when persona.source is 'both'")


def test_persona_bad_source_enum(full_data: dict[str, Any]) -> None:
    full_data["spec"]["persona"]["source"] = "telepathy"
    _rejects(full_data, "source")


# --------------------------------------------------------------------------- triggers


def test_trigger_unknown_kind(full_data: dict[str, Any]) -> None:
    full_data["spec"]["triggers"]["activations"].append({"kind": "carrier-pigeon"})
    _rejects(full_data, "kind")


def test_trigger_bad_cron(full_data: dict[str, Any]) -> None:
    full_data["spec"]["triggers"]["activations"][1]["cron"] = "0 7 * *"  # four fields
    _rejects(full_data, "cron")


def test_trigger_empty_activations(full_data: dict[str, Any]) -> None:
    full_data["spec"]["triggers"]["activations"] = []
    _rejects(full_data, "activations")


def test_event_trigger_secret_env_defaults_to_the_convention(full_data: dict[str, Any]) -> None:
    """`secretEnv` (v1 additive amendment, stage 18) defaults to the fixed
    conventional name, so every pre-amendment document validates unchanged and
    round-trips losslessly (the defaulted field stays out of dumps)."""
    from keep_spec import dump_spec_data

    spec = validate_spec_data(full_data)
    assert spec.spec.triggers is not None
    event = spec.spec.triggers.activations[2]
    assert event.kind == "event-subscription"
    assert event.secretEnv == "EVENT_WEBHOOK_SECRET"
    dumped = dump_spec_data(spec)
    assert "secretEnv" not in dumped["spec"]["triggers"]["activations"][2]


def test_event_trigger_secret_env_is_declarable(full_data: dict[str, Any]) -> None:
    full_data["spec"]["triggers"]["activations"][2]["secretEnv"] = "ALARM_FEED_SECRET"
    spec = validate_spec_data(full_data)
    assert spec.spec.triggers is not None
    assert spec.spec.triggers.activations[2].secretEnv == "ALARM_FEED_SECRET"


def test_event_trigger_secret_env_must_name_an_env_var(full_data: dict[str, Any]) -> None:
    # The spec names secrets by env var NAME, never value (contract rule 3).
    full_data["spec"]["triggers"]["activations"][2]["secretEnv"] = "not-an-env-var"
    _rejects(full_data, "secretEnv")


# --------------------------------------------------------------------------- channels


def test_channel_unknown_type(full_data: dict[str, Any]) -> None:
    full_data["spec"]["channels"].append({"type": "telegram"})
    _rejects(full_data, "type")


def test_channel_unknown_field(full_data: dict[str, Any]) -> None:
    full_data["spec"]["channels"][2]["workspace"] = "acme"
    _rejects(full_data, "workspace")


def test_channel_verification_requires_secret_env_name(full_data: dict[str, Any]) -> None:
    full_data["spec"]["channels"][2]["verification"] = {"method": "signature"}
    _rejects(full_data, "requires secretEnv")


def test_channel_verification_none_forbids_secret(full_data: dict[str, Any]) -> None:
    full_data["spec"]["channels"][4]["verification"] = {
        "method": "none",
        "secretEnv": "SMS_WEBHOOK_SECRET",
    }
    _rejects(full_data, "must not name a secretEnv")


def test_channel_secret_env_is_a_name_not_a_value(full_data: dict[str, Any]) -> None:
    # binding rule 3: the spec names secrets; a value-looking string fails the pattern
    full_data["spec"]["channels"][1]["verification"]["secretEnv"] = "xoxb-actual-secret"
    _rejects(full_data, "secretEnv")


def test_dev_http_channel_rejects_bad_transport(full_data: dict[str, Any]) -> None:
    full_data["spec"]["channels"][0]["transport"] = "websocket"
    _rejects(full_data, "transport")


# ---------------------------------------------------------------------------- gateway


def test_gateway_bad_allowlist_policy(full_data: dict[str, Any]) -> None:
    full_data["spec"]["gateway"]["allowlist"]["policy"] = "everyone"
    _rejects(full_data, "policy")


def test_gateway_bad_concurrency(full_data: dict[str, Any]) -> None:
    full_data["spec"]["gateway"]["concurrency"] = "parallel"
    _rejects(full_data, "concurrency")


def test_gateway_bad_identity_unification(full_data: dict[str, Any]) -> None:
    full_data["spec"]["gateway"]["identityUnification"] = "guess"
    _rejects(full_data, "identityUnification")


def test_gateway_roster_bad_tier(full_data: dict[str, Any]) -> None:
    full_data["spec"]["gateway"]["allowlist"]["roster"][0]["tier"] = "root"
    _rejects(full_data, "tier")


def test_gateway_roster_exact_duplicate_is_rejected(full_data: dict[str, Any]) -> None:
    """Stage 19 (#58): a byte-equal duplicate roster id fails `foundry validate`
    (schema level), not AllowlistGate construction (boot) — even when the
    duplicate declares a different tier (last-wins would let roster ORDER
    silently decide admission)."""
    roster = full_data["spec"]["gateway"]["allowlist"]["roster"]
    duplicate_id: str = roster[0]["id"]
    roster.append({"id": duplicate_id, "tier": "guest"})
    _rejects(full_data, "duplicate", duplicate_id)


def test_gateway_roster_case_variant_duplicates_still_pass_schema(
    full_data: dict[str, Any],
) -> None:
    """Pinned: the schema rejects EXACT (byte-equal) duplicates ONLY.
    Case-variant entries — which the gateway's roster normalization (the '@'
    heuristic + ascii_lower) collides at gate construction — must PASS schema
    validation: normalization is the gate's security transform, and the schema
    must not carry a second implementation of it."""
    roster = full_data["spec"]["gateway"]["allowlist"]["roster"]
    roster.append({"id": "webex:Nina@Example.com", "tier": "trusted"})
    roster.append({"id": "webex:nina@example.com", "tier": "trusted"})
    validate_spec_data(full_data)  # must not raise


# --------------------------------------------------------------------------- sessions


def test_sessions_bad_definition(full_data: dict[str, Any]) -> None:
    full_data["spec"]["sessions"]["definition"] = "global"
    _rejects(full_data, "definition")


def test_sessions_unknown_history_strategy(full_data: dict[str, Any]) -> None:
    full_data["spec"]["sessions"]["history"] = {"strategy": "forget-everything"}
    _rejects(full_data, "strategy")


def test_sessions_history_bad_window(full_data: dict[str, Any]) -> None:
    full_data["spec"]["sessions"]["history"] = {"strategy": "sliding-window", "maxTurns": 0}
    _rejects(full_data, "maxTurns")


def test_sessions_history_unknown_field_for_variant(full_data: dict[str, Any]) -> None:
    # maxTurns belongs to sliding-window, not layered — strict per variant
    full_data["spec"]["sessions"]["history"]["maxTurns"] = 10
    _rejects(full_data, "maxTurns")


# ----------------------------------------------------------------------------- memory


def test_memory_corpus_accepts_known_values(full_data: dict[str, Any]) -> None:
    from keep_spec.models import LayeredMemory, VectorMemory

    for corpus in ("agent-summaries", "transcripts", "documents"):
        full_data["spec"]["memory"]["structure"]["corpus"] = corpus
        spec = validate_spec_data(full_data)
        assert spec.spec.memory is not None
        structure = spec.spec.memory.structure
        assert isinstance(structure, LayeredMemory)
        assert structure.corpus == corpus
    # corpus rides vectors too, not just layered
    full_data["spec"]["memory"]["structure"] = {
        "kind": "vectors",
        "store": "sqlite-vec",
        "corpus": "agent-summaries",
    }
    spec = validate_spec_data(full_data)
    assert spec.spec.memory is not None
    structure = spec.spec.memory.structure
    assert isinstance(structure, VectorMemory)
    assert structure.corpus == "agent-summaries"


def test_memory_corpus_absent_by_default(full_data: dict[str, Any]) -> None:
    """Kill-switch/dark-launch: corpus defaults to absent — no behavior change."""
    from keep_spec.models import LayeredMemory

    spec = validate_spec_data(full_data)
    assert spec.spec.memory is not None
    structure = spec.spec.memory.structure
    assert isinstance(structure, LayeredMemory)
    assert structure.corpus is None


def test_memory_corpus_unknown_value_rejected(full_data: dict[str, Any]) -> None:
    full_data["spec"]["memory"]["structure"]["corpus"] = "everything"
    _rejects(full_data, "corpus")


def test_memory_facts_forbids_corpus(full_data: dict[str, Any]) -> None:
    # corpus scopes the vector layer; structured facts have nothing to embed
    full_data["spec"]["memory"]["structure"] = {"kind": "facts", "corpus": "documents"}
    _rejects(full_data, "corpus")


def test_memory_facts_forbids_vector_store(full_data: dict[str, Any]) -> None:
    full_data["spec"]["memory"]["structure"] = {"kind": "facts", "store": "pgvector"}
    _rejects(full_data, "store")


def test_memory_vectors_requires_real_store(full_data: dict[str, Any]) -> None:
    full_data["spec"]["memory"]["structure"] = {"kind": "vectors", "store": "none"}
    _rejects(full_data, "store")


def test_memory_bad_write_policy(full_data: dict[str, Any]) -> None:
    full_data["spec"]["memory"]["writePolicy"] = "anyone"
    _rejects(full_data, "writePolicy")


# ----------------------------------------------------------------------------- skills


def test_skill_keyword_selection_requires_keywords(full_data: dict[str, Any]) -> None:
    full_data["spec"]["skills"][0]["keywords"] = []
    _rejects(full_data, "requires non-empty keywords")


def test_skill_keywords_only_with_keyword_selection(full_data: dict[str, Any]) -> None:
    full_data["spec"]["skills"][1]["keywords"] = ["style"]
    _rejects(full_data, "only meaningful with")


def test_skill_bad_selection(full_data: dict[str, Any]) -> None:
    full_data["spec"]["skills"][2]["selection"] = "random"
    _rejects(full_data, "selection")


# ------------------------------------------------------------------------------ tools


def test_tool_server_requires_grants(full_data: dict[str, Any]) -> None:
    full_data["spec"]["tools"][0]["allow"] = []
    _rejects(full_data, "allow")


def test_tool_bad_scope(full_data: dict[str, Any]) -> None:
    full_data["spec"]["tools"][0]["allow"][0]["scope"] = "admin"
    _rejects(full_data, "scope")


def test_tool_bad_url(full_data: dict[str, Any]) -> None:
    full_data["spec"]["tools"][0]["transport"]["url"] = "ftp://ha.internal"
    _rejects(full_data, "url")


def test_tool_unknown_transport_kind(full_data: dict[str, Any]) -> None:
    full_data["spec"]["tools"][1]["transport"] = {"kind": "carrier-pigeon"}
    _rejects(full_data, "kind")


def test_tool_secret_envs_are_names_not_values(full_data: dict[str, Any]) -> None:
    # binding rule 3: the spec names secrets; a value-looking string fails the pattern
    full_data["spec"]["tools"][0]["secretEnvs"] = ["xoxb-1234-actual-secret"]
    _rejects(full_data, "secretEnvs", "String should match pattern")


def test_tool_url_rejects_embedded_credentials(full_data: dict[str, Any]) -> None:
    full_data["spec"]["tools"][0]["transport"]["url"] = "https://user:hunter2@ha.internal/mcp"
    _rejects(full_data, "embeds credentials")


def test_tool_local_transport_accepted(full_data: dict[str, Any]) -> None:
    """Stage-6 additive amendment: 'local' joins the transport discriminator."""
    full_data["spec"]["tools"][0]["transport"] = {"kind": "local"}
    spec = validate_spec_data(full_data)
    assert spec.spec.tools[0].transport.kind == "local"


def test_tool_local_transport_is_strict(full_data: dict[str, Any]) -> None:
    """A local transport declares nothing but its kind — no command, no url."""
    full_data["spec"]["tools"][0]["transport"] = {"kind": "local", "command": "sh"}
    _rejects(full_data, "command")


# -------------------------------------------------- per-grant constraints (stage 4)


def test_tool_grant_constraints_accept_validated_scalars(full_data: dict[str, Any]) -> None:
    full_data["spec"]["tools"][0]["allow"][1]["constraints"] = {
        "room": "noc-outages",
        "max_results": 5,
        "dry_run": False,
    }
    spec = validate_spec_data(full_data)
    grant = spec.spec.tools[0].allow[1]
    assert grant.constraints == {"room": "noc-outages", "max_results": 5, "dry_run": False}
    assert isinstance(grant.constraints["max_results"], int)
    assert isinstance(grant.constraints["dry_run"], bool)


def test_tool_grant_constraints_absent_by_default(full_data: dict[str, Any]) -> None:
    """Kill-switch/dark-launch: constraints default to absent — no behavior change."""
    spec = validate_spec_data(full_data)
    assert all(grant.constraints is None for server in spec.spec.tools for grant in server.allow)


@pytest.mark.parametrize(
    "value",
    [
        "the noc outages room",  # prose with spaces
        "noc\noutages",  # newline
        "page the room,\nthen escalate to the on-call engineer",  # multi-line prose
        "sk-" + "A1b2c3D4" * 10,  # secret-shaped long token (over 64 chars)
        "",  # empty string pins nothing
        2.5,  # float is not a validated scalar
        2.0,  # even an integral float — strict scalars, no silent coercion
        ["noc-outages"],  # non-scalar
        {"nested": "no"},  # non-scalar
        None,  # a pin must pin something
    ],
    ids=[
        "spaces",
        "newline",
        "prose",
        "secret-token",
        "empty",
        "float",
        "float-int",
        "list",
        "dict",
        "null",
    ],
)
def test_tool_grant_constraint_bad_value_rejected(full_data: dict[str, Any], value: Any) -> None:
    full_data["spec"]["tools"][0]["allow"][0]["constraints"] = {"room": value}
    _rejects(full_data, "constraints")


def test_tool_grant_constraint_bad_name_rejected(full_data: dict[str, Any]) -> None:
    # constraint names are tool-parameter-ish identifiers, not prose
    full_data["spec"]["tools"][0]["allow"][0]["constraints"] = {"room name": "noc-outages"}
    _rejects(full_data, "constraints")


def test_tool_grant_empty_constraints_rejected(full_data: dict[str, Any]) -> None:
    # exhaustive positive declaration: an empty mapping pins nothing
    full_data["spec"]["tools"][0]["allow"][0]["constraints"] = {}
    _rejects(full_data, "constraints")


# --------------------------------------------------------------------------- approval


def test_approval_auto_approve_needs_allowlist_policy(full_data: dict[str, Any]) -> None:
    full_data["spec"]["approval"]["policy"] = "everything"
    _rejects(full_data, "autoApprove")


def test_approval_bad_policy(full_data: dict[str, Any]) -> None:
    full_data["spec"]["approval"]["policy"] = "yolo"
    _rejects(full_data, "policy")


def test_auto_approve_entry_must_be_server_dot_tool(full_data: dict[str, Any]) -> None:
    # prose is not a '<server>.<tool>' name — rejected by pattern
    full_data["spec"]["approval"]["autoApprove"].append("approve everything please")
    _rejects(full_data, "autoApprove")


def test_auto_approve_entry_without_server_part_rejected(full_data: dict[str, Any]) -> None:
    full_data["spec"]["approval"]["autoApprove"].append("get_state")
    _rejects(full_data, "autoApprove")


def test_auto_approve_must_name_a_declared_grant(full_data: dict[str, Any]) -> None:
    # pattern-valid, but no such grant exists in spec.tools — cross-validation
    # fails with a message naming the dangling entry
    full_data["spec"]["approval"]["autoApprove"].append("nonexistent.tool")
    _rejects(full_data, "nonexistent.tool", "not declared in spec.tools")


def test_auto_approve_valid_entries_pass(full_data: dict[str, Any]) -> None:
    # the fixture's entries name declared grants; adding another declared one is fine
    full_data["spec"]["approval"]["autoApprove"].append("home-assistant.call_service")
    spec = validate_spec_data(full_data)
    assert "home-assistant.call_service" in spec.spec.approval.autoApprove


# ---------------------------------------------------------------------------- sandbox


def test_sandbox_bad_profile(full_data: dict[str, Any]) -> None:
    full_data["spec"]["sandbox"]["profile"] = "host"
    _rejects(full_data, "profile")


def test_sandbox_bad_egress_entry(full_data: dict[str, Any]) -> None:
    full_data["spec"]["sandbox"]["egress"].append("http://not a host")
    _rejects(full_data, "egress")


def test_sandbox_egress_port_out_of_range(full_data: dict[str, Any]) -> None:
    full_data["spec"]["sandbox"]["egress"].append("example.com:99999")
    _rejects(full_data, "port 99999 outside 1-65535")


# ----------------------------------------------------------------------------- models


def test_models_provider_requires_matching_config(full_data: dict[str, Any]) -> None:
    del full_data["spec"]["models"]["anthropic"]
    _rejects(full_data, "requires a 'anthropic' config block")


def test_models_unselected_provider_config_rejected(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["static"] = {"script": ["hi"]}
    _rejects(full_data, "unselected provider")


def test_models_tier_requires_matching_config(full_data: dict[str, Any]) -> None:
    del full_data["spec"]["models"]["tiers"][1]["anthropic"]
    _rejects(full_data, "requires a 'anthropic' config block")


def test_models_duplicate_tier_names(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["tiers"][1]["name"] = "triage"
    full_data["spec"]["models"]["tiers"][1]["provider"] = "static"
    full_data["spec"]["models"]["tiers"][1]["static"] = {"script": ["x"]}
    del full_data["spec"]["models"]["tiers"][1]["anthropic"]
    _rejects(full_data, "duplicate tier name")


def test_models_bad_budget(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["budgets"]["maxUsdPerSession"] = 0
    _rejects(full_data, "maxUsdPerSession")


def test_models_anthropic_max_tokens_accepted(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["anthropic"]["maxTokens"] = 1024
    spec = validate_spec_data(full_data)
    assert spec.spec.models.anthropic is not None
    assert spec.spec.models.anthropic.maxTokens == 1024


def test_models_anthropic_max_tokens_absent_defaults_to_none(full_data: dict[str, Any]) -> None:
    spec = validate_spec_data(full_data)
    assert spec.spec.models.anthropic is not None
    assert spec.spec.models.anthropic.maxTokens is None


def test_models_anthropic_max_tokens_zero_rejected(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["anthropic"]["maxTokens"] = 0
    _rejects(full_data, "maxTokens")


def test_models_anthropic_max_tokens_negative_rejected(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["anthropic"]["maxTokens"] = -1
    _rejects(full_data, "maxTokens")


def test_models_anthropic_max_tokens_non_int_rejected(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["anthropic"]["maxTokens"] = "plenty"
    _rejects(full_data, "maxTokens")


def test_models_anthropic_max_tokens_over_ceiling_rejected(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["anthropic"]["maxTokens"] = 128_001
    _rejects(full_data, "maxTokens")


# --------------------------------------------------------------- model pricing (st. 25)


def test_models_pricing_both_fields_accepted(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["anthropic"]["pricing"] = {
        "usdPerMillionInputTokens": 3.0,
        "usdPerMillionOutputTokens": 15.0,
    }
    spec = validate_spec_data(full_data)
    assert spec.spec.models.anthropic is not None
    pricing = spec.spec.models.anthropic.pricing
    assert pricing is not None
    assert pricing.usdPerMillionInputTokens == 3.0
    assert pricing.usdPerMillionOutputTokens == 15.0


def test_models_pricing_absent_defaults_to_none(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["anthropic"].pop("pricing", None)
    spec = validate_spec_data(full_data)
    assert spec.spec.models.anthropic is not None
    assert spec.spec.models.anthropic.pricing is None


def test_models_pricing_one_sided_rejected_by_model_validator(full_data: dict[str, Any]) -> None:
    # Only input declared — the both-or-neither model validator rejects it.
    full_data["spec"]["models"]["anthropic"]["pricing"] = {"usdPerMillionInputTokens": 3.0}
    _rejects(full_data, "both", "usdPerMillionOutputTokens")


def test_models_pricing_zero_rejected(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["anthropic"]["pricing"] = {
        "usdPerMillionInputTokens": 0,
        "usdPerMillionOutputTokens": 15.0,
    }
    _rejects(full_data, "usdPerMillionInputTokens")


def test_models_pricing_negative_rejected(full_data: dict[str, Any]) -> None:
    full_data["spec"]["models"]["anthropic"]["pricing"] = {
        "usdPerMillionInputTokens": 3.0,
        "usdPerMillionOutputTokens": -1.0,
    }
    _rejects(full_data, "usdPerMillionOutputTokens")


def test_usd_budget_requires_pricing_on_every_path_naming_the_tier(
    full_data: dict[str, Any],
) -> None:
    """A USD budget with an unpriced tier is refused at load, naming the tier
    (the autoApprove cross-check precedent). full-featured's `triage` tier is
    static (unpriceable), so adding a USD budget refuses and names it."""
    full_data["spec"]["models"]["budgets"]["maxUsdPerSession"] = 2.0
    _rejects(full_data, "maxUsdPerSession", "models.tiers['triage']", "static")


def test_usd_budget_with_all_paths_priced_accepted(full_data: dict[str, Any]) -> None:
    """USD budget accepted once every selectable path declares pricing — here
    an all-anthropic section (no static tier) with pricing on each path."""
    full_data["spec"]["models"] = {
        "provider": "anthropic",
        "anthropic": {
            "model": "claude-haiku-4-5",
            "pricing": {"usdPerMillionInputTokens": 1.0, "usdPerMillionOutputTokens": 5.0},
        },
        "tiers": [
            {
                "name": "reasoning",
                "provider": "anthropic",
                "anthropic": {
                    "model": "claude-opus-4-1",
                    "pricing": {
                        "usdPerMillionInputTokens": 15.0,
                        "usdPerMillionOutputTokens": 75.0,
                    },
                },
            }
        ],
        "budgets": {"maxUsdPerSession": 2.0, "onExceed": "warn"},
    }
    spec = validate_spec_data(full_data)  # must not raise
    assert spec.spec.models.budgets is not None
    assert spec.spec.models.budgets.maxUsdPerSession == 2.0


def test_usd_budget_default_provider_unpriced_refused(full_data: dict[str, Any]) -> None:
    """The DEFAULT provider path must be priced too — dropping its pricing
    while a USD budget is set refuses, naming `models`."""
    full_data["spec"]["models"] = {
        "provider": "anthropic",
        "anthropic": {"model": "claude-haiku-4-5"},  # no pricing
        "budgets": {"maxUsdPerSession": 2.0},
    }
    _rejects(full_data, "maxUsdPerSession", "models.pricing")


def test_pricing_allowed_without_usd_budget(full_data: dict[str, Any]) -> None:
    """Pricing is additive/optional: declaring it WITHOUT a USD budget is fine
    (no cross-validation triggers)."""
    full_data["spec"]["models"]["budgets"] = {"maxTokensPerSession": 1000}
    full_data["spec"]["models"]["anthropic"]["pricing"] = {
        "usdPerMillionInputTokens": 3.0,
        "usdPerMillionOutputTokens": 15.0,
    }
    spec = validate_spec_data(full_data)  # must not raise
    assert spec.spec.models.anthropic is not None
    assert spec.spec.models.anthropic.pricing is not None


# ---------------------------------------------------------------- observability et al.


def test_observability_bad_log_level(full_data: dict[str, Any]) -> None:
    full_data["spec"]["observability"]["logLevel"] = "trace"
    _rejects(full_data, "logLevel")


def test_persistence_bad_tier(full_data: dict[str, Any]) -> None:
    full_data["spec"]["persistence"]["tier"] = "mongodb"
    _rejects(full_data, "tier")


def test_unknown_field_in_new_section(full_data: dict[str, Any]) -> None:
    full_data["spec"]["memory"]["ttl"] = 3600
    _rejects(full_data, "ttl")

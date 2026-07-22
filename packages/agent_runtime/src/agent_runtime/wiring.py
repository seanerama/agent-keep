"""Spec -> component selection. Single source of truth for composer AND runner.

Each component is one module under agent_runtime/components/. The composer
copies ONLY the selected modules into the build context (absence semantics —
contract agent-spec, rule 2); the runner imports the same selection at boot.

Stage 2 makes the SCHEMA fully expressive before the component library catches
up, so a spec can validly select things this library version cannot build yet.
`ensure_buildable` turns that into a loud, specific ComponentNotImplementedError
(at `foundry build` and again at boot) instead of a silent partial agent.
"""

import importlib
import re
from dataclasses import dataclass
from types import ModuleType
from urllib.parse import urlsplit

from keep_spec import (
    AgentSpec,
    EventTrigger,
    HttpTransport,
    OllamaProviderConfig,
    ScheduleTrigger,
    SlackChannel,
    WebexChannel,
)

#: WebEx REST API host the outbound-reply path reaches — cross-validated against
#: sandbox.egress (see egress_violations). Keep in sync with
#: components.webex_channel.DEFAULT_BASE_URL.
WEBEX_API_HOST = "webexapis.com"
WEBEX_API_PORT = 443

#: Slack Web API host the outbound-reply path (chat.postMessage) reaches —
#: cross-validated against sandbox.egress (see egress_violations, stage 21).
#: Keep in sync with components.slack_channel.DEFAULT_BASE_URL (same convention
#: as WEBEX_API_HOST above; pinned by a sync test).
SLACK_API_HOST = "slack.com"
SLACK_API_PORT = 443

#: Anthropic REST API host the provider adapter reaches — cross-validated
#: against sandbox.egress (see egress_violations, stage 19 #50). Keep in sync
#: with components.anthropic_provider.DEFAULT_BASE_URL (same convention as
#: WEBEX_API_HOST above; pinned by a sync test).
ANTHROPIC_API_HOST = "api.anthropic.com"
ANTHROPIC_API_PORT = 443


class ComponentNotImplementedError(NotImplementedError):
    """The spec is schema-valid but selects components this library lacks."""


class EgressCrossValidationError(ValueError):
    """A declared outbound-capable component's host is not allowlisted in
    sandbox.egress — an HTTP MCP server (stage 7) or a platform channel's reply
    host (stage 10, WebEx).

    Stage-7 cross-validation hook decision (documented here, per the stage
    spec): enforced from `ensure_buildable`, so it fails `foundry build`
    (the composer calls it before emitting any context) AND agent boot
    (runner.build_app) with the same clear message. `foundry validate`
    stays purely schema-level — this is a spec/spec consistency rule, and
    build is the earliest gate every spec must pass through."""


#: Component id -> module name under agent_runtime.components
COMPONENT_REGISTRY: dict[str, str] = {
    "dev-http-channel": "dev_http",
    "in-process-queue": "memory_queue",
    "redis-queue": "redis_queue",
    "single-session": "single_session",
    "prompt-assembler": "prompt_assembler",
    "static-provider": "static_provider",
    "anthropic-provider": "anthropic_provider",
    "ollama-provider": "ollama_provider",
    "model-router": "model_router",
    "jsonl-audit": "jsonl_audit",
    "local-tools": "local_tools",
    "mcp-manager": "mcp_manager",
    "webex-channel": "webex_channel",
    "slack-channel": "slack_channel",
    "postgres-persistence": "postgres_persistence",
    "sqlite-persistence": "sqlite_persistence",
    "pgvector-memory": "pgvector_memory",
    "facts-memory": "facts_memory",
    "event-intake": "event_intake",
    "retrieval-history": "retrieval_history",
    "schedule-trigger": "schedule_trigger",
}

_CHANNEL_COMPONENTS = {
    "dev-http": "dev-http-channel",
    "webex": "webex-channel",
    "slack": "slack-channel",
}

#: Gateway allowlist policies this library enforces (see agent_runtime.gateway).
#: 'pairing' grows the roster at run time and is NOT implemented — a pairing
#: spec stays guarded rather than silently admitting strangers.
_ALLOWLIST_POLICIES = frozenset({"owner-only", "tiered"})
_QUEUE_COMPONENTS = {"in-process": "in-process-queue", "redis": "redis-queue"}
_SESSION_COMPONENTS = {"single": "single-session"}
#: Session definitions this library implements. 'per-channel' (stage 17) keys
#: conversation state by channel identity; 'per-user' (stage 23) keys it by
#: the sender's per-platform principal — the owner's thread follows the owner
#: across channels (both persistence tiers honor both rules through the one
#: sessions.session_key helper). 'hybrid' (shared memory, separate
#: transcripts) has no implementation and stays guarded.
_BUILDABLE_SESSION_DEFINITIONS = frozenset({"per-channel", "per-user"})
#: History strategies this library implements: 'retrieval' (stage 17 — its
#: index lives on the postgres tier; see the tier guard below) and
#: 'sliding-window' (stage 23 — a prompt-assembly truncation over the session
#: transcript, which EVERY buildable tier durably persists as of stages
#: 15/20, so it deliberately carries NO tier constraint and ships NO extra
#: component). 'summarization'/'layered' have no component and stay guarded.
_BUILDABLE_HISTORY_STRATEGIES = frozenset({"retrieval", "sliding-window"})
_PROVIDER_COMPONENTS = {
    "static": "static-provider",
    "anthropic": "anthropic-provider",
    "ollama": "ollama-provider",
}
_AUDIT_COMPONENTS = {"jsonl": "jsonl-audit"}
#: Persistence tiers this library can build. 'postgres' (stage 15) is the
#: real-database session manager; 'sqlite' (stage 20) is the real FILE-backed
#: one — every buildable tier now actually persists (stage 20 closed the
#: silently-in-process gap, issue #59). 'files' stays guarded.
_BUILDABLE_PERSISTENCE_TIERS = frozenset({"sqlite", "postgres"})
#: Memory corpora with a real writer behind the pgvector write boundary
#: (stage 16). Keep in sync with components.pgvector_memory.CORPUS_ADMITS.
_BUILDABLE_MEMORY_CORPORA = frozenset({"agent-summaries"})
#: writePolicy values the pgvector (vectors) component enforces:
#: 'agent-autonomous' (privileged, every write audited) and 'off' (read-only —
#: trivially enforceable). 'user-command' has no command path on the VECTOR
#: store and stays guarded there. Keep in sync with
#: components.pgvector_memory.ENFORCEABLE_WRITE_POLICIES.
_BUILDABLE_MEMORY_WRITE_POLICIES = frozenset({"agent-autonomous", "off"})
#: The ONLY writePolicy the facts structure builds under (stage 24): the
#: runtime command surface IS the write path, so 'user-command' is buildable
#: and 'agent-autonomous'/'off' (no facts writer) stay guarded on the facts
#: branch. Keep in sync with components.facts_memory.
_FACTS_WRITE_POLICY = "user-command"
#: Trigger kinds this library can build: 'message' (the skeleton's implicit
#: activation), 'event-subscription' (stage 18, components/event_intake), and
#: 'schedule' (stage 22, components/schedule_trigger — real cron validation,
#: #10, landed with it in keep_spec).
_BUILDABLE_TRIGGER_KINDS = frozenset({"message", "event-subscription", "schedule"})


def _local_tool_names() -> frozenset[str]:
    """Tool names the local_tools registry offers.

    Imported lazily: only tool-granting specs reach this, and exactly those
    specs ship the local_tools component — a tool-less image never imports it
    (absence semantics).
    """
    from agent_runtime.components import local_tools

    return frozenset(local_tools.REGISTRY)


def event_activations(spec: AgentSpec) -> list[EventTrigger]:
    """The spec's event-subscription activations, in declaration order.

    Single source of truth for composer AND runner: a non-empty list selects
    the event-intake component; an empty one means NO event trigger was
    declared and the component is absent from the image — the kill-switch is
    spec opt-in (absence semantics, contract agent-spec rule 2), not a flag.
    """
    if spec.spec.triggers is None:
        return []
    return [a for a in spec.spec.triggers.activations if isinstance(a, EventTrigger)]


def schedule_activations(spec: AgentSpec) -> list[ScheduleTrigger]:
    """The spec's schedule activations, in declaration order (stage 22).

    Single source of truth for composer AND runner, exactly like
    `event_activations` above: a non-empty list selects the schedule-trigger
    component; an empty one means NO schedule was declared and the component
    is absent from the image — the kill-switch is spec opt-in (absence
    semantics, contract agent-spec rule 2), not a flag. The declaration-order
    index is also the component's trigger-principal suffix
    (`trigger:schedule:<index>` — components/schedule_trigger)."""
    if spec.spec.triggers is None:
        return []
    return [a for a in spec.spec.triggers.activations if isinstance(a, ScheduleTrigger)]


def tool_execution_required(spec: AgentSpec) -> bool:
    """True when the spec grants tools — the image must ship the tool executor.

    A spec without tools ships NO executor module and NO approval endpoints
    (absence, not disablement).
    """
    return bool(spec.spec.tools)


def unimplemented_selections(spec: AgentSpec) -> list[str]:
    """Every selection in the spec that has no component in this library yet.

    Empty list = the spec is buildable by this component-library version.
    """
    missing: list[str] = []
    sections = spec.spec

    for index, channel in enumerate(sections.channels):
        if channel.type not in _CHANNEL_COMPONENTS:
            missing.append(f"channel adapter '{channel.type}' (spec.channels[{index}])")
        elif isinstance(channel, WebexChannel) and channel.verification.method != "signature":
            # The WebEx adapter implements webhook-signature verification only;
            # token/none postures are not enforced, so they stay guarded.
            missing.append(
                f"webex verification method '{channel.verification.method}' "
                f"(spec.channels[{index}].verification.method)"
            )
        elif isinstance(channel, SlackChannel):
            # Stage 21 flips exactly webhook transport + signature verification
            # (the Events API posture the adapter implements). Socket Mode
            # ('websocket') and 'polling' have no component; token/none
            # verification is not enforced — all stay loudly guarded.
            if channel.transport != "webhook":
                missing.append(
                    f"slack transport '{channel.transport}' (spec.channels[{index}].transport)"
                )
            elif channel.verification.method != "signature":
                missing.append(
                    f"slack verification method '{channel.verification.method}' "
                    f"(spec.channels[{index}].verification.method)"
                )
    if sections.triggers is not None:
        for activation in sections.triggers.activations:
            # Every v1 trigger kind now has a component: 'message' (the
            # skeleton's implicit activation), 'event-subscription' (stage 18,
            # components/event_intake), 'schedule' (stage 22,
            # components/schedule_trigger). The guard stays for future
            # additive kinds.
            if activation.kind not in _BUILDABLE_TRIGGER_KINDS:
                missing.append(f"trigger '{activation.kind}' (spec.triggers)")
    if sections.gateway.queue not in _QUEUE_COMPONENTS:
        missing.append(f"queue '{sections.gateway.queue}' (spec.gateway.queue)")
    if sections.gateway.concurrency != "serial":
        missing.append(f"concurrency '{sections.gateway.concurrency}' (spec.gateway.concurrency)")
    if sections.gateway.allowlist is not None:
        # Stage 10: the generic gateway (agent_runtime.gateway) enforces the
        # owner-only and tiered policies — verified sender identity mapped to
        # the roster, unknown senders dropped + audited. 'pairing' (runtime
        # roster growth) has no enforcer yet and stays guarded.
        policy = sections.gateway.allowlist.policy
        if policy not in _ALLOWLIST_POLICIES:
            missing.append(f"gateway allowlist policy '{policy}' (spec.gateway.allowlist.policy)")
    if sections.gateway.identityUnification != "separate":
        missing.append(
            f"identity unification '{sections.gateway.identityUnification}' "
            "(spec.gateway.identityUnification)"
        )
    if sections.sessions.mode not in _SESSION_COMPONENTS:
        missing.append(f"session manager '{sections.sessions.mode}' (spec.sessions.mode)")
    if (
        sections.sessions.definition is not None
        and sections.sessions.definition not in _BUILDABLE_SESSION_DEFINITIONS
    ):
        # Stage 17 flipped 'per-channel' (the outage spec's selection); stage
        # 23 flips 'per-user' (the client-tracking spec's: the owner's thread
        # follows the owner). 'hybrid' has no implementation and stays guarded.
        missing.append(
            f"session definition '{sections.sessions.definition}' (spec.sessions.definition)"
        )
    if sections.sessions.history is not None:
        strategy = sections.sessions.history.strategy
        if strategy not in _BUILDABLE_HISTORY_STRATEGIES:
            # summarization/layered have no component and stay guarded.
            missing.append(f"history strategy '{strategy}' (spec.sessions.history)")
        elif strategy == "retrieval" and sections.persistence.tier != "postgres":
            # Stage 17: retrieval retrieves over an INDEX that lives on the
            # postgres tier. sliding-window (stage 23) reads the session
            # transcript itself — durable on BOTH buildable tiers (stages
            # 15/20) — so it deliberately has no tier constraint.
            missing.append(
                f"history strategy 'retrieval' on persistence tier "
                f"'{sections.persistence.tier}' — the retrieval index lives on the postgres "
                "tier (spec.sessions.history with spec.persistence.tier)"
            )
    if sections.persona.source != "static":
        missing.append(f"persona source '{sections.persona.source}' (spec.persona.source)")
    if sections.memory is not None:
        # Two structures are buildable, each on its own branch so a writePolicy
        # is validated against the structure that actually enforces it:
        #   - 'facts' (stage 24): structured records on the persistence tier,
        #     written ONLY by the runtime command surface — so exactly
        #     writePolicy 'user-command' flips; 'agent-autonomous'/'off' have no
        #     facts writer and stay guarded ON THIS branch (the command path IS
        #     the write path). No corpus/store: FactsMemory.store is 'none'.
        #   - 'vectors' on store 'pgvector' over 'agent-summaries' (stage 16)
        #     with an enforceable writePolicy ('agent-autonomous'/'off');
        #     'user-command' has no command path on the vector store and stays
        #     guarded there.
        # 'layered' has no component; 'sqlite-vec' has no store; other corpora
        # have no write boundary. Everything guarded fails loudly, as before.
        structure = sections.memory.structure
        write_policy = sections.memory.writePolicy
        if structure.kind == "facts":
            if write_policy != _FACTS_WRITE_POLICY:
                missing.append(
                    f"memory writePolicy '{write_policy}' for facts (spec.memory.writePolicy)"
                )
        elif structure.kind != "vectors":
            missing.append(f"memory structure '{structure.kind}' (spec.memory)")
        elif structure.store != "pgvector":
            missing.append(
                f"memory structure 'vectors' store '{structure.store}' "
                "(spec.memory.structure.store)"
            )
        else:
            if structure.corpus not in _BUILDABLE_MEMORY_CORPORA:
                declared = structure.corpus or "transcripts (the schema default for absent corpus)"
                missing.append(f"memory corpus '{declared}' (spec.memory.structure.corpus)")
            if write_policy not in _BUILDABLE_MEMORY_WRITE_POLICIES:
                missing.append(f"memory writePolicy '{write_policy}' (spec.memory.writePolicy)")
    if sections.skills:
        names = ", ".join(pack.name for pack in sections.skills)
        missing.append(f"skill registry (spec.skills: {names})")
    # Stage 7: the MCP client manager EXISTS — stdio and streamable-HTTP MCP
    # grants bind through the mcp_manager component into the same stage-6
    # executor seam (constraints, autoApprove, and the approval gate are all
    # enforced there; read-only scopes at the manager boundary). Grants are
    # validated against the live server's tools/list at BOOT, not here — the
    # servers are not reachable at compose time. HTTP servers additionally
    # cross-validate against sandbox.egress (see mcp_egress_violations).
    local_servers = [server for server in sections.tools if server.transport.kind == "local"]
    if local_servers:
        known = _local_tool_names()
        for server in local_servers:
            for grant in server.allow:
                if grant.name not in known:
                    missing.append(f"local tool '{grant.name}' (spec.tools['{server.name}'].allow)")
    # Approval enforcement (stage 6) covers exactly the schema default policy,
    # 'allowlist-confirm-rest' (default-deny with an autoApprove allowlist).
    # Any other policy is still an unenforced declaration and must fail.
    if sections.approval.policy != "allowlist-confirm-rest":
        missing.append(
            f"approval enforcement for policy '{sections.approval.policy}' (spec.approval.policy)"
        )
    if sections.sandbox.profile != "container":
        missing.append(f"sandbox profile '{sections.sandbox.profile}' (spec.sandbox.profile)")
    # Stage 10 gives sandbox.egress its first ACTIVE build-time consumer: the
    # cross-validation below (egress_violations) checks that every declared
    # outbound-capable component — HTTP MCP servers AND platform channels'
    # reply hosts — is covered by the allowlist, and fails the build otherwise.
    # The allowlist is therefore no longer a decorative declaration (it gates
    # buildability), so the blanket "egress enforcement not implemented" guard
    # is retired. Runtime network-level enforcement (a container network policy)
    # remains a DEPLOY concern — documented alongside the tunnel note in
    # docs/security-review-walkthrough.md, the same status as the cloudflared
    # route — not solved in-code this stage.
    if sections.models.provider not in _PROVIDER_COMPONENTS:
        missing.append(f"model provider '{sections.models.provider}' (spec.models.provider)")
    # Stage 9 flips the tier/budget guards: both providers plus the router
    # component exist, so tiers and token budgets are buildable. Stage 25 flips
    # the USD-budget guard: USD budgets enforce against OPERATOR-declared
    # pricing (models.*.pricing) — no library price table (stale tables
    # silently mis-enforce). The spec cross-validation (models.py:
    # _usd_budget_requires_pricing) already refuses a maxUsdPerSession over any
    # unpriced/static path at LOAD, so an admitted spec is enforceable by
    # construction — the guard is gone.
    for tier in sections.models.tiers:
        if tier.provider not in _PROVIDER_COMPONENTS:
            missing.append(f"model provider '{tier.provider}' (spec.models.tiers['{tier.name}'])")
    if sections.observability.audit.sink not in _AUDIT_COMPONENTS:
        missing.append(
            f"audit sink '{sections.observability.audit.sink}' (spec.observability.audit.sink)"
        )
    health = sections.observability.health
    if health is not None and health.path != "/healthz":
        # dev_http serves exactly /healthz; a custom path would build but 404.
        missing.append(
            f"health surface at custom path '{health.path}' (spec.observability.health.path)"
        )
    # Stage 15 flips the postgres guard: `persistence.tier: postgres` selects
    # the real-database session manager (components/postgres_persistence).
    # 'files' still has no component and stays guarded.
    if sections.persistence.tier not in _BUILDABLE_PERSISTENCE_TIERS:
        missing.append(f"persistence tier '{sections.persistence.tier}' (spec.persistence.tier)")
    return missing


def _egress_entry_allows(entry: str, host: str, port: int) -> bool:
    """Does one sandbox.egress `host[:port]` entry (optionally `*.`-wildcarded)
    cover this host+port? Entries are already format-validated by the schema."""
    entry_host, entry_port = entry, None
    if re.search(r":[0-9]{1,5}$", entry):
        entry_host, _, port_text = entry.rpartition(":")
        entry_port = int(port_text)
    if entry_port is not None and entry_port != port:
        return False
    entry_host = entry_host.lower()
    if entry_host.startswith("*."):
        return host.endswith(entry_host[1:])  # subdomains only, per wildcard convention
    return host == entry_host


@dataclass(frozen=True)
class _EgressTarget:
    """One host:port a declared outbound-capable component must be able to reach,
    plus the violation message to emit if sandbox.egress does not cover it."""

    host: str
    port: int
    violation: str


def _mcp_egress_targets(spec: AgentSpec) -> list[_EgressTarget]:
    """Every HTTP MCP server's host:port (stdio servers are local — no egress)."""
    targets: list[_EgressTarget] = []
    for server in spec.spec.tools:
        transport = server.transport
        if not isinstance(transport, HttpTransport):
            continue
        parts = urlsplit(transport.url)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        targets.append(
            _EgressTarget(
                host=host,
                port=port,
                violation=(
                    f"HTTP MCP server '{server.name}' host '{host}:{port}' is not covered by "
                    f"sandbox.egress — add a matching host[:port] entry or the server is "
                    f"unreachable by construction (spec.tools['{server.name}'].transport.url)"
                ),
            )
        )
    return targets


def _channel_egress_targets(spec: AgentSpec) -> list[_EgressTarget]:
    """Every platform channel's reply host:port. dev-http is local (no egress);
    a WebEx channel replies via the WebEx REST API and a Slack channel via the
    Slack Web API (chat.postMessage) — both must be covered."""
    targets: list[_EgressTarget] = []
    for index, channel in enumerate(spec.spec.channels):
        if isinstance(channel, WebexChannel):
            targets.append(
                _EgressTarget(
                    host=WEBEX_API_HOST,
                    port=WEBEX_API_PORT,
                    violation=(
                        f"WebEx channel reply host '{WEBEX_API_HOST}:{WEBEX_API_PORT}' is not "
                        f"covered by sandbox.egress — add '{WEBEX_API_HOST}:{WEBEX_API_PORT}' or "
                        f"the WebEx API is unreachable by construction (spec.channels[{index}])"
                    ),
                )
            )
        elif isinstance(channel, SlackChannel):
            targets.append(
                _EgressTarget(
                    host=SLACK_API_HOST,
                    port=SLACK_API_PORT,
                    violation=(
                        f"Slack channel reply host '{SLACK_API_HOST}:{SLACK_API_PORT}' is not "
                        f"covered by sandbox.egress — add '{SLACK_API_HOST}:{SLACK_API_PORT}' or "
                        f"the Slack Web API is unreachable by construction (spec.channels[{index}])"
                    ),
                )
            )
    return targets


def _provider_egress_targets(spec: AgentSpec) -> list[_EgressTarget]:
    """The model provider's API host, when the spec selects a remote provider.

    The anthropic provider is a spec-SELECTED component with a constant, known
    host (components.anthropic_provider.DEFAULT_BASE_URL) — exactly the WebEx
    reply-host pattern, so a spec selecting it (top-level or in any tier) must
    allowlist it. The static provider is in-process: no egress. One target no
    matter how many selection sites; the violation names them all."""
    models = spec.spec.models
    selections = [("spec.models.provider", models.provider)] + [
        (f"spec.models.tiers['{tier.name}']", tier.provider) for tier in models.tiers
    ]
    sites = [site for site, provider in selections if provider == "anthropic"]
    if not sites:
        return []
    return [
        _EgressTarget(
            host=ANTHROPIC_API_HOST,
            port=ANTHROPIC_API_PORT,
            violation=(
                f"anthropic provider API host '{ANTHROPIC_API_HOST}:{ANTHROPIC_API_PORT}' is "
                f"not covered by sandbox.egress — add "
                f"'{ANTHROPIC_API_HOST}:{ANTHROPIC_API_PORT}' or the Anthropic API is "
                f"unreachable by construction ({'; '.join(sites)})"
            ),
        )
    ]


#: Default Ollama port when a baseHost omits `:port` (matches
#: components.ollama_provider.DEFAULT_BASE_URL).
OLLAMA_DEFAULT_PORT = 11434


def _ollama_egress_targets(spec: AgentSpec) -> list[_EgressTarget]:
    """The Ollama server host:port for every spec site selecting `ollama`.

    Unlike the anthropic provider (a constant, module-known host), the ollama
    endpoint is spec-declared: the host comes from the SELECTED ollama config's
    ``baseHost`` (top-level or per-tier — ADR 0006), so a spec selecting ollama
    must allowlist exactly that host:port in sandbox.egress. One target per
    selecting site (each may carry a different baseHost)."""
    models = spec.spec.models
    sites: list[tuple[str, OllamaProviderConfig | None]] = [
        ("spec.models.provider", models.ollama if models.provider == "ollama" else None)
    ]
    sites += [
        (f"spec.models.tiers['{tier.name}']", tier.ollama)
        for tier in models.tiers
        if tier.provider == "ollama"
    ]
    targets: list[_EgressTarget] = []
    for site, config in sites:
        if config is None:
            # Schema cross-validation guarantees a config on a selecting site;
            # nothing to allowlist without one.
            continue
        base_host, _, port_text = config.baseHost.rpartition(":")
        if base_host:
            host, port = base_host.lower(), int(port_text)
        else:
            host, port = config.baseHost.lower(), OLLAMA_DEFAULT_PORT
        targets.append(
            _EgressTarget(
                host=host,
                port=port,
                violation=(
                    f"ollama provider host '{host}:{port}' is not covered by "
                    f"sandbox.egress — add '{config.baseHost}' or the Ollama server is "
                    f"unreachable by construction ({site}.ollama.baseHost)"
                ),
            )
        )
    return targets


def _violations_for(targets: list[_EgressTarget], egress: list[str]) -> list[str]:
    return [
        target.violation
        for target in targets
        if not any(_egress_entry_allows(entry, target.host, target.port) for entry in egress)
    ]


def mcp_egress_violations(spec: AgentSpec) -> list[str]:
    """Stage-7 cross-validation: every HTTP MCP server's host must be covered by
    `sandbox.egress`. Kept as a stable, MCP-only view (behavior identical to
    before stage 10's generalization); `egress_violations` is the whole check."""
    return _violations_for(_mcp_egress_targets(spec), spec.spec.sandbox.egress)


def egress_violations(spec: AgentSpec) -> list[str]:
    """Every spec-SELECTED outbound-capable component with a spec-visible host
    must be covered by `sandbox.egress` — HTTP MCP servers (stage 7), platform
    channels' reply hosts (stage 10, WebEx), and the anthropic provider's API
    host (stage 19, #50). The allowlist is an exhaustive positive declaration,
    so a spec declaring a component its own egress rules would block is
    internally inconsistent and must not build. Deliberately NOT covered:
    redis/postgres backing-store hosts — those are composition-level plumbing
    whose addresses arrive as deploy-config env values (REDIS_URL /
    POSTGRES_DSN), invisible to the spec, so there is nothing spec-declared to
    cross-validate (the stage-15 decision recorded in
    components/postgres_persistence.py; sandbox.egress names the
    model-reachable perimeter the deploy enforces — see
    docs/security-review-walkthrough.md). Empty list = consistent.
    """
    targets = (
        _mcp_egress_targets(spec)
        + _channel_egress_targets(spec)
        + _provider_egress_targets(spec)
        + _ollama_egress_targets(spec)
    )
    return _violations_for(targets, spec.spec.sandbox.egress)


def ensure_buildable(spec: AgentSpec) -> None:
    """Raise if the spec selects missing components or is egress-inconsistent.

    Called by BOTH `foundry build` (composer, before emitting a context) and
    agent boot (runner.build_app) — the same guard, the same message."""
    missing = unimplemented_selections(spec)
    if missing:
        raise ComponentNotImplementedError(
            "component not implemented: "
            + "; ".join(missing)
            + ". The spec is valid keep/v1, but this component-library version "
            "cannot build it yet."
        )
    egress_problems = egress_violations(spec)
    if egress_problems:
        raise EgressCrossValidationError(
            "spec cross-validation failed: " + "; ".join(egress_problems)
        )


def select_components(spec: AgentSpec) -> list[str]:
    """Return the sorted component ids the spec positively declares.

    The prompt assembler is part of every composed agent's message path, so it
    is always selected; everything else follows the spec's declarations.
    Raises ComponentNotImplementedError for selections the library lacks.
    """
    ensure_buildable(spec)
    selected = {"prompt-assembler"}
    for channel in spec.spec.channels:
        selected.add(_CHANNEL_COMPONENTS[channel.type])
    selected.add(_QUEUE_COMPONENTS[spec.spec.gateway.queue])
    # The persistence tier's manager serves the session mode (single — the
    # only implemented mode, guarded above). ensure_buildable admits exactly
    # {sqlite, postgres}, so after stage 20 EVERY buildable spec gets a
    # durable session manager: the in-memory single_session module is no
    # longer part of ANY composed image (absence semantics cut both ways,
    # like the redis flip — it remains in the library only as the non-durable
    # building block unit tests wire directly).
    if spec.spec.persistence.tier == "postgres":
        # Stage 15: real-database sessions/transcripts.
        selected.add("postgres-persistence")
    else:
        # Stage 20: real file-backed sessions/transcripts — 'sqlite' is the
        # only other admitted tier (the guard on _SESSION_COMPONENTS above
        # still validates sessions.mode itself).
        selected.add("sqlite-persistence")
    history = spec.spec.sessions.history
    if history is not None and history.strategy == "retrieval":
        # Stage 17: the retrieval strategy ships its index component (admitted
        # by ensure_buildable only on the postgres tier). The sliding-window
        # strategy (stage 23) ships NOTHING extra: the window is a truncation
        # inside the always-selected prompt assembler. `history:` absent means
        # neither is in the image (absence semantics ARE the kill-switch:
        # spec-opt-in, full-transcript replay by default).
        selected.add("retrieval-history")
    if spec.spec.memory is not None:
        # ensure_buildable above admits exactly the buildable memory selections;
        # `memory:` absent means NO memory module in the image (absence
        # semantics ARE the kill-switch: spec-opt-in, dark by default). The
        # facts structure (stage 24) ships facts_memory; the vectors structure
        # (stage 16) ships pgvector_memory — one memory structure, one module.
        if spec.spec.memory.structure.kind == "facts":
            selected.add("facts-memory")
        else:
            selected.add("pgvector-memory")
    selected.add(_PROVIDER_COMPONENTS[spec.spec.models.provider])
    for tier in spec.spec.models.tiers:
        selected.add(_PROVIDER_COMPONENTS[tier.provider])
    if spec.spec.models.tiers or spec.spec.models.budgets is not None:
        # The router ships ONLY when the spec declares tiers or budgets; a
        # single-provider, unbudgeted agent has no router module at all.
        selected.add("model-router")
    selected.add(_AUDIT_COMPONENTS[spec.spec.observability.audit.sink])
    if event_activations(spec):
        # Stage 18: the event-intake receiver ships ONLY when the spec declares
        # an event-subscription activation; message-only agents have no event
        # endpoint in the image at all (absence, not disablement).
        selected.add("event-intake")
    if schedule_activations(spec):
        # Stage 22: the schedule clock loop ships ONLY when the spec declares
        # a schedule activation; schedule-less agents have no scheduler module
        # in the image at all (absence, not disablement).
        selected.add("schedule-trigger")
    if any(server.transport.kind == "local" for server in spec.spec.tools):
        selected.add("local-tools")
    if any(server.transport.kind in ("stdio", "http") for server in spec.spec.tools):
        selected.add("mcp-manager")
    return sorted(selected)


def component_module_names(spec: AgentSpec) -> list[str]:
    """Module names (under agent_runtime.components) for the spec's selection."""
    return sorted(COMPONENT_REGISTRY[cid] for cid in select_components(spec))


def load_component(component_id: str) -> ModuleType:
    """Import a selected component module. Fails loudly if it is absent from the image."""
    return importlib.import_module(f"agent_runtime.components.{COMPONENT_REGISTRY[component_id]}")

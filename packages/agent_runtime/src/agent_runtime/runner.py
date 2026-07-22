"""Container entrypoint: validate the baked-in spec, wire selected components, serve.

Components are resolved through agent_runtime.wiring — the same selection the
composer used to build the image — so a wiring/composition mismatch fails loudly
at boot (the module would be absent) instead of silently degrading.
"""

import asyncio
import logging
import os
import re
import signal
import sys
from typing import Any

from agent_runtime.audit import AgentIdentity
from agent_runtime.core import (
    AgentCore,
    CommandSurfaceProtocol,
    FactsReadProtocol,
    HistoryStrategyProtocol,
    MemoryRecallProtocol,
    ModelRouterProtocol,
)
from agent_runtime.provider import ModelProvider
from agent_runtime.queues import Gate
from agent_runtime.wiring import (
    ComponentNotImplementedError,
    ensure_buildable,
    event_activations,
    load_component,
    schedule_activations,
)
from keep_spec import AgentSpec, DevHttpChannel, SlackChannel, WebexChannel, load_spec
from keep_spec.models import (
    AnthropicProviderConfig,
    FactsMemory,
    RetrievalHistory,
    SlidingWindowHistory,
    StaticProviderConfig,
    VectorMemory,
)

#: The one shape agent.image_digest may take (contract audit-record:
#: "<sha256 of the running image>"). Anything else — including a sentinel
#: like 'sha256:unknown' — must never enter the append-only log.
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ImageDigestError(RuntimeError):
    """AGENT_IMAGE_DIGEST unset or malformed — the runner refuses to start."""


def require_image_digest() -> str:
    """The running image's digest, from AGENT_IMAGE_DIGEST — or refuse to boot.

    Every audit record carries agent.image_digest as provenance; a fabricated
    sentinel would poison the append-only log for every record this agent ever
    writes. The digest names the image itself, so it is only knowable at run
    time: the deploy path injects it (deploy.sh) and so does the integration
    fixture. No digest, no boot — never a masquerade.
    """
    digest = os.environ.get("AGENT_IMAGE_DIGEST", "")
    if not _IMAGE_DIGEST_RE.fullmatch(digest):
        detail = "unset" if not digest else f"malformed: {digest!r}"
        raise ImageDigestError(
            f"refusing to start: AGENT_IMAGE_DIGEST is {detail} — expected "
            "'sha256:<64 hex>' of the running image, injected at run time "
            "(e.g. docker run -e AGENT_IMAGE_DIGEST=...; deploy.sh writes it)"
        )
    return digest


#: TOOLS_ENABLED values that switch the tool layer OFF at runtime.
_FALSEY = frozenset({"", "0", "false", "no", "off"})


def tools_enabled(spec: AgentSpec) -> bool:
    """The TOOLS_ENABLED kill-switch: default ON only when the spec grants tools.

    OFF removes the tool layer entirely at runtime — no executor, no model-
    visible tool list, no approval endpoints (absence semantics at runtime).
    A tool-less spec has nothing to enable; the flag cannot conjure tools the
    spec never granted (their modules are not even in the image).
    """
    if not spec.spec.tools:
        return False
    raw = os.environ.get("TOOLS_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def _build_provider(
    provider: str,
    static: "StaticProviderConfig | None",
    anthropic: "AnthropicProviderConfig | None",
) -> ModelProvider:
    """One provider instance from a (provider, config) pair — default or tier.

    The anthropic constructor reads the API key from the env var the SPEC
    names (apiKeyEnv); a missing key is a loud boot failure naming that var —
    never a lazily-failing half-booted agent.

    `maxTokens` (v1 additive amendment, stage 13) is forwarded only when the
    spec sets it — an absent field keeps the adapter's own default (4096), so
    every pre-amendment spec behaves exactly as before.
    """
    if provider == "static":
        assert static is not None  # ensure_buildable + schema admit no other shape
        built: ModelProvider = load_component("static-provider").StaticProvider(static.script)
        return built
    assert anthropic is not None  # provider == "anthropic" (schema-validated)
    kwargs: dict[str, Any] = {"model": anthropic.model, "api_key_env": anthropic.apiKeyEnv}
    if anthropic.maxTokens is not None:
        kwargs["max_tokens"] = anthropic.maxTokens
    built = load_component("anthropic-provider").AnthropicProvider(**kwargs)
    return built


class ChannelWithEventIntake:
    """The served surface of a trigger-bearing agent: the channel adapter plus
    the event-intake receiver behind one serve()/aclose() facade, so run()
    (and every caller of build_app) keeps its single-adapter shape. Built ONLY
    when the spec declares an event-subscription activation — a message-only
    agent gets the bare channel adapter, exactly as before (absence semantics).
    """

    def __init__(self, channel: Any, intake: Any) -> None:
        self.channel = channel
        self.intake = intake

    async def serve(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.channel.serve())
            tg.create_task(self.intake.serve())

    async def aclose(self) -> None:
        for served in (self.channel, self.intake):
            aclose = getattr(served, "aclose", None)
            if aclose is not None:
                await aclose()


class ChannelWithScheduleTrigger:
    """The served surface of a schedule-bearing agent: whatever surface was
    built so far (the bare channel adapter, or channel + event intake) plus
    the schedule clock loop behind the same serve()/aclose() facade — the
    stage-18 pattern. Built ONLY when the spec declares a schedule activation;
    a schedule-less agent's surface is exactly as before (absence semantics).
    """

    def __init__(self, channel: Any, scheduler: Any) -> None:
        self.channel = channel
        self.scheduler = scheduler

    async def serve(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.channel.serve())
            tg.create_task(self.scheduler.serve())

    async def aclose(self) -> None:
        # The scheduler owns no connections/clients; only the wrapped surface
        # may need closing (e.g. webex's httpx client, or the intake facade).
        aclose = getattr(self.channel, "aclose", None)
        if aclose is not None:
            await aclose()


def build_app(spec: AgentSpec) -> tuple[AgentCore, Any]:
    """Wire the agent core and the channel adapter from the spec's selections."""
    # Same guard the composer applies at build time: a spec selecting components
    # this library lacks fails loudly at boot, never a silent partial agent.
    ensure_buildable(spec)
    # Fail-loud provenance guard (issue #8): resolve the digest before wiring
    # anything, so a misconfigured launch cannot produce a single audit record.
    image_digest = require_image_digest()
    if spec.spec.gateway.queue == "in-process":
        queue = load_component("in-process-queue").InProcessQueue()
    else:
        queue = load_component("redis-queue").RedisQueue(os.environ.get("REDIS_URL", ""))

    # Stage 17: `sessions.definition` rides into whichever tier serves the
    # session seam (absent = the one 'single' session, exactly as before —
    # both managers treat None as the pre-stage-17 behavior).
    definition = spec.spec.sessions.definition
    if spec.spec.persistence.tier == "postgres":
        # Stage 15: real-database sessions/transcripts. The DSN arrives at
        # deploy time as POSTGRES_DSN (a NAME, like REDIS_URL — never a value
        # in spec or image); missing or unreachable refuses to boot, no silent
        # fallback to the in-memory tier.
        sessions = load_component("postgres-persistence").PostgresSessionManager(
            os.environ.get("POSTGRES_DSN", ""), definition=definition
        )
    else:
        # Stage 20: the sqlite tier is file-backed for REAL (the only other
        # tier ensure_buildable admits). The path arrives at deploy time as
        # SQLITE_PATH (a NAME, like POSTGRES_DSN/REDIS_URL — never a value in
        # spec or image), read unconditionally here; missing refuses to boot
        # naming the variable. NO in-memory or default-path fallback — the
        # silent in-process fallback was the durability lie this stage removes.
        sessions = load_component("sqlite-persistence").SqliteSessionManager(
            os.environ.get("SQLITE_PATH", ""), definition=definition
        )

    history: HistoryStrategyProtocol | None = None
    window_turns: int | None = None
    history_selection = spec.spec.sessions.history
    if isinstance(history_selection, SlidingWindowHistory):
        # Stage 23: sliding-window is NOT a history-strategy component — that
        # seam renders a fenced system-context section and collapses the
        # conversation, while sliding-window's contract is the last-N turns
        # VERBATIM. The always-selected prompt assembler owns the window
        # instead: it truncates its full-transcript rendering to the last
        # maxTurns turns; nothing else changes, and the stored transcript is
        # never trimmed. No tier constraint: the window reads the session
        # transcript, which both buildable tiers durably persist.
        window_turns = history_selection.maxTurns
    elif history_selection is not None:
        # Stage 17: the retrieval-history index — same fixed POSTGRES_DSN env
        # name as the persistence tier (the index lives on the same postgres
        # deployment). Missing/unreachable/extension-less refuses to boot —
        # no silent fall-back to full-transcript replay. ensure_buildable
        # admitted exactly retrieval (postgres tier only) or sliding-window;
        # assert the shape so a future guard regression fails loudly here.
        assert isinstance(history_selection, RetrievalHistory)
        history = load_component("retrieval-history").RetrievalHistoryIndex(
            os.environ.get("POSTGRES_DSN", ""),
            top_k=history_selection.topK,
        )
    assembler = load_component("prompt-assembler").PromptAssembler(window_turns=window_turns)
    models = spec.spec.models
    provider = _build_provider(models.provider, models.static, models.anthropic)
    router: ModelRouterProtocol | None = None
    if models.tiers or models.budgets is not None:
        tier_providers = {
            tier.name: _build_provider(tier.provider, tier.static, tier.anthropic)
            for tier in models.tiers
        }
        budgets = models.budgets
        router_module = load_component("model-router")

        def _price(config: AnthropicProviderConfig | None) -> Any:
            # Operator-declared pricing -> the router's rate object (stage 25).
            # Cross-validation guarantees pricing is present on every path when
            # a USD budget is set, so unpriced here means no USD budget.
            if config is None or config.pricing is None:
                return None
            return router_module.ModelPrice(
                usd_per_million_input=config.pricing.usdPerMillionInputTokens,
                usd_per_million_output=config.pricing.usdPerMillionOutputTokens,
            )

        router = router_module.ModelRouter(
            default=provider,
            tiers=tier_providers,
            max_tokens_per_session=budgets.maxTokensPerSession if budgets else None,
            max_usd_per_session=budgets.maxUsdPerSession if budgets else None,
            on_exceed=budgets.onExceed if budgets else "block",
            default_price=_price(models.anthropic),
            tier_prices={tier.name: _price(tier.anthropic) for tier in models.tiers},
        )
    audit_sink = load_component("jsonl-audit").JsonlAuditSink(spec.spec.observability.audit.path)

    identity = AgentIdentity(
        slug=spec.metadata.slug,
        spec_version=spec.metadata.specVersion,
        image_digest=image_digest,
    )

    memory: MemoryRecallProtocol | None = None
    commands: CommandSurfaceProtocol | None = None
    facts: FactsReadProtocol | None = None
    if spec.spec.memory is not None:
        structure = spec.spec.memory.structure
        if isinstance(structure, FactsMemory):
            # Stage 24: the facts store lives on the ACTIVE persistence tier —
            # it reuses the tier's own connection (built above) through the
            # narrow FactsBackend seam, so it works on sqlite AND postgres with
            # no DSN/path of its own. The SAME object is the model-free command
            # surface (writes) and the read seam (facts into the prompt).
            facts_store = load_component("facts-memory").FactsMemoryStore(
                sessions.facts_backend(),
                audit_sink=audit_sink,
                identity=identity,
            )
            commands = facts_store
            facts = facts_store
        else:
            # Stage 16: the pgvector vector-memory component — same fixed
            # POSTGRES_DSN env name as the persistence tier (the store lives on
            # the same postgres deployment; the value is deploy-config, never
            # in spec or image). Missing/unreachable/extension-less refuses to
            # boot — no silent memory-less agent. ensure_buildable admitted
            # exactly the vectors+pgvector+agent-summaries selection; assert the
            # shape so a future guard regression fails loudly here.
            assert isinstance(structure, VectorMemory) and structure.corpus is not None
            memory = load_component("pgvector-memory").PgvectorMemoryStore(
                os.environ.get("POSTGRES_DSN", ""),
                audit_sink=audit_sink,
                identity=identity,
                corpus=structure.corpus,
                write_policy=spec.spec.memory.writePolicy,
            )

    # Generic gateway allowlist enforcement — wired ONLY when the spec declares
    # an identity layer. The gateway module is absent from the image otherwise
    # (composer copies it exactly on this condition), so import it lazily here.
    gate: Gate | None = None
    if spec.spec.gateway.allowlist is not None:
        from agent_runtime.gateway import AllowlistGate

        gate = AllowlistGate(spec.spec.gateway.allowlist, audit_sink=audit_sink, identity=identity)

    executor = None
    extra_routes = None
    if tools_enabled(spec):
        # Imported only on this path: a tool-less image does not CONTAIN the
        # executor module (absence semantics), and the kill-switch must leave
        # tool-granting agents runnable with the tool layer fully removed.
        from agent_runtime.executor import ApprovalHttpRoutes, build_executor

        executor = build_executor(spec, identity=identity, audit_sink=audit_sink)
        extra_routes = ApprovalHttpRoutes(executor, secret=os.environ.get("APPROVAL_SECRET"))

    core = AgentCore(
        identity=identity,
        persona_identity=spec.spec.persona.identity,
        queue=queue,
        sessions=sessions,
        assembler=assembler,
        provider=provider,
        audit_sink=audit_sink,
        executor=executor,
        router=router,
        memory=memory,
        history=history,
        commands=commands,
        facts=facts,
    )

    channel = spec.spec.channels[0]
    # The bind host convention is shared: inside a container the composer sets
    # DEV_HTTP_HOST=0.0.0.0 (the container boundary is the isolation), 127.0.0.1
    # for a bare-process run.
    bind_host = os.environ.get("DEV_HTTP_HOST", "127.0.0.1")
    adapter: Any
    if isinstance(channel, DevHttpChannel):
        adapter = load_component("dev-http-channel").DevHttpAdapter(
            queue,
            host=bind_host,
            port=channel.port,
            extra_routes=extra_routes,
            gate=gate,
        )
    elif isinstance(channel, WebexChannel):
        # ensure_buildable guarantees signature verification with a named
        # secretEnv; assert it so a future guard regression fails loudly here.
        secret_env = channel.verification.secretEnv
        assert secret_env is not None
        webex = load_component("webex-channel")
        adapter = webex.WebexAdapter(
            queue,
            secret_env=secret_env,
            audit_sink=audit_sink,
            identity=identity,
            host=bind_host,
            port=webex.DEFAULT_WEBHOOK_PORT,
            gate=gate,
        )
    elif isinstance(channel, SlackChannel):
        # Stage 21: ensure_buildable admits exactly webhook transport with
        # signature verification, which requires a named secretEnv (schema);
        # assert it so a future guard regression fails loudly here. The bot
        # token env (SLACK_BOT_TOKEN) is the adapter's fixed convention — the
        # constructor refuses to boot when either variable is absent.
        secret_env = channel.verification.secretEnv
        assert secret_env is not None
        slack = load_component("slack-channel")
        adapter = slack.SlackAdapter(
            queue,
            secret_env=secret_env,
            audit_sink=audit_sink,
            identity=identity,
            host=bind_host,
            port=slack.DEFAULT_WEBHOOK_PORT,
            gate=gate,
        )
    else:
        # ensure_buildable admits only implemented channels; keep the invariant
        # explicit so a future guard regression fails loudly here.
        raise ComponentNotImplementedError(
            f"component not implemented: channel adapter '{channel.type}' (spec.channels[0])"
        )
    events = event_activations(spec)
    if events:
        # Stage 18: the event-intake receiver serves the spec's declared
        # event-subscription activations on its own port. It shares the queue
        # seam with the channel adapter (memory or redis — it does not care)
        # and is deliberately given NO gate: trigger-originated messages
        # bypass the roster with a constructed principal, audited as such
        # (`trigger_event`) — see the component's module docstring.
        intake_module = load_component("event-intake")
        intake = intake_module.EventIntakeAdapter(
            queue,
            activations=events,
            audit_sink=audit_sink,
            identity=identity,
            host=bind_host,
            port=intake_module.DEFAULT_EVENT_INTAKE_PORT,
        )
        adapter = ChannelWithEventIntake(adapter, intake)
    schedules = schedule_activations(spec)
    if schedules:
        # Stage 22: the schedule-trigger clock loop shares the queue seam with
        # the channel adapter (memory or redis — it does not care) and, like
        # the event intake, is deliberately given NO gate: a scheduled firing
        # is a constructed trigger principal (audited as `trigger_event`),
        # not a rostered human — see the component's module docstring. It
        # serves no port: it is a clock, not a listener.
        scheduler = load_component("schedule-trigger").ScheduleTriggerRunner(
            queue,
            activations=schedules,
            audit_sink=audit_sink,
            identity=identity,
        )
        adapter = ChannelWithScheduleTrigger(adapter, scheduler)
    return core, adapter


class _ShutdownRequested(Exception):
    """Internal: SIGTERM/SIGINT arrived — unwind the task group cleanly."""


async def _raise_on_signal(stop: asyncio.Event) -> None:
    await stop.wait()
    raise _ShutdownRequested


async def run(spec_path: str) -> None:
    spec = load_spec(spec_path)
    logging.basicConfig(level=spec.spec.observability.logLevel.upper())
    core, adapter = build_app(spec)
    # SIGTERM (docker stop) and SIGINT trigger a CLEAN unwind so the finally
    # below always runs: the tool executor's close() tears down MCP transports
    # — stdio child process GROUPS included — leaving no orphans behind
    # (stage 7; the mcp-manager's atexit sweep is only the last resort).
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handled_signals: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
            handled_signals.append(sig)
        except NotImplementedError:  # non-POSIX event loop
            break
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(core.run())
            tg.create_task(adapter.serve())
            tg.create_task(_raise_on_signal(stop))
    except* _ShutdownRequested:
        pass  # requested shutdown is not an error
    finally:
        for sig in handled_signals:
            loop.remove_signal_handler(sig)
        if core.executor is not None:
            core.executor.close()
        # Some adapters own an httpx client (webex outbound replies); close it
        # so shutdown leaves no dangling connections. dev-http has no aclose.
        adapter_aclose = getattr(adapter, "aclose", None)
        if adapter_aclose is not None:
            await adapter_aclose()


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m agent_runtime.runner <spec.yaml>", file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    main()

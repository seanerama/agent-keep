"""Model router unit tests — tier selection + per-session token budgets.

Tier rule (stage 9): a tier named after the activation kind ('message')
handles the call; otherwise the default provider does. Budgets: checked
before each call; warn logs + writes ONE budget_warning record per session
and proceeds; block (the schema's refuse) writes a denied model_call record
and fails the call with BudgetExceededError — audited BEFORE it propagates.
"""

import asyncio

import pytest

from agent_runtime.audit import AgentIdentity, AuditRecord, Trigger
from agent_runtime.components.model_router import ModelPrice, ModelRouter
from agent_runtime.components.prompt_assembler import PromptAssembler
from agent_runtime.components.single_session import SingleSessionManager
from agent_runtime.core import AgentCore
from agent_runtime.provider import AssembledPrompt, BudgetExceededError, ProviderReply

IDENTITY = AgentIdentity(slug="t", spec_version="0.1.0", image_digest="sha256:test")
PROMPT = AssembledPrompt(system="persona")
TRIGGER = Trigger(message_id="m-1", purpose="reply to test message")


class _CountingProvider:
    def __init__(self, name: str, *, tokens: int = 10, tokens_out: int = 0) -> None:
        self.name = name
        self.calls = 0
        self._tokens = tokens
        self._tokens_out = tokens_out

    async def complete(self, prompt: AssembledPrompt) -> ProviderReply:
        self.calls += 1
        return ProviderReply(
            text=f"reply from {self.name}", tokens_in=self._tokens, tokens_out=self._tokens_out
        )


class _ListSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.records.append(record)


def _core(router: ModelRouter, default: _CountingProvider, sink: _ListSink) -> AgentCore:
    return AgentCore(
        identity=IDENTITY,
        persona_identity="persona",
        queue=None,  # type: ignore[arg-type] — call_model does not touch the queue
        sessions=SingleSessionManager(),
        assembler=PromptAssembler(),
        provider=default,
        audit_sink=sink,
        router=router,
    )


# ----------------------------------------------------------------- tier selection


def test_message_trigger_routes_to_the_message_tier() -> None:
    default = _CountingProvider("static")
    tier = _CountingProvider("anthropic:claude-test-fixture")
    router = ModelRouter(default=default, tiers={"message": tier})
    assert router.provider_for(TRIGGER) is tier


def test_without_a_matching_tier_the_default_provider_serves() -> None:
    default = _CountingProvider("static")
    other = _CountingProvider("anthropic:claude-test-fixture")
    router = ModelRouter(default=default, tiers={"reasoning": other})
    assert router.provider_for(TRIGGER) is default
    # schedule/event activations (trigger_id) have no buildable tier yet -> default
    assert router.provider_for(Trigger(trigger_id="t-1", purpose="p")) is default
    assert router.provider_for(None) is default


def test_core_audits_the_tier_selected_provider_name() -> None:
    default = _CountingProvider("static")
    tier = _CountingProvider("anthropic:claude-test-fixture")
    sink = _ListSink()
    core = _core(ModelRouter(default=default, tiers={"message": tier}), default, sink)
    reply = asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    assert reply.text == "reply from anthropic:claude-test-fixture"
    assert tier.calls == 1 and default.calls == 0
    [record] = sink.records
    assert record.action.name == "anthropic:claude-test-fixture"


# ------------------------------------------------------------------------ budgets


def test_under_budget_calls_proceed_and_cost_accumulates() -> None:
    provider = _CountingProvider("static", tokens=10)
    router = ModelRouter(default=provider, max_tokens_per_session=25, on_exceed="block")
    sink = _ListSink()
    core = _core(router, provider, sink)
    for _ in range(3):  # 10 + 10 + 10 — third call starts at 20 < 25
        asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    assert provider.calls == 3
    assert all(r.event == "model_call" and r.outcome.status == "ok" for r in sink.records)


def test_refuse_writes_denied_model_call_record_and_fails_gracefully() -> None:
    provider = _CountingProvider("static", tokens=100)
    router = ModelRouter(default=provider, max_tokens_per_session=50, on_exceed="block")
    sink = _ListSink()
    core = _core(router, provider, sink)
    asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))  # spends 100 of 50
    with pytest.raises(BudgetExceededError, match="maxTokensPerSession=50"):
        asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    assert provider.calls == 1, "the refused call must never reach the provider"
    denied = sink.records[-1]
    assert denied.event == "model_call"
    assert denied.outcome.status == "denied"
    assert denied.trigger.message_id == "m-1"
    assert denied.cost is not None and denied.cost.tokens_in == 0
    assert "maxTokensPerSession=50" in denied.action.input_summary
    # every subsequent call keeps refusing (and keeps being audited)
    with pytest.raises(BudgetExceededError):
        asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    assert sink.records[-1].outcome.status == "denied"


def test_warn_logs_audits_once_and_lets_calls_proceed() -> None:
    provider = _CountingProvider("static", tokens=100)
    router = ModelRouter(default=provider, max_tokens_per_session=50, on_exceed="warn")
    sink = _ListSink()
    core = _core(router, provider, sink)
    for _ in range(3):
        asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    assert provider.calls == 3, "onExceed=warn must never block a call"
    warnings = [r for r in sink.records if r.event == "budget_warning"]
    assert len(warnings) == 1, "one warning per session, not one per call"
    [warning] = warnings
    assert warning.outcome.status == "ok"
    assert "onExceed=warn" in warning.action.input_summary
    assert warning.trigger.message_id == "m-1"
    ok_calls = [r for r in sink.records if r.event == "model_call"]
    assert len(ok_calls) == 3 and all(r.outcome.status == "ok" for r in ok_calls)


def test_budgets_are_per_session() -> None:
    provider = _CountingProvider("static", tokens=100)
    router = ModelRouter(default=provider, max_tokens_per_session=50, on_exceed="block")
    sink = _ListSink()
    core = _core(router, provider, sink)
    asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    with pytest.raises(BudgetExceededError):
        asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    # a DIFFERENT session starts with a fresh budget
    reply = asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-2"))
    assert reply.text == "reply from static"


def test_no_budget_means_no_enforcement() -> None:
    provider = _CountingProvider("static", tokens=10_000)
    router = ModelRouter(default=provider)
    sink = _ListSink()
    core = _core(router, provider, sink)
    for _ in range(5):
        asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    assert provider.calls == 5
    assert all(r.event == "model_call" for r in sink.records)


# -------------------------------------------------------------- USD budgets (stage 25)


def test_usd_accounting_matches_tokens_times_declared_rates() -> None:
    """Per-session USD accumulates as tokens_in × input_rate + tokens_out ×
    output_rate (rates per 1,000,000 tokens) — deterministic, exact."""
    provider = _CountingProvider("anthropic:m")
    price = ModelPrice(usd_per_million_input=3.0, usd_per_million_output=15.0)
    # 100000 * 3/1e6 + 20000 * 15/1e6 = 0.3 + 0.3 = 0.6 exactly.
    assert price.cost(tokens_in=100_000, tokens_out=20_000) == 0.6
    router = ModelRouter(default=provider, max_usd_per_session=1.0, default_price=price)
    router.record_cost("s-1", tokens_in=100_000, tokens_out=20_000, provider=provider)
    assert router.budget_verdict("s-1").action == "ok"  # 0.6 < 1.0
    router.record_cost("s-1", tokens_in=100_000, tokens_out=20_000, provider=provider)
    assert router.budget_verdict("s-1").action == "refuse"  # 1.2 >= 1.0


def test_usd_block_at_the_exact_boundary() -> None:
    """A session at EXACTLY the USD ceiling refuses the next call (checked
    before the call, overshoot by at most one — the token-budget semantics)."""
    provider = _CountingProvider("anthropic:m")
    price = ModelPrice(usd_per_million_input=3.0, usd_per_million_output=15.0)
    router = ModelRouter(default=provider, max_usd_per_session=0.6, default_price=price)
    router.record_cost("s-1", tokens_in=100_000, tokens_out=20_000, provider=provider)  # 0.6
    assert router.budget_verdict("s-1").action == "refuse"  # 0.6 >= 0.6, exact boundary


def test_usd_block_writes_denied_record_and_fails_gracefully() -> None:
    """onExceed=block over the USD ceiling: the refused call writes a denied
    model_call record naming maxUsdPerSession, then raises — audited before it
    propagates, provider never reached, at the exact boundary."""
    # tokens_in=250000 @ $2/M = $0.5 per call; cap $1.0 -> call 3 refuses.
    provider = _CountingProvider("anthropic:m", tokens=250_000, tokens_out=0)
    price = ModelPrice(usd_per_million_input=2.0, usd_per_million_output=2.0)
    router = ModelRouter(
        default=provider, max_usd_per_session=1.0, on_exceed="block", default_price=price
    )
    sink = _ListSink()
    core = _core(router, provider, sink)
    asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))  # 0.5
    asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))  # 1.0 (exact boundary)
    with pytest.raises(BudgetExceededError, match="maxUsdPerSession=1.0"):
        asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    assert provider.calls == 2, "the refused call must never reach the provider"
    denied = sink.records[-1]
    assert denied.event == "model_call"
    assert denied.outcome.status == "denied"
    assert denied.cost is not None and denied.cost.tokens_in == 0
    assert "maxUsdPerSession=1.0" in denied.action.input_summary


def test_usd_warn_continues_with_one_audit() -> None:
    provider = _CountingProvider("anthropic:m", tokens=250_000, tokens_out=0)
    price = ModelPrice(usd_per_million_input=2.0, usd_per_million_output=2.0)
    router = ModelRouter(
        default=provider, max_usd_per_session=1.0, on_exceed="warn", default_price=price
    )
    sink = _ListSink()
    core = _core(router, provider, sink)
    for _ in range(4):
        asyncio.run(core.call_model(PROMPT, TRIGGER, session_id="s-1"))
    assert provider.calls == 4, "onExceed=warn must never block a call"
    warnings = [r for r in sink.records if r.event == "budget_warning"]
    assert len(warnings) == 1, "one warning per session, not one per call"
    assert "maxUsdPerSession" in warnings[0].action.input_summary
    assert "onExceed=warn" in warnings[0].action.input_summary


def test_usd_charges_each_call_at_its_own_tier_rate() -> None:
    """The USD charge uses the rate of the provider that SERVED the call: a
    call routed to an expensive tier is charged at that tier's rate, not the
    default's."""
    default = _CountingProvider("default")
    tier = _CountingProvider("tier")
    router = ModelRouter(
        default=default,
        tiers={"message": tier},
        max_usd_per_session=5.0,
        default_price=ModelPrice(usd_per_million_input=1.0, usd_per_million_output=1.0),
        tier_prices={
            "message": ModelPrice(usd_per_million_input=10.0, usd_per_million_output=10.0)
        },
    )
    # 1,000,000 input tokens: tier @ $10/M = $10 (over $5); default @ $1/M = $1 (under).
    router.record_cost("s-tier", tokens_in=1_000_000, tokens_out=0, provider=tier)
    assert router.budget_verdict("s-tier").action == "refuse"
    router.record_cost("s-def", tokens_in=1_000_000, tokens_out=0, provider=default)
    assert router.budget_verdict("s-def").action == "ok"


def test_token_and_usd_budgets_coexist() -> None:
    """Both ceilings can be set; whichever is hit first refuses. Here the token
    ceiling trips before the USD one."""
    provider = _CountingProvider("anthropic:m", tokens=100, tokens_out=0)
    price = ModelPrice(usd_per_million_input=1.0, usd_per_million_output=1.0)
    router = ModelRouter(
        default=provider,
        max_tokens_per_session=50,
        max_usd_per_session=1000.0,
        on_exceed="block",
        default_price=price,
    )
    router.record_cost("s-1", tokens_in=100, tokens_out=0, provider=provider)
    verdict = router.budget_verdict("s-1")
    assert verdict.action == "refuse"
    assert "maxTokensPerSession=50" in verdict.note  # token ceiling hit first

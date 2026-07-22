"""Model router — spec-driven tier selection + per-session token budgets.

Blueprint model/llmrouter (stage 9): named tiers map to providers; budgets are
per-session cost control. The router is a COMPONENT — a spec with no tiers and
no budgets wires no router at all and the core's behavior is bit-identical to
pre-stage-9 (absence semantics, contract agent-spec rule 2).

Tier-selection rule (this stage — deliberately minimal, fully spec-driven):

    A tier whose NAME equals the activation kind of the call's trigger
    handles the call; otherwise the spec's default provider does.

Message-triggered calls (the only buildable activation today) therefore route
to a tier literally named ``message`` when the spec declares one. The rule is
a hook, not a destination — Phase 3+ may add per-purpose routing (schedule /
event activations once those triggers build), task-type classification
("cheap model for triage, flagship for reasoning"), and cost-aware fallback
chains. Keeping the rule this small is a decision, documented here, not an
accident.

Budget semantics (``models.budgets.maxTokensPerSession``):

- Spend is counted per session id: tokens_in + tokens_out of every completed
  call (the core reports them from the audited cost).
- The verdict is checked BEFORE each call against tokens already spent, so a
  session may overshoot by at most one call — a call's cost is unknowable
  until the provider answers.
- ``onExceed: warn``  -> the first over-budget call per session yields a
  ``warn`` verdict (the core logs it and writes a ``budget_warning`` audit
  record); later calls proceed as ``ok`` — one warning per session, not one
  per call, keeps the append-only log signal, not noise.
- ``onExceed: block`` -> every over-budget call yields ``refuse``: the core
  writes a denied ``model_call`` record and raises BudgetExceededError (the
  spec schema's 'block' IS the stage's refuse semantics).

Budget semantics (``models.budgets.maxUsdPerSession`` — stage 25):

- The USD budget enforces against OPERATOR-declared pricing
  (``models.*.pricing``), not a library price table — a stale table silently
  mis-enforces, so the operator declares rates next to each model. The spec
  cross-validation refuses a USD budget over any unpriced/static path at LOAD,
  so every provider the router can select has a known rate by construction.
- USD spend accumulates per session exactly like tokens: each completed call
  charges ``tokens_in × input_rate + tokens_out × output_rate`` (rates are USD
  per 1,000,000 tokens) at the rate of the provider that served the call. The
  verdict is checked BEFORE each call, so a session overshoots by at most one.
- ``onExceed`` applies to BOTH ceilings; the first over-budget call (token OR
  USD) per session warns (one record) or blocks, identical to the token path.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from agent_runtime.audit import Trigger
from agent_runtime.provider import BudgetVerdict, ModelProvider

logger = logging.getLogger(__name__)

#: Activation kind of a message-triggered call (Trigger.message_id non-null).
_MESSAGE_KIND = "message"

#: One million — the pricing denominator (rates are USD per 1,000,000 tokens).
_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """Operator-declared USD rates for one provider (models.*.pricing), the
    numbers the USD budget enforces against."""

    usd_per_million_input: float
    usd_per_million_output: float

    def cost(self, *, tokens_in: int, tokens_out: int) -> float:
        return (
            tokens_in * self.usd_per_million_input + tokens_out * self.usd_per_million_output
        ) / _PER_MILLION


class ModelRouter:
    def __init__(
        self,
        *,
        default: ModelProvider,
        tiers: Mapping[str, ModelProvider] | None = None,
        max_tokens_per_session: int | None = None,
        max_usd_per_session: float | None = None,
        on_exceed: Literal["block", "warn"] = "block",
        default_price: ModelPrice | None = None,
        tier_prices: Mapping[str, ModelPrice | None] | None = None,
    ) -> None:
        self._default = default
        self._tiers = dict(tiers or {})
        self._cap = max_tokens_per_session
        self._usd_cap = max_usd_per_session
        self._on_exceed = on_exceed
        self._spent: dict[str, int] = {}
        self._usd_spent: dict[str, float] = {}
        self._warned: set[str] = set()
        # Rates keyed by the identity of the SELECTED provider object — the same
        # object provider_for() returns and the core hands back to record_cost,
        # so a call is charged at exactly its own tier's declared rate.
        prices = dict(tier_prices or {})
        self._price_by_provider: dict[int, ModelPrice] = {}
        if default_price is not None:
            self._price_by_provider[id(default)] = default_price
        for name, provider in self._tiers.items():
            price = prices.get(name)
            if price is not None:
                self._price_by_provider[id(provider)] = price

    def provider_for(self, trigger: Trigger | None) -> ModelProvider:
        """The stage-9 rule: tier named after the activation kind, else default."""
        if trigger is not None and trigger.message_id is not None:
            tier = self._tiers.get(_MESSAGE_KIND)
            if tier is not None:
                return tier
        return self._default

    def _exceeded_note(self, session_id: str) -> str | None:
        """The budget note if this session is at/over any ceiling, else None.

        Token budget is checked first (stable message for existing callers),
        then USD; either exhausted ceiling refuses/warns the next call."""
        if self._cap is not None and self._spent.get(session_id, 0) >= self._cap:
            spent = self._spent.get(session_id, 0)
            return (
                f"session '{session_id}' spent {spent} model tokens; "
                f"budget maxTokensPerSession={self._cap}"
            )
        if self._usd_cap is not None and self._usd_spent.get(session_id, 0.0) >= self._usd_cap:
            spent_usd = self._usd_spent.get(session_id, 0.0)
            return (
                f"session '{session_id}' spent ${spent_usd:.6f} in model usage; "
                f"budget maxUsdPerSession={self._usd_cap}"
            )
        return None

    def budget_verdict(self, session_id: str) -> BudgetVerdict:
        note = self._exceeded_note(session_id)
        if note is None:
            return BudgetVerdict(action="ok")
        if self._on_exceed == "warn":
            if session_id in self._warned:
                return BudgetVerdict(action="ok")
            self._warned.add(session_id)
            return BudgetVerdict(action="warn", note=f"{note} — onExceed=warn, call allowed")
        return BudgetVerdict(action="refuse", note=f"{note} — onExceed=block, model call refused")

    def record_cost(
        self,
        session_id: str,
        *,
        tokens_in: int,
        tokens_out: int,
        provider: ModelProvider | None = None,
    ) -> None:
        self._spent[session_id] = self._spent.get(session_id, 0) + tokens_in + tokens_out
        if self._usd_cap is None or provider is None:
            return
        price = self._price_by_provider.get(id(provider))
        if price is None:
            return  # unpriced path — cross-validation forbids this under a USD budget
        self._usd_spent[session_id] = self._usd_spent.get(session_id, 0.0) + price.cost(
            tokens_in=tokens_in, tokens_out=tokens_out
        )

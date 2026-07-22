"""run-lifecycle@1 — RunState + RunHeartbeat, the live-plane vocabulary.

Contract: contracts/run-lifecycle.md (frozen v1, ADR 0014). Shape ONLY: no
emitter, no sink, no cadence, no consumer exists in v1 — this module is the
drift-guarded target a future heartbeat emitter component builds against, and
it is deliberately NOT in the composer's CORE_MODULES, so no built image ships
or imports it (emission is spec-governed and opt-in, default none).

Key invariants carried by the shape:

- `run_id` IS the activation id — the non-null one of the triggering
  `internal-message.id` / trigger activation id, i.e. exactly the value every
  audit record carries as `trigger.message_id`/`trigger_id`. One run = one
  activation; the heartbeat↔audit join is key equality, never timestamps, and
  NO second run key exists.
- CLOSED VOCABULARIES ONLY (anti-exfiltration): `status` and `current_step`
  come from closed, runtime-owned sets — never text the model composed or
  influenced. The token grammar on `current_step` is the shape-level half of
  that invariant; the runtime-owned-only guarantee is an emitter-side property
  and lands with the emitter.
- STATE, NOT DATA: a heartbeat carries no message content, no tool I/O, no
  prompt text, no secret values.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.audit import AgentIdentity

#: Closed run-state enum — EXACTLY the transitions the runtime can produce
#: today (queued → running → awaiting_approval → running → completed;
#: running → failed). There is deliberately NO `rejected` (an approval denial
#: does not terminate the run — the tool outcome is `denied` and the run
#: resumes) and NO `cancelled` (nothing can cancel a run in v1). Future states
#: arrive ADDITIVELY under the contract's versioning rule; consumers MUST
#: tolerate unknown states by treating them as non-terminal.
RunState = Literal["queued", "running", "awaiting_approval", "completed", "failed"]

#: The closed token grammar for `current_step` (anti-exfiltration shape guard):
#: lowercase component/operation tokens — letters, digits, `_`, `.`, `-`,
#: starting with a letter or digit, at most 128 chars. Free-form prose (spaces,
#: uppercase, punctuation) cannot validate.
CURRENT_STEP_PATTERN = r"^[a-z0-9][a-z0-9_.-]*$"
CURRENT_STEP_MAX_LENGTH = 128


class RunHeartbeat(BaseModel):
    """One run-lifecycle@1 heartbeat record — live state, emitted DURING a run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        description="THE activation id — the non-null trigger.message_id/trigger_id "
        "every audit record of the run carries; NOT a freshly minted uuid."
    )
    agent: AgentIdentity = Field(
        description="Identical block to audit-record `agent` (shared identity model)."
    )
    status: RunState
    current_step: str = Field(
        pattern=CURRENT_STEP_PATTERN,
        max_length=CURRENT_STEP_MAX_LENGTH,
        description="Token from the runtime-owned step vocabulary, e.g. "
        "`executor.local_tool.read_bundle`, `provider.complete`. Closed grammar "
        "`^[a-z0-9][a-z0-9_.-]*$` (<=128 chars): lowercase component/op tokens only, "
        "so free-form prose/model text cannot validate. This is the shape-level half "
        "of the contract's closed-vocabulary (anti-exfiltration) invariant — the "
        "runtime-owned-only guarantee lands with the emitter.",
    )
    started_at: datetime = Field(description="RFC 3339, UTC — when this run left `queued`.")
    last_seen: datetime = Field(
        description="RFC 3339, UTC — emission time; freshness signal, NOT ordering "
        "(consumers use max-seen-per-run and must not assume monotonicity)."
    )
    worker_id: str = Field(description="Container/process instance identity.")
    trace_id: str | None = Field(
        default=None,
        description="Reserved analytics-plane correlation key (ADR 0014); "
        "no tracer exists in v1 — null today.",
    )

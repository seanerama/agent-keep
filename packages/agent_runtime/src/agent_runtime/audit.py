"""AuditRecord + append-only sink interface.

Contract: contracts/audit-record.md (frozen v1). The vision's claim — "each
tool call carries its purpose into the audit log" — is enforced here:
`trigger` is mandatory with EXACTLY ONE of message_id / trigger_id non-null,
and an unrecordable call must not execute (see core.AgentCore._call_model,
which refuses execution without a trigger).
"""

from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TriggerRefusedError(RuntimeError):
    """Raised when an action arrives without a valid trigger — execution is refused."""


class AgentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    spec_version: str = Field(description="metadata.specVersion the running image was built from.")
    image_digest: str = Field(description="sha256 of the running image.")


class Trigger(BaseModel):
    """The WHY — required, never null. Exactly one of message_id/trigger_id is non-null."""

    model_config = ConfigDict(extra="forbid")

    message_id: str | None = Field(default=None, description="internal-message id that led here.")
    trigger_id: str | None = Field(default=None, description="Schedule/event activation id.")
    purpose: str = Field(min_length=1, description="Short human-readable statement.")

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "Trigger":
        if (self.message_id is None) == (self.trigger_id is None):
            raise ValueError(
                "trigger requires exactly one of message_id / trigger_id to be non-null"
            )
        return self


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Tool/model/operation name as declared in the spec.")
    input_digest: str = Field(description="sha256 of canonicalized inputs.")
    input_summary: str = Field(description="Redacted human-readable summary.")


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "error", "denied", "pending_approval"]
    output_digest: str | None = Field(default=None, description="sha256, when applicable.")


class Approval(BaseModel):
    """Always present on tool_call events."""

    model_config = ConfigDict(extra="forbid")

    required: bool
    decided_by: str = Field(description='internal_user_id or "policy:auto" when required=false.')


class Cost(BaseModel):
    """Present on model_call events."""

    model_config = ConfigDict(extra="forbid")

    tokens_in: int
    tokens_out: int


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    agent: AgentIdentity
    # "budget_warning" (stage 9), "gateway_reject" (stage 10), and
    # "trigger_event" (stage 18) are ADDITIVE event types per the contract's
    # versioning rule ("Changes are additive only (new event types, ...)" —
    # audit-record.md §Versioning, frozen v1): no contract edit is required.
    #   - budget_warning: a session crossed its token budget with onExceed=warn
    #     — the call proceeded.
    #   - gateway_reject: the gateway dropped an inbound message before it
    #     reached the queue — an unverifiable/bad-signature webhook, or a sender
    #     not permitted by the spec's allowlist roster. NOTHING ran downstream;
    #     the drop itself is the recorded action (outcome denied, no approval or
    #     cost block).
    #   - trigger_event: a non-inbound activation became a synthetic
    #     trigger-originated message — an event-subscription delivery that
    #     passed shared-secret verification (stage 18,
    #     components/event_intake) or a schedule boundary that fired
    #     (stage 22, components/schedule_trigger). The documented roster
    #     BYPASS for trigger principals is audited by exactly this record: it
    #     names the activation and the constructed principal, and its
    #     trigger_id is the id every model_call/tool_call record of the
    #     triggered turn carries.
    event: Literal[
        "tool_call",
        "approval",
        "memory_write",
        "model_call",
        "budget_warning",
        "gateway_reject",
        "trigger_event",
    ]
    trigger: Trigger
    action: Action
    outcome: Outcome
    approval: Approval | None = None
    cost: Cost | None = None
    # Amendment (2026-07-14), ADR 0014: additive optional field per the
    # contract's versioning rule — reserved analytics-plane correlation key.
    # No tracer exists today; producers simply do not set it (null on every
    # record), so NO producer changes anywhere.
    trace_id: str | None = Field(
        default=None,
        description="Reserved analytics-plane correlation key (ADR 0014); "
        "no tracer exists — null today.",
    )

    @model_validator(mode="after")
    def _event_blocks(self) -> "AuditRecord":
        if self.event == "tool_call" and self.approval is None:
            raise ValueError("approval block is required on tool_call events")
        if self.event == "model_call" and self.cost is None:
            raise ValueError("cost block is required on model_call events")
        return self


class AuditSink(Protocol):
    """Append-only at the interface level: no update, no delete, no read-back."""

    def append(self, record: AuditRecord) -> None: ...

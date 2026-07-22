"""run-lifecycle@1 unit tests (contract: run-lifecycle, frozen v1, ADR 0014).

Drift-guards the RunState enum and the RunHeartbeat wire shape against the
frozen contract, and pins the shape-level anti-exfiltration guard on
`current_step` (closed token grammar — free-form prose cannot validate).
"""

from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from agent_runtime.audit import AgentIdentity
from agent_runtime.lifecycle import (
    CURRENT_STEP_MAX_LENGTH,
    RunHeartbeat,
    RunState,
)

IDENTITY = AgentIdentity(slug="skeleton", spec_version="0.1.0", image_digest="sha256:test")
TS = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _heartbeat(**overrides: object) -> RunHeartbeat:
    fields: dict[str, object] = {
        "run_id": "m-1",
        "agent": IDENTITY,
        "status": "running",
        "current_step": "executor.local_tool.read_bundle",
        "started_at": TS,
        "last_seen": TS,
        "worker_id": "worker-1",
    }
    fields.update(overrides)
    return RunHeartbeat(**fields)  # type: ignore[arg-type]


def test_run_state_members_exactly_match_the_frozen_contract() -> None:
    """CLOSED enum — exactly the contract's set. `rejected` is deliberately
    absent (an approval denial resumes the run with a denied tool outcome) and
    so is `cancelled` (nothing can cancel a run in v1)."""
    assert set(get_args(RunState)) == {
        "queued",
        "running",
        "awaiting_approval",
        "completed",
        "failed",
    }


def test_wire_shape_matches_the_frozen_contract() -> None:
    """Every field the contract's wire block freezes is present; only
    `trace_id` is optional; `agent` REUSES the audit-record identity block."""
    fields = RunHeartbeat.model_fields
    assert set(fields) == {
        "run_id",
        "agent",
        "status",
        "current_step",
        "started_at",
        "last_seen",
        "worker_id",
        "trace_id",
    }
    required = {name for name, field in fields.items() if field.is_required()}
    assert required == set(fields) - {"trace_id"}
    assert fields["agent"].annotation is AgentIdentity, (
        "agent must be the SHARED audit-record identity block, not a duplicate"
    )


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _heartbeat(status="rejected")
    with pytest.raises(ValidationError):
        _heartbeat(status="cancelled")


def test_current_step_rejects_free_form_prose() -> None:
    """The shape-level half of the closed-vocabulary (anti-exfiltration)
    invariant: model-composed prose cannot validate as a step token."""
    for prose in (
        "Correlating alarms for the user",
        "Executor.Local_Tool.Read_Bundle",  # uppercase
        "step with spaces",
        "_leading.underscore",  # must start with a letter/digit
        "",
    ):
        with pytest.raises(ValidationError):
            _heartbeat(current_step=prose)


def test_current_step_accepts_runtime_owned_tokens_within_bounds() -> None:
    assert _heartbeat(current_step="executor.local_tool.read_bundle").current_step == (
        "executor.local_tool.read_bundle"
    )
    assert _heartbeat(current_step="provider.complete").status == "running"
    at_bound = "a" * CURRENT_STEP_MAX_LENGTH
    assert _heartbeat(current_step=at_bound).current_step == at_bound
    with pytest.raises(ValidationError):
        _heartbeat(current_step="a" * (CURRENT_STEP_MAX_LENGTH + 1))


def test_trace_id_is_optional_and_nullable() -> None:
    assert _heartbeat().trace_id is None
    assert _heartbeat(trace_id=None).trace_id is None
    assert _heartbeat(trace_id="trace-1").trace_id == "trace-1"


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _heartbeat(step_detail="free text smuggled in an unknown field")


def test_heartbeat_round_trips_through_json() -> None:
    heartbeat = _heartbeat(status="awaiting_approval", trace_id="trace-1")
    assert RunHeartbeat.model_validate_json(heartbeat.model_dump_json()) == heartbeat

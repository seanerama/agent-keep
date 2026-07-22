"""audit-record v1 unit tests — mandatory trigger, exactly-one source, refusal."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime.audit import (
    Action,
    AgentIdentity,
    Approval,
    AuditRecord,
    Cost,
    Outcome,
    Trigger,
    TriggerRefusedError,
)
from agent_runtime.components.jsonl_audit import JsonlAuditSink
from agent_runtime.components.prompt_assembler import PromptAssembler
from agent_runtime.components.single_session import SingleSessionManager
from agent_runtime.core import AgentCore, prompt_digest
from agent_runtime.provider import AssembledPrompt, ProviderReply

IDENTITY = AgentIdentity(slug="skeleton", spec_version="0.1.0", image_digest="sha256:test")
ACTION = Action(name="static", input_digest="sha256:abc", input_summary="test")


def test_trigger_requires_exactly_one_source() -> None:
    with pytest.raises(ValidationError):
        Trigger(purpose="no source at all")
    with pytest.raises(ValidationError):
        Trigger(message_id="m-1", trigger_id="t-1", purpose="both sources")
    assert Trigger(message_id="m-1", purpose="ok").message_id == "m-1"
    assert Trigger(trigger_id="t-1", purpose="ok").trigger_id == "t-1"


def test_model_call_requires_cost() -> None:
    with pytest.raises(ValidationError):
        AuditRecord(
            agent=IDENTITY,
            event="model_call",
            trigger=Trigger(message_id="m-1", purpose="p"),
            action=ACTION,
            outcome=Outcome(status="ok"),
        )


def test_tool_call_requires_approval_block() -> None:
    with pytest.raises(ValidationError):
        AuditRecord(
            agent=IDENTITY,
            event="tool_call",
            trigger=Trigger(message_id="m-1", purpose="p"),
            action=ACTION,
            outcome=Outcome(status="ok"),
        )
    record = AuditRecord(
        agent=IDENTITY,
        event="tool_call",
        trigger=Trigger(message_id="m-1", purpose="p"),
        action=ACTION,
        outcome=Outcome(status="ok"),
        approval=Approval(required=False, decided_by="policy:auto"),
    )
    assert record.approval is not None and record.approval.decided_by == "policy:auto"


def test_jsonl_sink_appends_records(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path / "audit" / "log.jsonl")
    record = AuditRecord(
        agent=IDENTITY,
        event="model_call",
        trigger=Trigger(message_id="m-1", purpose="p"),
        action=ACTION,
        outcome=Outcome(status="ok", output_digest="sha256:def"),
        cost=Cost(tokens_in=3, tokens_out=5),
    )
    sink.append(record)
    sink.append(record)
    lines = (tmp_path / "audit" / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["event"] == "model_call"
    assert parsed["trigger"]["message_id"] == "m-1"
    assert parsed["cost"] == {"tokens_in": 3, "tokens_out": 5}


class _RecordingProvider:
    name = "static"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, prompt: AssembledPrompt) -> ProviderReply:
        self.calls += 1
        return ProviderReply(text="hi", tokens_in=1, tokens_out=1)


class _ListSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.records.append(record)


def test_model_call_without_trigger_is_refused_before_execution() -> None:
    provider = _RecordingProvider()
    sink = _ListSink()
    core = AgentCore(
        identity=IDENTITY,
        persona_identity="persona",
        # call_model does not touch the queue
        queue=None,  # type: ignore[arg-type]
        sessions=SingleSessionManager(),
        assembler=PromptAssembler(),
        provider=provider,
        audit_sink=sink,
    )
    with pytest.raises(TriggerRefusedError):
        asyncio.run(core.call_model(AssembledPrompt(system="persona"), None))
    assert provider.calls == 0, "provider must not run without a trigger"
    assert sink.records == []


class _FailingProvider:
    name = "static"

    async def complete(self, prompt: AssembledPrompt) -> ProviderReply:
        raise RuntimeError("provider exploded")


class _OfflineSink:
    def append(self, record: AuditRecord) -> None:
        raise OSError("audit sink offline")


def _core(
    provider: _RecordingProvider | _FailingProvider, sink: _ListSink | _OfflineSink
) -> AgentCore:
    return AgentCore(
        identity=IDENTITY,
        persona_identity="persona",
        # call_model does not touch the queue
        queue=None,  # type: ignore[arg-type]
        sessions=SingleSessionManager(),
        assembler=PromptAssembler(),
        provider=provider,
        audit_sink=sink,
    )


def test_provider_failure_writes_error_record_then_reraises() -> None:
    """Issue #7: a raising provider is still a model call — recorded, then re-raised."""
    sink = _ListSink()
    core = _core(_FailingProvider(), sink)
    prompt = AssembledPrompt(system="persona")
    trigger = Trigger(message_id="m-1", purpose="p")
    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(core.call_model(prompt, trigger))
    [record] = sink.records
    assert record.event == "model_call"
    assert record.outcome.status == "error"
    assert record.outcome.output_digest is None, "no reply — nothing to digest"
    assert record.action.name == "static"
    assert record.action.input_digest == prompt_digest(prompt), (
        "digest of what was SENT (no payload) must be recorded even on failure"
    )
    assert record.trigger.message_id == "m-1"
    assert record.cost == Cost(tokens_in=0, tokens_out=0)


def test_offline_sink_fails_the_model_call() -> None:
    """Sink offline ⇒ the call raises — it can never silently succeed unrecorded."""
    core = _core(_RecordingProvider(), _OfflineSink())
    trigger = Trigger(message_id="m-1", purpose="p")
    with pytest.raises(OSError, match="audit sink offline"):
        asyncio.run(core.call_model(AssembledPrompt(system="persona"), trigger))


def test_record_without_trace_id_validates_and_serializes_null() -> None:
    """Amendment (2026-07-14): `trace_id` is additive-optional — every
    producer today simply does not set it, and such records stay valid."""
    record = AuditRecord(
        agent=IDENTITY,
        event="model_call",
        trigger=Trigger(message_id="m-1", purpose="p"),
        action=ACTION,
        outcome=Outcome(status="ok"),
        cost=Cost(tokens_in=1, tokens_out=1),
    )
    assert record.trace_id is None
    assert json.loads(record.model_dump_json())["trace_id"] is None


def test_record_with_trace_id_round_trips() -> None:
    record = AuditRecord(
        agent=IDENTITY,
        event="model_call",
        trigger=Trigger(message_id="m-1", purpose="p"),
        action=ACTION,
        outcome=Outcome(status="ok"),
        cost=Cost(tokens_in=1, tokens_out=1),
        trace_id="trace-1",
    )
    reloaded = AuditRecord.model_validate_json(record.model_dump_json())
    assert reloaded.trace_id == "trace-1"
    assert reloaded == record

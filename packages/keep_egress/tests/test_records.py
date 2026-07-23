"""Contract tests for the `egress` audit record — audit-record v1, additive
record kind (contracts/audit-record.md + contracts/egress-observation.md).

The field names asserted here FROZE with this stage's first green test and are
additive-only from then on (the authoritative list is the keep_egress package
docstring). These tests are the freeze: renaming or removing a field fails
here before it fails any consumer.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from keep_egress.records import EgressAuditRecord, EgressJsonlSink, ObservedAgent

AGENT = ObservedAgent(slug="default-chatbot", spec_version="0.1.0")

#: THE frozen field roster (additive-only from the first green test).
#: `connection_id` was ADDED additively (issue #24 / egress-observation amendment
#: 2026-07-22) — the correlation seam pairing an allowed CONNECT's open+close
#: records; the same additive path as audit-record's `trace_id`. Nothing was
#: removed or renamed.
FROZEN_TOP_LEVEL_FIELDS = {
    "id",
    "ts",
    "agent",
    "event",
    "action",
    "target",
    "verdict",
    "matched_entry",
    "bytes_up",
    "bytes_down",
    "run_id",
    "connection_id",
}
FROZEN_AGENT_FIELDS = {"slug", "spec_version"}


def _record(**overrides: object) -> EgressAuditRecord:
    payload: dict[str, object] = {
        "agent": AGENT,
        "target": "api.anthropic.com:443",
        "verdict": "allowed",
        "matched_entry": "api.anthropic.com:443",
    }
    payload.update(overrides)
    return EgressAuditRecord.model_validate(payload)


def test_frozen_field_names() -> None:
    """Removing a field or renaming it breaks this assertion; ADDING one is
    the (allowed) additive path and must extend the roster consciously."""
    assert set(EgressAuditRecord.model_fields) == FROZEN_TOP_LEVEL_FIELDS
    assert set(ObservedAgent.model_fields) == FROZEN_AGENT_FIELDS


def test_audit_record_v1_envelope_shape() -> None:
    """The egress kind sits in the audit-record v1 plane: uuid id, RFC 3339
    UTC ts, agent identity block, `event` as the record kind and — per the
    egress-observation wire section — event `egress` with action `connect`."""
    record = _record()
    UUID(record.id)  # id is a uuid
    assert record.ts.tzinfo is not None
    assert record.ts.utcoffset() == datetime.now(UTC).utcoffset()
    assert record.event == "egress"
    assert record.action == "connect"
    assert record.agent.slug == "default-chatbot"


def test_contract_minimum_fields_and_v1_defaults() -> None:
    """Contract minimum: target host:port, verdict, matched entry (or none),
    byte counts on close, run-correlation key when attributable — the v1
    proxy is not run-aware, so run_id defaults to null."""
    record = _record()
    assert record.target == "api.anthropic.com:443"
    assert record.verdict == "allowed"
    assert record.matched_entry == "api.anthropic.com:443"
    assert record.bytes_up == 0 and record.bytes_down == 0
    assert record.run_id is None


def test_denied_record_carries_null_matched_entry() -> None:
    record = _record(verdict="denied", matched_entry=None)
    assert record.verdict == "denied"
    assert record.matched_entry is None


def test_open_action_validates_additively() -> None:
    """Issue #24: `action: open` is the new additive value for the real-time
    record emitted at CONNECT establish — it validates, alongside the existing
    `connect`. A default record is still `connect` (nothing existing changed)."""
    assert _record().action == "connect"  # default unchanged
    opened = _record(action="open", bytes_up=0, bytes_down=0)
    assert opened.action == "open"
    assert opened.event == "egress"  # same record kind / plane


def test_connection_id_defaults_to_a_unique_uuid() -> None:
    """The additive `connection_id` field mints a fresh uuid per record when the
    producer supplies none — so single (unpaired) records each get their own."""
    a = _record()
    b = _record()
    UUID(a.connection_id)  # is a uuid
    assert a.connection_id != b.connection_id  # unique per record by default


def test_open_and_close_records_pair_by_shared_connection_id() -> None:
    """A producer threads ONE connection_id through an allowed CONNECT's `open`
    (at establish, zero bytes) and `connect` (on close, final bytes) records so
    the pair correlates on the wire."""
    cid = "11111111-2222-3333-4444-555555555555"
    opened = _record(action="open", connection_id=cid, bytes_up=0, bytes_down=0)
    closed = _record(action="connect", connection_id=cid, bytes_up=2459, bytes_down=6063)
    assert opened.connection_id == closed.connection_id == cid
    assert (opened.bytes_up, opened.bytes_down) == (0, 0)
    assert (closed.bytes_up, closed.bytes_down) == (2459, 6063)


def test_shape_is_strict() -> None:
    """extra=forbid, verdict/event/action are closed enums, byte counts are
    non-negative — a drifting producer fails validation, never logs garbage."""
    with pytest.raises(ValidationError):
        _record(unexpected_field="x")
    with pytest.raises(ValidationError):
        _record(verdict="maybe")
    with pytest.raises(ValidationError):
        _record(event="tool_call")
    with pytest.raises(ValidationError):
        _record(action="read")
    with pytest.raises(ValidationError):
        _record(bytes_up=-1)


def test_json_line_round_trips_with_frozen_names(tmp_path: Path) -> None:
    """A sink line re-validates and exposes exactly the frozen names on the
    wire — what CI's container job greps is this shape."""
    sink = EgressJsonlSink(tmp_path / "egress-audit.jsonl")
    sink.append(_record())
    sink.append(_record(verdict="denied", matched_entry=None, target="attacker.invalid:80"))
    lines = (tmp_path / "egress-audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # append-only: second write appended, not replaced
    for line in lines:
        payload = json.loads(line)
        assert set(payload) == FROZEN_TOP_LEVEL_FIELDS
        assert set(payload["agent"]) == FROZEN_AGENT_FIELDS
        EgressAuditRecord.model_validate(payload)
    assert json.loads(lines[1])["verdict"] == "denied"

"""Generic gateway allowlist enforcement (stage 10).

Verifies the spec-driven gate maps platform identity onto the roster, admits by
tier, and drops unknown/under-tiered senders with an ADDITIVE `gateway_reject`
audit record — and that a dropped message produces NOTHING downstream. The gate
is exercised both directly and through the dev-http adapter (so the roster is
enforced with zero platform credentials).
"""

import asyncio
import json

import pytest

from agent_runtime.audit import AgentIdentity, AuditRecord
from agent_runtime.components.dev_http import DevHttpAdapter
from agent_runtime.gateway import AllowlistGate
from agent_runtime.messages import ChannelRef, ContentBlock, InternalMessage, Provenance, Sender
from agent_runtime.queues import QueueItem
from keep_spec.models import AllowlistEntry, GatewayAllowlist

IDENTITY = AgentIdentity(slug="a", spec_version="0.1.0", image_digest="sha256:test")


class _ListSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.records.append(record)


class _RecordingQueue:
    def __init__(self) -> None:
        self.items: list[QueueItem] = []

    async def put(self, item: QueueItem) -> None:
        self.items.append(item)

    async def get(self) -> QueueItem:  # pragma: no cover - not exercised here
        raise AssertionError("get() must not be called in these tests")


def _message(platform: str, platform_id: str) -> InternalMessage:
    return InternalMessage(
        channel=ChannelRef(platform=platform, conversation_id="room-1"),
        sender=Sender(kind="human", platform_id=platform_id, internal_user_id=None, verified=True),
        content=[ContentBlock(type="text", text="hi")],
        provenance=Provenance(adapter="test", trust="untrusted"),
    )


def _tiered_gate(sink: _ListSink) -> AllowlistGate:
    allowlist = GatewayAllowlist(
        policy="tiered",
        roster=[
            AllowlistEntry(id="webex:nina@example.com", tier="owner"),
            AllowlistEntry(id="webex:kofi@example.com", tier="trusted"),
        ],
    )
    return AllowlistGate(allowlist, audit_sink=sink, identity=IDENTITY)


def test_owner_is_admitted_and_identity_resolved() -> None:
    sink = _ListSink()
    gate = _tiered_gate(sink)
    admitted = gate.admit(_message("webex", "nina@example.com"))
    assert admitted is not None
    assert admitted.sender.internal_user_id == "webex:nina@example.com"
    assert sink.records == [], "an admitted sender writes no gateway_reject"


def test_trusted_is_admitted_under_tiered() -> None:
    sink = _ListSink()
    admitted = _tiered_gate(sink).admit(_message("webex", "kofi@example.com"))
    assert admitted is not None
    assert admitted.sender.internal_user_id == "webex:kofi@example.com"
    assert sink.records == []


def test_unknown_sender_is_dropped_and_audited() -> None:
    sink = _ListSink()
    dropped = _tiered_gate(sink).admit(_message("webex", "stranger@example.com"))
    assert dropped is None, "an unknown sender produces nothing downstream"
    [record] = sink.records
    assert record.event == "gateway_reject"
    assert record.outcome.status == "denied"
    assert record.trigger.message_id is not None
    assert record.approval is None and record.cost is None
    assert "webex:stranger@example.com" in record.action.input_summary


def test_owner_only_policy_rejects_trusted_tier() -> None:
    sink = _ListSink()
    allowlist = GatewayAllowlist(
        policy="owner-only",
        roster=[
            AllowlistEntry(id="slack:U-owner", tier="owner"),
            AllowlistEntry(id="slack:U-helper", tier="trusted"),
        ],
    )
    gate = AllowlistGate(allowlist, audit_sink=sink, identity=IDENTITY)
    assert gate.admit(_message("slack", "U-owner")) is not None
    assert gate.admit(_message("slack", "U-helper")) is None, "owner-only drops trusted"
    [reject] = sink.records
    assert reject.event == "gateway_reject"
    assert "tier 'trusted'" in reject.action.input_summary


def test_principal_is_platform_scoped() -> None:
    # The same platform_id on two platforms is two distinct principals — a
    # webex roster never admits a dev-http sender of the same id.
    sink = _ListSink()
    gate = _tiered_gate(sink)
    assert gate.admit(_message("dev-http", "nina@example.com")) is None


# ------------------------------------------------- webex email case-normalization


def test_webex_email_roster_entry_is_case_normalized() -> None:
    # Emails are case-insensitive: a mixed-case roster entry must not silently
    # drop the legit lowercase sender the adapter produces.
    sink = _ListSink()
    allowlist = GatewayAllowlist(
        policy="tiered", roster=[AllowlistEntry(id="webex: Bob@Example.COM ", tier="owner")]
    )
    gate = AllowlistGate(allowlist, audit_sink=sink, identity=IDENTITY)
    admitted = gate.admit(_message("webex", "bob@example.com"))
    assert admitted is not None
    assert admitted.sender.internal_user_id == "webex:bob@example.com"
    assert sink.records == []


def test_webex_person_id_roster_entry_stays_case_sensitive() -> None:
    # personIds are opaque base64 (no '@' possible) — casing stays significant.
    sink = _ListSink()
    allowlist = GatewayAllowlist(
        policy="tiered", roster=[AllowlistEntry(id="webex:UGVSc09uLTE", tier="owner")]
    )
    gate = AllowlistGate(allowlist, audit_sink=sink, identity=IDENTITY)
    assert gate.admit(_message("webex", "ugvsc09ulte")) is None, "personId casing must match"
    assert gate.admit(_message("webex", "UGVSc09uLTE")) is not None


def test_non_webex_roster_entry_with_at_is_not_normalized() -> None:
    # The normalization is scoped to webex email entries only: a non-webex
    # platform id that happens to contain '@' keeps its exact casing.
    sink = _ListSink()
    allowlist = GatewayAllowlist(
        policy="tiered", roster=[AllowlistEntry(id="dev-http:Bob@Example.com", tier="owner")]
    )
    gate = AllowlistGate(allowlist, audit_sink=sink, identity=IDENTITY)
    assert gate.admit(_message("dev-http", "bob@example.com")) is None, "dev-http is untouched"
    assert gate.admit(_message("dev-http", "Bob@Example.com")) is not None


def test_colliding_normalized_roster_entries_refuse_to_construct() -> None:
    # #45: 'webex:Bob@x.com' and 'webex:bob@x.com' are ONE principal — a
    # last-wins roster would let roster ORDER silently decide the tier.
    # Fail-loud at construction instead, naming the colliding principal.
    allowlist = GatewayAllowlist(
        policy="tiered",
        roster=[
            AllowlistEntry(id="webex:Bob@x.com", tier="owner"),
            AllowlistEntry(id="webex:bob@x.com", tier="guest"),
        ],
    )
    with pytest.raises(ValueError, match="webex:bob@x.com"):
        AllowlistGate(allowlist, audit_sink=_ListSink(), identity=IDENTITY)


def test_exact_duplicate_roster_entries_are_the_same_collision() -> None:
    # #45: the same key twice (identical casing, no normalization involved)
    # is the same silent-last-wins hazard. Stage 19 (#58) moved the EXACT-
    # duplicate refusal up to the schema, so it now fails `foundry validate`
    # (pydantic construction), long before boot...
    with pytest.raises(ValueError, match="dev-http:owner-1"):
        GatewayAllowlist(
            policy="tiered",
            roster=[
                AllowlistEntry(id="dev-http:owner-1", tier="owner"),
                AllowlistEntry(id="dev-http:owner-1", tier="guest"),
            ],
        )
    # ...and the gate keeps its own refusal (defense in depth for rosters that
    # never crossed the schema — construct one bypassing validation to prove it).
    allowlist = GatewayAllowlist.model_construct(
        policy="tiered",
        roster=[
            AllowlistEntry(id="dev-http:owner-1", tier="owner"),
            AllowlistEntry(id="dev-http:owner-1", tier="guest"),
        ],
    )
    with pytest.raises(ValueError, match="dev-http:owner-1"):
        AllowlistGate(allowlist, audit_sink=_ListSink(), identity=IDENTITY)


def test_non_colliding_roster_constructs_unchanged() -> None:
    # Distinct principals — including ASCII case variants of DIFFERENT
    # local parts — construct exactly as before.
    sink = _ListSink()
    gate = _tiered_gate(sink)
    assert gate.admit(_message("webex", "nina@example.com")) is not None
    assert sink.records == []


def test_unicode_casefold_pair_does_not_merge() -> None:
    # #45: 'STRAßE'.casefold() == 'strasse' (Unicode FULL case folding) but
    # 'straße@x.com' and 'strasse@x.com' are formally DISTINCT SMTPUTF8 local
    # parts. ASCII-only lowercasing keeps them apart in both directions.
    sink = _ListSink()
    ascii_roster = GatewayAllowlist(
        policy="tiered", roster=[AllowlistEntry(id="webex:strasse@example.com", tier="owner")]
    )
    gate = AllowlistGate(ascii_roster, audit_sink=sink, identity=IDENTITY)
    assert gate.admit(_message("webex", "straße@example.com")) is None

    unicode_roster = GatewayAllowlist(
        policy="tiered", roster=[AllowlistEntry(id="webex:STRAßE@example.com", tier="owner")]
    )
    gate = AllowlistGate(unicode_roster, audit_sink=sink, identity=IDENTITY)
    assert gate.admit(_message("webex", "strasse@example.com")) is None
    # ASCII letters in the entry still lower; the ß survives untouched.
    assert gate.admit(_message("webex", "straße@example.com")) is not None


def test_casefold_distinct_roster_entries_are_not_a_collision() -> None:
    # The pair that merges under casefold() must be two ADMISSIBLE entries
    # under ASCII-only lowercasing — never a construction-time collision.
    allowlist = GatewayAllowlist(
        policy="tiered",
        roster=[
            AllowlistEntry(id="webex:strasse@example.com", tier="owner"),
            AllowlistEntry(id="webex:straße@example.com", tier="guest"),
        ],
    )
    gate = AllowlistGate(allowlist, audit_sink=_ListSink(), identity=IDENTITY)
    assert gate.admit(_message("webex", "strasse@example.com")) is not None
    assert gate.admit(_message("webex", "straße@example.com")) is not None


def test_unsupported_policy_refuses_to_construct() -> None:
    with pytest.raises(ValueError, match="pairing"):
        AllowlistGate(GatewayAllowlist(policy="pairing"), audit_sink=_ListSink(), identity=IDENTITY)


# --------------------------------------------------- dev-http end-to-end (no creds)


def _raw_post(sender_id: str) -> bytes:
    body = json.dumps({"text": "hello", "conversation_id": "c-1", "sender_id": sender_id}).encode()
    head = f"POST /message HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode("ascii")
    return head + body


def test_dev_http_drops_unrostered_sender_before_enqueue() -> None:
    """POST /message from an un-rostered sender ⇒ 403, audited, NOT enqueued."""
    sink = _ListSink()
    allowlist = GatewayAllowlist(
        policy="tiered", roster=[AllowlistEntry(id="dev-http:owner-1", tier="owner")]
    )
    gate = AllowlistGate(allowlist, audit_sink=sink, identity=IDENTITY)
    queue = _RecordingQueue()
    adapter = DevHttpAdapter(queue, gate=gate)  # type: ignore[arg-type]

    async def drive() -> tuple[int, dict[str, object]]:
        reader = asyncio.StreamReader()
        reader.feed_data(_raw_post("stranger"))
        reader.feed_eof()
        return await adapter._handle_request(reader)

    status, body = asyncio.run(drive())
    assert status == 403
    assert queue.items == [], "a dropped message never reaches the queue (nothing downstream)"
    [record] = sink.records
    assert record.event == "gateway_reject"
    assert "dev-http:stranger" in record.action.input_summary

"""Stage-23 unit honesty — no server needed.

Two halves, mirroring the stage-17 layout (test_sessions_retrieval_unit).
(1) The per-user session DEFINITION: one keying rule (sessions.session_key)
shared by every manager — the owner's thread follows the owner across a
platform's channels (the schema: 'per user (shared across channels)'), two
users in one channel never share, identities stay per-PLATFORM
(gateway.identityUnification: 'separate' is the only buildable value), and a
trigger-originated message (no principal) keys by channel identity. The
carried real tier honors the rule: the sqlite manager against a real file —
with restart survival.
(2) The sliding-window HISTORY strategy: a windowed-replay path in the prompt
assembler — the last maxTurns session turns render EXACTLY as the
full-transcript path renders them (same fencing, same trust handling; the
window truncates, never reformats), an absent window is byte-identical to
every stage before (digest pin), and the stored transcript is never trimmed.
"""

import asyncio
import copy
import json
from pathlib import Path

import pytest
import yaml

from agent_runtime.audit import AgentIdentity
from agent_runtime.components.dev_http import DevHttpAdapter
from agent_runtime.components.jsonl_audit import JsonlAuditSink
from agent_runtime.components.memory_queue import InProcessQueue
from agent_runtime.components.prompt_assembler import PromptAssembler
from agent_runtime.components.single_session import SingleSessionManager
from agent_runtime.components.sqlite_persistence import SqliteSessionManager
from agent_runtime.core import AgentCore, prompt_digest
from agent_runtime.messages import ChannelRef, ContentBlock, InternalMessage, Provenance, Sender
from agent_runtime.provider import AssembledPrompt, ProviderReply
from agent_runtime.queues import QueueItem
from agent_runtime.sessions import Session, session_key

REPO_ROOT = Path(__file__).parents[3]
SKELETON_SPEC = REPO_ROOT / "examples" / "skeleton.yaml"

IDENTITY = AgentIdentity(slug="window-unit", spec_version="0.1.0", image_digest="sha256:test")


def _message(
    text: str,
    conversation_id: str = "room-a",
    sender_id: str = "kofi",
    platform: str = "dev-http",
) -> InternalMessage:
    return InternalMessage(
        channel=ChannelRef(platform=platform, conversation_id=conversation_id),
        sender=Sender(kind="human", platform_id=sender_id, verified=False),
        content=[ContentBlock(type="text", text=text)],
        provenance=Provenance(adapter="unit-test", trust="untrusted"),
    )


def _system_message(conversation_id: str = "event-subscription:alertmanager") -> InternalMessage:
    """A trigger-originated message: `kind: system`, NO platform principal."""
    return InternalMessage(
        channel=ChannelRef(platform="system", conversation_id=conversation_id),
        sender=Sender(kind="system", platform_id=None, verified=True),
        content=[ContentBlock(type="text", text="Triage this alarm.")],
        provenance=Provenance(adapter="unit-test", trust="untrusted"),
    )


def _normalized(
    text: str, conversation_id: str = "c-1", sender_id: str = "kofi"
) -> InternalMessage:
    adapter = DevHttpAdapter(InProcessQueue())
    message: InternalMessage = adapter.normalize(
        {"text": text, "conversation_id": conversation_id, "sender_id": sender_id}
    )
    return message


# ------------------------------------------------ per-user session definition


def test_per_user_shares_across_channels_and_isolates_users() -> None:
    """The owner's thread follows the owner: one user in two channels of one
    platform is ONE session; two users in one channel never share."""
    manager = SingleSessionManager(definition="per-user")
    room_a = manager.session_for(_message("follow up with acme", "room-a", sender_id="kofi"))
    room_b = manager.session_for(_message("any updates?", "room-b", sender_id="kofi"))
    assert room_a is room_b  # one user, two channels, ONE session
    assert room_a.session_id == "user:dev-http:kofi"
    mara = manager.session_for(_message("hello", "room-a", sender_id="mara"))
    assert mara is not room_a  # two users in one channel never share
    assert mara.session_id == "user:dev-http:mara"


def test_per_user_key_is_platform_scoped() -> None:
    """identityUnification 'separate' (the only buildable value): the same
    opaque platform_id on two platforms is two people — per-PLATFORM-user,
    never unified identity."""
    dev = _message("hi", "room-1", sender_id="kofi", platform="dev-http")
    webex = _message("hi", "room-1", sender_id="kofi", platform="webex")
    assert session_key("per-user", dev) != session_key("per-user", webex)
    assert session_key("per-user", dev) == "user:dev-http:kofi"
    assert session_key("per-user", webex) == "user:webex:kofi"


def test_per_user_system_sender_keys_by_channel_identity() -> None:
    """A trigger-originated message has no owning user (platform_id null) —
    it keys by channel identity, so each synthetic trigger conversation is
    its own thread rather than a crash or a shared bucket."""
    message = _system_message()
    assert session_key("per-user", message) == "channel:system:event-subscription:alertmanager"
    assert session_key("per-user", message) == session_key("per-channel", message)


def test_per_user_and_per_channel_key_spaces_never_collide() -> None:
    """A user id equal to a conversation id must not merge the two
    definitions' sessions (distinct prefixes)."""
    message = _message("hi", conversation_id="kofi", sender_id="kofi")
    assert session_key("per-user", message) != session_key("per-channel", message)


def test_sqlite_manager_keys_per_user_and_survives_restart(tmp_path: Path) -> None:
    """The sqlite tier honors the rule against a REAL file, and a fresh
    manager on the same file (a new process life) reloads each user's own
    turns — cross-channel accumulation included."""
    path = str(tmp_path / "sessions.sqlite3")
    writer = SqliteSessionManager(path, definition="per-user")
    kofi = writer.session_for(_message("follow up with acme", "room-a", sender_id="kofi"))
    kofi.add_message(_message("follow up with acme", "room-a", sender_id="kofi"))
    kofi.add_reply("noted")
    # the SAME user in ANOTHER channel lands in the SAME session
    same = writer.session_for(_message("any acme news?", "room-b", sender_id="kofi"))
    assert same is kofi
    same.add_message(_message("any acme news?", "room-b", sender_id="kofi"))
    # another user is another conversation
    mara = writer.session_for(_message("hello", "room-a", sender_id="mara"))
    assert mara is not kofi
    mara.add_message(_message("hello", "room-a", sender_id="mara"))
    assert len(kofi.turns) == 3 and len(mara.turns) == 1
    writer.close()

    reborn = SqliteSessionManager(path, definition="per-user")
    kofi_again = reborn.session_for(_message("probe", "room-c", sender_id="kofi"))
    mara_again = reborn.session_for(_message("probe", "room-c", sender_id="mara"))
    assert [t.text for t in kofi_again.turns] == [
        "follow up with acme",
        "noted",
        "any acme news?",
    ]
    assert [t.text for t in mara_again.turns] == ["hello"]
    assert kofi_again.session_id == "user:dev-http:kofi"
    reborn.close()


# --------------------------------------------- sliding-window history strategy


def _transcript_session(turn_count: int) -> Session:
    """A session with `turn_count` alternating user/assistant turns whose
    texts are distinct and ordered (turn-1 ... turn-N)."""
    session = Session(session_id="s")
    for i in range(1, turn_count + 1):
        if i % 2 == 1:
            session.add_message(_normalized(f"turn-{i}"))
        else:
            session.add_reply(f"turn-{i}")
    return session


def test_sliding_window_replays_exactly_the_last_n_turns_verbatim() -> None:
    """N+3 stored turns, window N: the assembled prompt carries EXACTLY the
    last N turns, each rendered byte-identically to the full-transcript
    path's rendering — the window truncates, never reformats — and the
    oldest 3 turns are absent."""
    n = 5
    session = _transcript_session(n + 3)
    full = PromptAssembler().assemble("persona text", session)
    windowed = PromptAssembler(window_turns=n).assemble("persona text", session)
    assert len(windowed.messages) == n
    assert windowed.messages == full.messages[-n:]  # verbatim, same rendering
    assert windowed.system == full.system == "persona text"
    for old in ("turn-1", "turn-2", "turn-3"):
        assert not any(old in m.text for m in windowed.messages), f"{old} leaked past the window"
    # the stored transcript is NEVER trimmed — the window is prompt-only
    assert len(session.turns) == n + 3


def test_sliding_window_keeps_fencing_and_trust_handling() -> None:
    """Windowed turns keep the full path's trust handling: untrusted user
    turns stay fenced, the agent's own (operator-trust) replies stay unfenced."""
    session = _transcript_session(6)
    windowed = PromptAssembler(window_turns=4).assemble("persona text", session)
    user_turns = [m for m in windowed.messages if m.role == "user"]
    assistant_turns = [m for m in windowed.messages if m.role == "assistant"]
    assert user_turns and assistant_turns
    assert all("UNTRUSTED CONTENT" in m.text for m in user_turns)
    assert all("UNTRUSTED" not in m.text for m in assistant_turns)


def test_sliding_window_larger_than_the_transcript_replays_everything() -> None:
    """A window wider than the transcript is the full transcript — no padding,
    no reformatting."""
    session = _transcript_session(3)
    full = PromptAssembler().assemble("persona text", session)
    windowed = PromptAssembler(window_turns=50).assemble("persona text", session)
    assert windowed == full


def test_absent_window_is_byte_identical_digest_pin() -> None:
    """Kill-switch (the stage-17 digest-pin pattern): no window wired
    (window_turns=None, the default) leaves the assembled prompt EXACTLY as
    before this stage — full-transcript replay, same digest."""
    session = _transcript_session(4)
    bare = PromptAssembler().assemble("persona text", session)
    explicit_none = PromptAssembler(window_turns=None).assemble("persona text", session)
    assert len(bare.messages) == 4
    assert bare == explicit_none
    assert prompt_digest(bare) == prompt_digest(explicit_none)


@pytest.mark.parametrize("bad", [0, -1, -50])
def test_window_turns_below_one_is_refused(bad: int) -> None:
    """#79: PromptAssembler(window_turns=0) would slice `turns[-0:]` — the WHOLE
    list — silently replaying the full transcript, the OPPOSITE of a 0-turn
    window. The __init__ guard refuses any window_turns < 1 (None stays valid
    for full replay). Spec-unreachable (maxTurns ge=1) but a direct-construction
    footgun."""
    with pytest.raises(ValueError, match=r"window_turns must be >= 1"):
        PromptAssembler(window_turns=bad)


def test_retrieval_strategy_still_collapses_when_a_window_is_wired() -> None:
    """Defense in depth: the schema admits ONE history strategy, so a window
    and a retrieval result cannot co-occur through the spec — but if they
    ever did, retrieval semantics own the context window (current message
    only), exactly as in stage 17."""
    session = _transcript_session(5)
    prompt = PromptAssembler(window_turns=3).assemble(
        "persona text", session, retrieved_history=["[user] turn-1"]
    )
    [current] = prompt.messages
    assert "turn-5" in current.text


def test_core_end_to_end_windowed_prompt(tmp_path: Path) -> None:
    """The REAL core loop with a windowed assembler: prompts grow with the
    transcript until the window caps them, the provider sees the last-N turns
    verbatim, and the session transcript keeps every turn."""
    captured: list[AssembledPrompt] = []

    class CapturingProvider:
        name = "capturing"

        async def complete(self, prompt: AssembledPrompt) -> ProviderReply:
            captured.append(prompt)
            return ProviderReply(text=f"reply-{len(captured)}", tokens_in=1, tokens_out=1)

    async def run() -> None:
        core = AgentCore(
            identity=IDENTITY,
            persona_identity="persona",
            queue=InProcessQueue(),
            sessions=SingleSessionManager(definition="per-user"),
            assembler=PromptAssembler(window_turns=4),
            provider=CapturingProvider(),
            audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        )
        for i in range(1, 4):
            item = QueueItem(
                message=_normalized(f"message-{i}"),
                reply=asyncio.get_running_loop().create_future(),
            )
            await core.handle(item)
            await item.reply

    asyncio.run(run())
    # transcript at assembly time: 1, 3, then 5 turns -> capped at 4
    assert [len(p.messages) for p in captured] == [1, 3, 4]
    last = captured[-1]
    # the last 4 turns verbatim: reply-1, message-2, reply-2, message-3
    assert "message-1" not in json.dumps([m.text for m in last.messages])
    assert [m.role for m in last.messages] == ["assistant", "user", "assistant", "user"]
    assert "message-3" in last.messages[-1].text


def test_build_app_wires_per_user_and_the_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boot-level honesty: build_app on a sliding-window + per-user spec (the
    client-tracking session block, sqlite tier) wires the window into the
    assembler and the definition into the manager — audited prompt sizes cap
    at maxTurns while the transcript file keeps every turn under ONE
    user-keyed session across two channels."""
    from agent_runtime.runner import build_app
    from keep_spec import validate_spec_data

    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    data = copy.deepcopy(data)
    data["spec"]["sessions"] = {
        "mode": "single",
        "definition": "per-user",
        "history": {"strategy": "sliding-window", "maxTurns": 2},
    }
    audit_path = tmp_path / "audit.jsonl"
    data["spec"]["observability"]["audit"]["path"] = str(audit_path)
    sqlite_path = tmp_path / "sessions.sqlite3"
    monkeypatch.setenv("SQLITE_PATH", str(sqlite_path))
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", "sha256:" + "0" * 64)
    core, _adapter = build_app(validate_spec_data(data))

    async def run() -> None:
        for i, conversation in enumerate(["room-a", "room-b", "room-a"], start=1):
            item = QueueItem(
                message=_normalized(f"message-{i}", conversation, sender_id="kofi"),
                reply=asyncio.get_running_loop().create_future(),
            )
            await core.handle(item)
            await item.reply

    asyncio.run(run())
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    summaries = [r["action"]["input_summary"] for r in records if r["event"] == "model_call"]
    # transcript at assembly: 1 turn, then 3 -> capped at 2, then 5 -> 2
    assert [s.split(" message(s)")[0] for s in summaries] == [
        "assembled prompt: 1",
        "assembled prompt: 2",
        "assembled prompt: 2",
    ]
    # the window never trimmed STORAGE, and both channels fed ONE user session
    manager = SqliteSessionManager(str(sqlite_path), definition="per-user")
    reloaded = manager.session_for(_normalized("probe", "room-z", sender_id="kofi"))
    assert reloaded.session_id == "user:dev-http:kofi"
    assert len(reloaded.turns) == 6  # 3 user + 3 assistant, nothing dropped
    manager.close()

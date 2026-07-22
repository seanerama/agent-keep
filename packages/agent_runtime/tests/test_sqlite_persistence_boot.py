"""Sqlite persistence honesty — a real FILE, no server needed.

`persistence.tier: sqlite` is a durability DECLARATION. Before stage 20 it was
vacuous (issue #59): the guard admitted it while sessions lived in-process, so
the headline test below — write turns, DISCARD the manager, reopen the same
file with a fresh one, read everything back — was IMPOSSIBLE. Now it must
pass. The refusal postures mirror the postgres tier translated to file
reality: missing SQLITE_PATH or an unopenable file fails construction loudly
(never an in-memory or default-path fallback), errors echo only the file's
base name (a full path can embed user info), WAL + a bounded pinned
busy_timeout are pinned, and a failed turn write never diverges the in-memory
session from the file.
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_runtime.components.sqlite_persistence import (
    BUSY_TIMEOUT_MILLIS,
    SqlitePersistenceUnavailableError,
    SqliteSessionManager,
    _redacted_path,
)
from agent_runtime.messages import ChannelRef, ContentBlock, InternalMessage, Provenance, Sender
from keep_spec import AgentSpec, validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
SKELETON_SPEC = REPO_ROOT / "examples" / "skeleton.yaml"
VALID_DIGEST = "sha256:" + "ab" * 32


def _message(
    text: str, conversation_id: str = "unit", platform: str = "dev-http"
) -> InternalMessage:
    return InternalMessage(
        channel=ChannelRef(platform=platform, conversation_id=conversation_id),
        sender=Sender(kind="human", platform_id="unit", verified=False),
        content=[ContentBlock(type="text", text=text)],
        provenance=Provenance(adapter="unit-test", trust="untrusted"),
    )


def _skeleton_spec(tmp_path: Path) -> AgentSpec:
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data["spec"]["observability"]["audit"]["path"] = str(tmp_path / "audit.jsonl")
    return validate_spec_data(data)


# ------------------------------------------------------------ loud refusals


def test_missing_sqlite_path_fails_loudly() -> None:
    with pytest.raises(SqlitePersistenceUnavailableError, match="SQLITE_PATH is not set"):
        SqliteSessionManager("")


def test_unopenable_file_fails_loudly_without_echoing_the_full_path(tmp_path: Path) -> None:
    """Parent directory absent -> refuse at construction. The error names the
    problem and the base name but never the full path (it can embed user info
    — e.g. this tmp_path's /home/<user>/ prefix)."""
    path = tmp_path / "no-such-dir" / "sessions.sqlite3"
    with pytest.raises(SqlitePersistenceUnavailableError) as excinfo:
        SqliteSessionManager(str(path))
    message = str(excinfo.value)
    assert "cannot be opened" in message
    assert "SQLITE_PATH" in message  # the operator knows WHERE to fix it
    assert "sessions.sqlite3" in message  # ...and which file it meant
    assert str(tmp_path) not in message  # the user-info-bearing prefix, withheld


def test_readonly_database_file_fails_loudly_without_echoing_the_full_path(
    tmp_path: Path,
) -> None:
    """A pre-existing READ-ONLY database file (still in the default rollback
    journal mode — e.g. hand-created, or restored from a backup tool) ->
    refuse at construction with the component's own error, not a raw
    sqlite3.OperationalError leaking out of `PRAGMA journal_mode = WAL` (the
    first statement that must WRITE such a file, to flip its header). Same
    redaction posture as the unopenable case: base name yes, full
    user-info-bearing path no."""
    path = tmp_path / "sessions.sqlite3"
    sqlite3.connect(path).close()  # a valid NON-WAL database file exists...
    path.chmod(0o444)  # ...but is read-only underneath the deploy
    try:
        with pytest.raises(SqlitePersistenceUnavailableError) as excinfo:
            SqliteSessionManager(str(path))
    finally:
        path.chmod(0o644)
    message = str(excinfo.value)
    assert "SQLITE_PATH" in message  # the operator knows WHERE to fix it
    assert "sessions.sqlite3" in message  # ...and which file it meant
    assert str(tmp_path) not in message  # the user-info-bearing prefix, withheld


def test_missing_env_refuses_build_app_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runner reads SQLITE_PATH unconditionally at build_app (the
    POSTGRES_DSN pattern): missing env -> refuse to start, naming the
    variable. This is THE behavior change for sqlite-tier specs — before
    stage 20 this boot silently proceeded with ephemeral in-process sessions."""
    from agent_runtime.runner import build_app

    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    with pytest.raises(SqlitePersistenceUnavailableError, match="SQLITE_PATH is not set"):
        build_app(_skeleton_spec(tmp_path))


def test_build_app_wires_the_sqlite_manager_for_the_skeleton(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The skeleton (tier: sqlite) now boots onto the FILE-backed manager —
    never the in-process one (no buildable spec gets memory-only sessions)."""
    from agent_runtime.runner import build_app

    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    core, _adapter = build_app(_skeleton_spec(tmp_path))
    assert isinstance(core._sessions, SqliteSessionManager)


# ------------------------------------------------- pinned pragmas (WAL etc.)


def test_wal_and_bounded_busy_timeout_are_pinned(tmp_path: Path) -> None:
    """The durability claim rests on these: WAL journal mode (a concurrent
    reader cannot wedge the loop) and a bounded, PINNED busy_timeout (a held
    lock fails loudly after the bound, never an unbounded synchronous wait)."""
    manager = SqliteSessionManager(str(tmp_path / "s.sqlite3"))
    assert manager._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert manager._conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MILLIS
    assert manager._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert BUSY_TIMEOUT_MILLIS == 5000  # pinned constant, bounded
    manager.close()


# -------------------------------------- round-trip + THE restart-survival test


def test_restart_survival_on_the_same_file(tmp_path: Path) -> None:
    """THE test stage 20 exists for — impossible before it: write turns,
    DISCARD the manager entirely, reopen the SAME file with a fresh one, and
    every turn reads back in order. The in-process tier this replaces returned
    a blank session here."""
    path = str(tmp_path / "sessions.sqlite3")

    writer = SqliteSessionManager(path)
    session = writer.session_for(_message("hello, durable one"))
    session.add_message(_message("hello, durable one"))
    session.add_reply("noted, human")
    session.add_message(_message("second message"))
    written = [(t.role, t.text, t.trust, t.platform) for t in session.turns]
    writer.close()
    del writer, session  # the process life that wrote is gone

    reader = SqliteSessionManager(path)
    reloaded = reader.session_for(_message("ignored probe"))
    read_back = [(t.role, t.text, t.trust, t.platform) for t in reloaded.turns]
    assert read_back == written
    assert [t.role for t in reloaded.turns] == ["user", "assistant", "user"]
    reader.close()


def test_turn_ids_and_timestamps_are_stable(tmp_path: Path) -> None:
    """Mechanic design constraint (same as the postgres tier): every turn has
    a UUID id and a timestamp, and two reads see the SAME ids in the same
    order — the stored shape is addressable."""
    path = str(tmp_path / "sessions.sqlite3")
    manager = SqliteSessionManager(path)
    session = manager.session_for(_message("hi"))
    session.add_message(_message("hi"))
    session.add_reply("hello")
    manager.close()

    with sqlite3.connect(path) as conn:
        first = conn.execute(
            "SELECT turn_id, recorded_at FROM foundry_transcript_turns ORDER BY seq"
        ).fetchall()
        second = conn.execute(
            "SELECT turn_id, recorded_at FROM foundry_transcript_turns ORDER BY seq"
        ).fetchall()
    assert len(first) == 2
    assert all(turn_id and recorded_at for turn_id, recorded_at in first)
    assert first == second


def test_reboot_on_existing_file_is_an_additive_no_op(tmp_path: Path) -> None:
    """CREATE IF NOT EXISTS only: a second boot against an existing file
    changes nothing and loses nothing (no destructive migration)."""
    path = str(tmp_path / "sessions.sqlite3")
    first = SqliteSessionManager(path)
    first.session_for(_message("x")).add_reply("kept")
    first.close()

    second = SqliteSessionManager(path)  # re-runs the schema statements
    assert [t.text for t in second.session_for(_message("probe")).turns] == ["kept"]
    second.close()


# ------------------------------------------------------- per-channel keying


def test_per_channel_definition_isolates_channels_and_shares_within(tmp_path: Path) -> None:
    """Stage 17 on the sqlite tier: two channels never share a session, two
    senders in one channel do — and the keying survives a restart."""
    path = str(tmp_path / "sessions.sqlite3")
    manager = SqliteSessionManager(path, definition="per-channel")
    noc = manager.session_for(_message("alarm", conversation_id="noc-room"))
    noc.add_reply("ack noc")
    other = manager.session_for(_message("hi", conversation_id="water-cooler"))
    assert other.turns == []  # isolated
    again = manager.session_for(_message("update", conversation_id="noc-room"))
    assert again is noc  # shared within the channel
    manager.close()

    reopened = SqliteSessionManager(path, definition="per-channel")
    noc_reloaded = reopened.session_for(_message("p", conversation_id="noc-room"))
    assert [t.text for t in noc_reloaded.turns] == ["ack noc"]
    assert reopened.session_for(_message("p", conversation_id="water-cooler")).turns == []
    reopened.close()


def test_absent_definition_keeps_the_one_single_session(tmp_path: Path) -> None:
    """No definition = the skeleton's one shared session (id 'single'),
    exactly as the seam behaved before — the stage-17 kill-switch holds."""
    manager = SqliteSessionManager(str(tmp_path / "s.sqlite3"))
    a = manager.session_for(_message("a", conversation_id="one"))
    b = manager.session_for(_message("b", conversation_id="two"))
    assert a is b
    assert a.session_id == "single"
    manager.close()


# ------------------------------------------------ divergence pop on failure


def test_failed_turn_write_never_diverges_memory_from_file(tmp_path: Path) -> None:
    """If the INSERT fails, the in-memory turn is rolled back with the error
    (stage-15 finding-4 posture): a memory-only turn would feed future
    prompts, vanish on restart, and double-append on a caller retry. The
    failure here is REAL — the connection is flipped read-only underneath the
    manager, the exact shape of a filesystem gone read-only at run time."""
    manager = SqliteSessionManager(str(tmp_path / "s.sqlite3"))
    session = manager.session_for(_message("hello"))
    manager._conn.execute("PRAGMA query_only = ON")

    with pytest.raises(sqlite3.OperationalError):
        session.add_message(_message("hello"))
    assert session.turns == [], "failed add_message left a memory-only turn"

    with pytest.raises(sqlite3.OperationalError):
        session.add_reply("lost reply")
    assert session.turns == [], "failed add_reply left a memory-only turn"
    manager.close()


# ----------------------------------------------------------- path redaction


def test_redacted_path_keeps_only_the_base_name() -> None:
    assert _redacted_path("/home/someuser/agents/sessions.sqlite3") == ".../sessions.sqlite3"
    assert "someuser" not in _redacted_path("/home/someuser/agents/sessions.sqlite3")

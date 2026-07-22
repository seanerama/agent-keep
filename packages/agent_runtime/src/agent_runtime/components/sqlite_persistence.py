"""Sqlite persistence tier — sessions + transcripts on a real FILE
(`persistence.tier: sqlite`).

This component sits behind the EXISTING persistence seam (agent_runtime.
sessions): it is a `SessionManager` whose `session_for` returns a `Session`
that WRITES THROUGH to a sqlite database file — every turn
`Session.add_message` / `Session.add_reply` appends is durably recorded before
the call returns, and a fresh process life on the SAME FILE reads the same
turns back in order. The seam itself is unchanged (one interface, swappable):
the core, the prompt assembler, and the postgres tier are untouched by this
component's existence. Before stage 20 the sqlite tier was vacuous — the guard
admitted it while sessions lived in-process (issue #59, a durability lie);
this component makes the declaration true.

Schema (additive only — CREATE IF NOT EXISTS, no destructive change): the
same shapes as the postgres tier — `foundry_sessions` (one row per session)
and `foundry_transcript_turns` (append-only turn log; `seq` orders, `turn_id`
is a stable UUID, `recorded_at` a database-side UTC timestamp).

Mechanic note (vision v0.2): the transcripts stored here are a future
Mechanic READ SOURCE — the observer layer's log-egress contract will address
individual turns, which is why every turn carries a stable `turn_id` and a
`recorded_at` timestamp from day one, exactly like the postgres tier. No
reader, exporter, or egress code exists in this component (or anywhere) yet;
this stage only guarantees the stored shape is addressable when that contract
arrives.

Connection config: `SQLITE_PATH` (a NAME per the spec's env conventions,
mirroring `POSTGRES_DSN`/`REDIS_URL` — the value is deploy-time only, never in
spec or image). If the spec selects `persistence.tier: sqlite` and the path is
missing or the file cannot be opened at boot, construction raises
SqlitePersistenceUnavailableError — a LOUD failure by design (the stage-5
sentinel posture). There is deliberately NO in-memory and NO default-path
fallback: a silently ephemeral session store is the exact lie this stage
removes.

Redaction posture, proportional (mirrors the postgres tier's DSN discipline):
the path is deploy config, not a credential, but a full filesystem path can
embed user info (e.g. a `/home/<user>/...` prefix), so error messages echo
only the file's base name — the operator finds the full value where they set
it, under the SQLITE_PATH name in their deploy config.

Failure posture after boot: the database is a LOCAL file, so the postgres
tier's network-liveness machinery (TCP keepalives, tcp_user_timeout,
statement_timeout, reconnect-once) has no analog here and is deliberately
dropped. What replaces it: `PRAGMA journal_mode=WAL` plus a bounded, pinned
`busy_timeout` (BUSY_TIMEOUT_MILLIS), so a concurrent reader of the file (a
backup, an operator's sqlite3 shell) can never wedge the event loop for more
than the pinned bound — a still-locked database surfaces as a loud
sqlite3.OperationalError, never a hang. And a failed turn write never diverges
memory from the database: the in-memory turn is rolled back along with the
error, so the session the assembler reads only ever contains turns the
database durably holds (stage-15 finding-4 posture).
"""

import logging
import sqlite3
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

from agent_runtime.facts import FactsBackend
from agent_runtime.messages import InternalMessage
from agent_runtime.sessions import Session, Turn, session_key

logger = logging.getLogger(__name__)

#: Bounded, PINNED wait on a concurrently-held file lock: after this many
#: milliseconds a still-locked database fails the statement loudly
#: (sqlite3.OperationalError: database is locked) instead of wedging the event
#: loop — the seam is synchronous, so an unbounded wait would freeze the agent.
BUSY_TIMEOUT_MILLIS = 5000

#: Additive schema — every statement is CREATE IF NOT EXISTS (acceptance
#: condition: no destructive change; re-running against an existing file is a
#: no-op). Same shapes as the postgres tier: stable turn_id + recorded_at on
#: every turn (the Mechanic constraint applies identically), seq orders.
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS foundry_sessions (
        session_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS foundry_transcript_turns (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL REFERENCES foundry_sessions (session_id),
        role TEXT NOT NULL,
        text TEXT NOT NULL,
        trust TEXT NOT NULL,
        platform TEXT NOT NULL,
        message_id TEXT,
        recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS foundry_transcript_turns_session_idx
        ON foundry_transcript_turns (session_id, seq)
    """,
)

#: Additive facts schema (stage 24) — the facts memory structure lives on the
#: persistence tier, so its table rides the SAME file as the transcript. Same
#: shape as the postgres tier: a stable UUID `fact_id`, tier-side timestamps,
#: unique keys per agent (the upsert target). CREATE IF NOT EXISTS: re-running
#: against an existing file is a no-op; no destructive change.
_FACTS_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS foundry_facts (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_id TEXT NOT NULL UNIQUE,
        agent_slug TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (agent_slug, key)
    )
    """,
)


class SqlitePersistenceUnavailableError(RuntimeError):
    """`persistence.tier: sqlite` was selected but the database file cannot be
    used — refuse to run (no silent fallback to an in-memory session store)."""


def _redacted_path(path: str) -> str:
    """Only the file's base name, safe for logs and errors.

    A full path may embed user info (a `/home/<user>/...` prefix); the value
    itself lives under the SQLITE_PATH name in the operator's deploy config."""
    return f".../{Path(path).name}"


class _WriteThroughSession(Session):
    """A Session whose appended turns are durably recorded before returning.

    The seam's callers (AgentCore) keep using the exact `Session` surface —
    `turns`, `add_message`, `add_reply` — and never see the store."""

    def __init__(self, session_id: str, turns: list[Turn], store: "SqliteSessionManager"):
        super().__init__(session_id=session_id, turns=turns)
        self._store = store

    def add_message(self, message: InternalMessage) -> None:
        super().add_message(message)
        self._record_last_turn()

    def add_reply(self, text: str) -> None:
        super().add_reply(text)
        self._record_last_turn()

    def _record_last_turn(self) -> None:
        """Persist the just-appended turn — or ROLL IT BACK from memory.

        If the INSERT fails, the in-memory turn must not survive: a turn that
        exists only in memory would feed future prompts, vanish on restart,
        and double-append if the caller retries — memory and database must
        never diverge (stage-15 finding-4 posture, mirrored)."""
        try:
            self._store.record_turn(self.session_id, self.turns[-1])
        except BaseException:
            self.turns.pop()
            raise


class SqliteSessionManager:
    """SessionManager over a sqlite file (see module docstring).

    Serves the single-session mode (`sessions.mode: single` — the only mode
    this library implements) with every session's transcript persisted: a
    restart on the same file reloads the full turn history instead of starting
    blank. `definition` rides in exactly as on the postgres tier (stage 17):
    absent keeps the one 'single' session; 'per-channel' keys sessions by
    channel identity; 'per-user' (stage 23) by the sender's per-platform
    principal (the keying rule is sessions.session_key — shared across
    tiers so flipping persistence.tier never re-cuts conversations).

    For local/unit use the constructor takes the explicit file path; only the
    runner wiring reads the SQLITE_PATH env var (the POSTGRES_DSN pattern).
    """

    def __init__(self, path: str, *, definition: str | None = None) -> None:
        self._definition = definition
        if not path:
            raise SqlitePersistenceUnavailableError(
                "spec selects persistence.tier 'sqlite' but SQLITE_PATH is not set. "
                "Provide SQLITE_PATH at deploy time — a writable database file path, "
                "e.g. on a mounted volume (value never lives in spec or image). There "
                "is no in-memory or default-path fallback: the spec declares a durable "
                "persistence tier; refusing to boot without one."
            )
        self._path = path
        try:
            # autocommit: every recorded turn is durable the moment the INSERT
            # returns — a crash mid-conversation loses nothing already written
            # (the postgres tier's autocommit posture, translated).
            self._conn = sqlite3.connect(path, autocommit=True)
        except sqlite3.Error as exc:
            raise SqlitePersistenceUnavailableError(
                f"spec selects persistence.tier 'sqlite' but the database file at "
                f"{_redacted_path(path)} cannot be opened: {exc}. Check the SQLITE_PATH "
                "value in the deploy config (the parent directory must exist and be "
                "writable). Refusing to boot — no silent fallback to an in-memory store."
            ) from exc
        try:
            self._configure_connection()
            self._ensure_schema()
        except sqlite3.Error as exc:
            # A raw sqlite3 error must never escape boot: e.g. a pre-existing
            # READ-ONLY database file makes `PRAGMA journal_mode = WAL` (the
            # first statement that must WRITE such a file) raise
            # OperationalError directly, and a read-only or corrupt file can
            # fail the schema statements the same way. Same loud refusal, same
            # path redaction as the open failure above.
            raise SqlitePersistenceUnavailableError(
                f"spec selects persistence.tier 'sqlite' but the database file at "
                f"{_redacted_path(path)} cannot be prepared (WAL/busy_timeout pragmas "
                f"+ additive schema): {exc}. Check the SQLITE_PATH value in the deploy "
                "config (the file must be writable by the agent). Refusing to boot — "
                "no silent fallback to an in-memory store."
            ) from exc
        self._sessions: dict[str, _WriteThroughSession] = {}

    # ------------------------------------------------------------------ boot

    def _configure_connection(self) -> None:
        """Pin the pragmas the durability claim rests on — refuse if they
        cannot hold (a filesystem that cannot take WAL cannot honor the tier).
        """
        self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLIS}")
        # Referential integrity is ON in postgres by construction; sqlite
        # needs it requested per-connection.
        self._conn.execute("PRAGMA foreign_keys = ON")
        # FULL sync in WAL mode fsyncs at every commit: "durably recorded
        # before the call returns" is a physical claim, not a cache's promise.
        self._conn.execute("PRAGMA synchronous = FULL")
        # A read-only file makes this pragma RAISE (it must write the header);
        # __init__ wraps that into the same refusal. The check below covers
        # the quieter failure: the pragma succeeds but returns a non-WAL mode
        # (e.g. a filesystem without shared-memory support).
        row = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
        mode = cast(str, row[0]) if row is not None else "<none>"
        if mode.lower() != "wal":
            raise SqlitePersistenceUnavailableError(
                f"spec selects persistence.tier 'sqlite' but the database file at "
                f"{_redacted_path(self._path)} cannot enter WAL journal mode (got "
                f"{mode!r}) — typically a filesystem without shared-memory support. "
                "Refusing to boot: without WAL a concurrent reader could wedge the "
                "event loop."
            )

    def _ensure_schema(self) -> None:
        """Create the schema (additive-only CREATE IF NOT EXISTS).

        Deliberately NO advisory-lock analog of the postgres mirror: sqlite's
        single-writer FILE lock plus the pinned busy_timeout already
        serializes concurrent first boots' schema creation — a second booter
        waits on the file lock, then finds every object present (a no-op).
        Postgres needed the advisory lock only because two servers-side
        catalog writers can race; a single file cannot."""
        for statement in _SCHEMA_STATEMENTS:
            self._conn.execute(statement)

    # ------------------------------------------------------------- interface

    def session_for(self, message: InternalMessage) -> Session:
        """The message's session (per the wired definition), its turn history
        loaded from the file."""
        session_id = session_key(self._definition, message)
        cached = self._sessions.get(session_id)
        if cached is not None:
            return cached
        self._conn.execute(
            "INSERT INTO foundry_sessions (session_id) VALUES (?) ON CONFLICT DO NOTHING",
            (session_id,),
        )
        session = _WriteThroughSession(session_id, self._load_turns(session_id), self)
        self._sessions[session_id] = session
        return session

    def record_turn(self, session_id: str, turn: Turn) -> None:
        """Durably append one turn — stable UUID + database-side timestamp
        (the future Mechanic log-egress contract addresses turns by these)."""
        self._conn.execute(
            "INSERT INTO foundry_transcript_turns "
            "(turn_id, session_id, role, text, trust, platform, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                session_id,
                turn.role,
                turn.text,
                turn.trust,
                turn.platform,
                turn.message_id,
            ),
        )

    def facts_backend(self) -> FactsBackend:
        """The facts-table seam on THIS tier's connection (stage 24): the facts
        memory component reuses the same file + busy_timeout/WAL posture as the
        transcript, without a line of it duplicated (see agent_runtime.facts)."""
        return _SqliteFactsBackend(self)

    def close(self) -> None:
        self._conn.close()

    # -------------------------------------------------------------- plumbing

    def _load_turns(self, session_id: str) -> list[Turn]:
        rows: list[tuple[Any, ...]] = self._conn.execute(
            "SELECT role, text, trust, platform, message_id "
            "FROM foundry_transcript_turns WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [
            Turn(
                role=cast(Literal["user", "assistant"], row[0]),
                text=row[1],
                trust=row[2],
                platform=row[3],
                message_id=row[4],
            )
            for row in rows
        ]


class _SqliteFactsBackend:
    """`FactsBackend` over the sqlite tier's connection (stage 24).

    Runs facts statements on the SAME sqlite connection as the transcript, so
    they inherit the tier's WAL + busy_timeout posture (a concurrent reader can
    never wedge the loop past the pinned bound). The facts component owns every
    statement's text; this backend only names the dialect and runs it — no
    connection/liveness code is duplicated here."""

    placeholder = "?"
    #: UTC ISO-8601 with millis, matching the tier's transcript timestamps.
    now_sql = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

    def __init__(self, manager: SqliteSessionManager) -> None:
        self._manager = manager

    def ensure_facts_schema(self) -> None:
        # sqlite's single-writer file lock + busy_timeout serializes concurrent
        # first boots exactly as it does the transcript schema (no advisory-lock
        # analog needed — the postgres tier's reason does not apply to a file).
        for statement in _FACTS_SCHEMA_STATEMENTS:
            self._manager._conn.execute(statement)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._manager._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = self._manager._conn.execute(sql, tuple(params)).fetchall()
        return rows

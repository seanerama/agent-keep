"""In-memory session manager — the NON-DURABLE building block.

As of stage 20 this module is selected by NO buildable spec and ships in NO
composed image: every buildable persistence tier has a real component now
(sqlite_persistence on files, postgres_persistence on the database), so an
agent can no longer silently get memory-only sessions (issue #59). It remains
in the library as the seam's simplest implementation — unit tests wire it
directly where durability is not the behavior under test.

Stage 17's `spec.sessions.definition` semantics apply here too: with no
definition (the default) every message lands in the one 'single' session;
with `definition: per-channel` each channel identity gets its own session;
with `definition: per-user` (stage 23) each sender's per-platform principal
does (sessions.session_key is the single source of truth for the keying rule
— shared with both persistence tiers so no manager ever cuts conversations
differently).
"""

from agent_runtime.messages import InternalMessage
from agent_runtime.sessions import Session, session_key


class SingleSessionManager:
    def __init__(self, definition: str | None = None) -> None:
        self._definition = definition
        self._sessions: dict[str, Session] = {}

    def session_for(self, message: InternalMessage) -> Session:
        key = session_key(self._definition, message)
        session = self._sessions.get(key)
        if session is None:
            session = Session(session_id=key)
            self._sessions[key] = session
        return session

"""Facts memory seam — the narrow contract the persistence tiers implement so
the facts component (components/facts_memory) reuses the stage-15/20 connection
plumbing WITHOUT duplicating a line of it.

`spec.memory.structure.kind: facts` puts structured, human-auditable key/value
records on the ACTIVE persistence tier (the schema pins it — FactsMemory.store
is Literal["none"]: "facts live in the persistence tier"). Facts are NOT a
vector store: no embeddings, no similarity — deterministic reads.

Two tiers serve one component. Rather than copy the postgres component's
liveness/reconnect machinery (or the sqlite component's WAL/busy_timeout
posture) into a facts store, each tier exposes a thin `FactsBackend` bound to
its OWN connection: the facts component builds dialect-correct SQL from the
backend's `placeholder` + `now_sql` and runs it through the tier's existing
statement path, so the tier's failure posture governs facts exactly as it
governs transcript turns. This seam is PROMOTED to always-shipped core (the
stage-17 embedding.py precedent): both tier modules — present in every image
that selects their tier — name the seam whether or not the facts component
ships, and components/facts_memory re-exports `Fact`/`FactsBackend` with pins
so its import surface stays stable.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Fact:
    """One stored fact — a key/value record with a stable id and timestamps.

    `fact_id` is a UUID that survives upserts (an updated fact keeps its id)
    and `created_at`/`updated_at` are tier-side timestamps, so the future
    Mechanic log-egress contract can address a fact the same way it addresses a
    transcript turn (the stage-15/20 stable-id constraint, applied to facts).
    """

    key: str
    value: str
    fact_id: str
    created_at: str
    updated_at: str


class FactsBackend(Protocol):
    """The persistence tier's facts-table surface (implemented by
    components/sqlite_persistence and components/postgres_persistence over each
    tier's own connection + failure posture — see the module docstring).

    The facts component owns every statement's TEXT (built from `placeholder`
    and `now_sql`); the backend only names its dialect and runs the statement
    through the tier's plumbing. Reads return raw rows; the component builds
    `Fact`s. Structural typing: a tier need not import this Protocol to satisfy
    it, but both annotate `facts_backend()` with it so mypy verifies the shape.
    """

    #: Parameter placeholder for this dialect: '?' (sqlite) / '%s' (postgres).
    placeholder: str
    #: SQL scalar for the current UTC timestamp in this dialect — spliced into
    #: an UPDATE's `updated_at =` (a fixed component constant, never user input).
    now_sql: str

    def ensure_facts_schema(self) -> None:
        """Create the facts table additively (CREATE IF NOT EXISTS), serialized
        across concurrent boots exactly as the tier serializes its own schema."""
        ...

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Run a non-returning statement (DDL / INSERT / UPDATE / DELETE)."""
        ...

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run a SELECT and return its rows."""
        ...

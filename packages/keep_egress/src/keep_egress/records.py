"""The `egress` audit record — audit-record v1, additive record kind.

Shape discipline: same append-only plane as `agent_runtime.audit` (jsonl,
append mode on every write, no update/delete/read-back in the interface) but
the proxy's OWN file — the proxy observes the agent from outside and never
shares the worker's audit.jsonl (no write collision). Field names are FROZEN
(first green test of the egress stage) and additive-only from then on; the
authoritative field list lives in the `keep_egress` package docstring.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

#: Safe target representation for a request whose target could not be parsed —
#: raw request bytes never reach the log (digests-not-payloads discipline).
INVALID_TARGET = "invalid"


class ObservedAgent(BaseModel):
    """Identity of the OBSERVED agent, read from the same spec.yaml the agent
    image was baked from (metadata.slug / metadata.specVersion). The proxy has
    no reliable view of the agent's image digest (it runs outside that image),
    so — unlike agent_runtime's AgentIdentity — no image_digest field exists
    here; adding one later would be additive."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    spec_version: str = Field(description="metadata.specVersion of the mounted spec.")


class EgressAuditRecord(BaseModel):
    """One outbound connection ATTEMPT — allowed or denied — per the
    egress-observation v1 contract ("Audit record kind: `egress` with
    `action: connect`"). Sits alongside agent_runtime.audit.AuditRecord in the
    same audit plane: shared envelope names (id/ts/agent/event/action), plus
    the egress-specific fields the contract requires."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    agent: ObservedAgent
    event: Literal["egress"] = "egress"
    action: Literal["connect"] = "connect"
    target: str = Field(
        description="host:port of the attempt; 'invalid' when unparseable "
        "(never raw request bytes, never URLs beyond host:port)."
    )
    verdict: Literal["allowed", "denied"]
    matched_entry: str | None = Field(
        description="The sandbox.egress entry that allowed the attempt; null on deny."
    )
    bytes_up: int = Field(default=0, ge=0, description="Bytes relayed client->target, on close.")
    bytes_down: int = Field(default=0, ge=0, description="Bytes relayed target->client, on close.")
    run_id: str | None = Field(
        default=None,
        description="Run-correlation key when the attempt is attributable to a "
        "run (contract: 'when attributable'); the v1 proxy is not run-aware — "
        "null on every record today.",
    )


class EgressAuditSink(Protocol):
    """Append-only at the interface level: no update, no delete, no read-back."""

    def append(self, record: EgressAuditRecord) -> None: ...


class EgressJsonlSink:
    """Append-only jsonl on local disk — the jsonl_audit pattern, applied to
    the proxy's own file (opened in append mode on every write so the sink
    itself cannot rewrite history)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: EgressAuditRecord) -> None:
        line = record.model_dump_json()
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

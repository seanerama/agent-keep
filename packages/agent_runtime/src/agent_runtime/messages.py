"""InternalMessage — the canonical inbound message shape.

Contract: contracts/internal-message.md (frozen v1). Every channel adapter
translates INTO this shape at the boundary; everything downstream speaks only
this. Downstream components MUST ignore unknown content-block types rather
than error (forward compatibility of additive types) — hence blocks carry an
open `type` and allow extra per-type fields.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ChannelRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(description="e.g. dev-http | discord | slack | system (additive enum).")
    conversation_id: str = Field(description="Platform-scoped opaque string.")


class Sender(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["human", "system"] = Field(description="system = trigger-originated.")
    platform_id: str | None = Field(description="Opaque platform identity; null for system.")
    internal_user_id: str | None = Field(
        default=None, description="Resolved internal identity, or null if unmapped."
    )
    verified: bool = Field(
        description="Signature/token verification passed at the adapter. Adapters MUST set this "
        "honestly (false when the platform offers no verification)."
    )


class ContentBlock(BaseModel):
    """One ordered content block. `type` is an additive enum (text|image|file|event|...).

    Extra fields are allowed here (per-type block fields); unknown types must be
    IGNORED by downstream consumers, never rejected.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: str = Field(description="Component id + version that produced this.")
    trust: str = Field(
        description="untrusted | operator (additive). ALL inbound human content is untrusted; "
        "operator is reserved for content from the agent's own spec/config."
    )


class InternalMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="UUID, assigned at the normalizing boundary.",
    )
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC), description="RFC 3339, UTC.")
    channel: ChannelRef
    sender: Sender
    content: list[ContentBlock] = Field(
        description="Ordered content blocks — always a block list, never a bare string."
    )
    provenance: Provenance

    def text(self) -> str:
        """Concatenated text of `text` blocks; unknown block types are ignored."""
        return "\n".join(b.text for b in self.content if b.type == "text" and b.text is not None)

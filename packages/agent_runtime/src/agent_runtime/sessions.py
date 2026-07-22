"""Session interfaces — a session is the ordered turn history the assembler reads."""

from dataclasses import dataclass, field
from typing import Literal, Protocol

from agent_runtime.messages import InternalMessage


@dataclass
class Turn:
    role: Literal["user", "assistant"]
    text: str
    trust: str  # provenance trust of the content: untrusted | operator | ...
    platform: str
    message_id: str | None = None


@dataclass
class Session:
    session_id: str
    turns: list[Turn] = field(default_factory=list)

    def add_message(self, message: InternalMessage) -> None:
        self.turns.append(
            Turn(
                role="user",
                text=message.text(),
                trust=message.provenance.trust,
                platform=message.channel.platform,
                message_id=message.id,
            )
        )

    def add_reply(self, text: str) -> None:
        self.turns.append(Turn(role="assistant", text=text, trust="operator", platform="internal"))


class SessionManager(Protocol):
    def session_for(self, message: InternalMessage) -> Session: ...


def session_key(definition: str | None, message: InternalMessage) -> str:
    """What a session IS (spec.sessions.definition), as a session id.

    - absent (None): the skeleton's one shared session — id 'single',
      exactly as before stage 17 (the kill-switch: no definition, no change).
    - 'per-channel': conversation state keyed by CHANNEL identity — the NOC
      room is one conversation regardless of which rostered person speaks.
      Two senders in one channel share; two channels never do.
    - 'per-user' (stage 23): conversation state keyed by the SENDER's
      principal — the owner's thread follows the owner across every channel
      of one platform (the schema: 'per user (shared across channels)');
      two users in one channel never share. This is per-PLATFORM-user,
      deliberately: unifying one human across platforms is a gateway concern
      (spec.gateway.identityUnification), and 'separate' — the only buildable
      value, the one client-tracking selects — keeps each platform identity
      its own person, so the same opaque id on two platforms is two sessions,
      never unified identity. A trigger-originated message (`sender.kind:
      system`, platform_id null) has no owning user and keys by channel
      identity instead — each synthetic trigger conversation is its own
      thread.

      Accepted limitation (#79): the null-`platform_id` fallback below keys by
      CHANNEL regardless of `sender.kind`, so a FUTURE adapter that emitted a
      null-id *human* would silently share one channel bucket under per-user
      (rather than getting an isolated per-user thread). No shipping adapter
      does this — dev-http coerces anonymous senders to a stable
      `dev-http-anonymous` id (a pre-existing dev-only merge), and the real
      channel adapters always carry a platform_id for humans — so this is a
      documented guard for a sender shape that cannot occur today, not a live
      gap. Left as-is deliberately.

    One helper, every manager: the sqlite tier (sqlite_persistence), the
    postgres tier (postgres_persistence), and the in-memory building block
    (single_session) key sessions identically, so
    flipping persistence.tier never re-cuts conversations. Any other
    definition ('hybrid') is refused by the wiring guard before a
    manager exists; this raises as defense in depth behind it.
    """
    if definition is None:
        return "single"
    if definition == "per-channel":
        return f"channel:{message.channel.platform}:{message.channel.conversation_id}"
    if definition == "per-user":
        if message.sender.platform_id is None:
            return f"channel:{message.channel.platform}:{message.channel.conversation_id}"
        return f"user:{message.channel.platform}:{message.sender.platform_id}"
    raise ValueError(
        f"session definition '{definition}' has no implementation in this component-library "
        "version. The wiring guard should have refused this spec."
    )

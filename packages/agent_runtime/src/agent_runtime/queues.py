"""Message queue interface between channel adapters and the agent core."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from agent_runtime.messages import InternalMessage


@dataclass
class QueueItem:
    """An inbound message plus the future the originating adapter awaits for the reply."""

    message: InternalMessage
    reply: asyncio.Future[str]


class MessageQueue(Protocol):
    async def put(self, item: QueueItem) -> None: ...

    async def get(self) -> QueueItem: ...


class Gate(Protocol):
    """A gateway admission gate a channel adapter consults BEFORE it enqueues.

    Returns the admitted message (identity resolved) or None when the sender is
    not permitted — in which case the gate has already written the rejection to
    the audit sink and the adapter MUST NOT enqueue (nothing goes downstream).

    Structural typing keeps the adapters importable in images that ship no gate
    (specs without spec.gateway.allowlist — the gateway module is absent then,
    absence semantics), while agent_runtime.gateway.AllowlistGate satisfies it.
    """

    def admit(self, message: InternalMessage) -> InternalMessage | None: ...

"""dev-http channel adapter — localhost-only HTTP, zero external platform deps.

POST /message  {"text": "...", "conversation_id"?: str, "sender_id"?: str}
GET  /healthz  -> {"status": "ok"}

Produces internal-message v1 objects (platform: dev-http) at the boundary.
dev-http offers no sender verification, so `verified` is honestly false and
all inbound content is marked `untrusted` (contract: internal-message).

Unmatched paths can be delegated to optional extension routes injected by the
runner (an awaitable that returns a response or None). This adapter knows
nothing about what those routes serve — the modules that own extra endpoints
ship them, so a spec that selects none has none (absence semantics).

Implemented on asyncio streams (stdlib only) so the composed image needs no
HTTP framework. Binds 127.0.0.1 by default; inside a container the bind host
comes from DEV_HTTP_HOST (the container boundary is the isolation there).
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent_runtime import __version__
from agent_runtime.components.http_receiver import BoundedHttpReceiver, ReceiverLimits
from agent_runtime.messages import ChannelRef, ContentBlock, InternalMessage, Provenance, Sender
from agent_runtime.queues import Gate, MessageQueue, QueueItem

logger = logging.getLogger(__name__)

ADAPTER_ID = f"dev_http@{__version__}"
PLATFORM = "dev-http"
REPLY_TIMEOUT_SECONDS = 30.0
MAX_BODY_BYTES = 1_000_000

#: Extension-route callable: (method, path, headers, body) -> response or None
#: (None = not handled here; the adapter answers 404).
ExtraRoutes = Callable[
    [str, str, Mapping[str, str], bytes | None],
    Awaitable[tuple[int, dict[str, Any]] | None],
]


class DevHttpAdapter:
    def __init__(
        self,
        queue: MessageQueue,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        extra_routes: ExtraRoutes | None = None,
        gate: Gate | None = None,
    ) -> None:
        self._queue = queue
        self._host = host
        self._port = port
        self._extra_routes = extra_routes
        # Generic gateway allowlist enforcement — wired ONLY when the spec
        # declares spec.gateway.allowlist. None ⇒ no identity layer, behavior
        # bit-identical to before (absence semantics).
        self._gate = gate
        # The shared bounded transport (AVAIL-01). This adapter provides only
        # the route logic (_handle); the receiver owns the socket, the bounds,
        # the deadlines, and the disconnect-watch that cancels the in-flight
        # turn if the client vanishes mid-reply (dev-http replies synchronously).
        self._receiver = BoundedHttpReceiver(
            self._handle,
            host=host,
            port=port,
            limits=ReceiverLimits(max_body_bytes=MAX_BODY_BYTES),
            error_label="dev-http request",
        )

    async def serve(self) -> None:
        await self._receiver.serve()

    async def _handle_request(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, Any]]:
        """Thin shim over the shared receiver's transport seam — kept so the
        socket-seam tests (a hand-fed StreamReader) keep driving the real
        bounded parse + this adapter's route logic."""
        return await self._receiver.handle_request(reader)

    def normalize(self, payload: dict[str, Any]) -> InternalMessage:
        """Translate the adapter-private HTTP payload into internal-message v1."""
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("body must include a non-empty string field 'text'")
        conversation_id = payload.get("conversation_id") or "dev"
        sender_id = payload.get("sender_id") or "dev-http-anonymous"
        return InternalMessage(
            channel=ChannelRef(platform=PLATFORM, conversation_id=str(conversation_id)),
            sender=Sender(
                kind="human",
                platform_id=str(sender_id),
                internal_user_id=None,
                verified=False,  # dev-http offers no verification — set honestly
            ),
            content=[ContentBlock(type="text", text=text)],
            provenance=Provenance(adapter=ADAPTER_ID, trust="untrusted"),
        )

    async def _handle(
        self, method: str, path: str, headers: dict[str, str], raw_body: bytes
    ) -> tuple[int, dict[str, Any]]:
        """Route logic, unchanged: dispatch GET /healthz and POST /message
        (with the gate admission + synchronous reply), delegate anything else
        to the injected extension routes, else 404. The shared receiver has
        already enforced the transport bounds and handed over the raw body."""
        if method == "GET" and path == "/healthz":
            return 200, {"status": "ok"}
        if method == "POST" and path == "/message":
            if not raw_body:
                return 400, {"error": "missing or oversized body"}
            try:
                payload = json.loads(raw_body)
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                message = self.normalize(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                return 400, {"error": str(exc)}
            if self._gate is not None:
                admitted = self._gate.admit(message)
                if admitted is None:
                    # Not on the roster — the gate already audited the drop; the
                    # message is never enqueued, so nothing runs downstream.
                    return 403, {"error": "sender not permitted by gateway allowlist"}
                message = admitted
            loop = asyncio.get_running_loop()
            item = QueueItem(message=message, reply=loop.create_future())
            await self._queue.put(item)
            try:
                reply_text = await asyncio.wait_for(item.reply, timeout=REPLY_TIMEOUT_SECONDS)
            except TimeoutError:
                return 504, {"error": "reply timed out"}
            except Exception as exc:
                return 500, {"error": f"agent error: {exc}"}
            return 200, {"reply": reply_text, "message_id": message.id}
        if self._extra_routes is not None:
            routed = await self._extra_routes(method, path, headers, raw_body or None)
            if routed is not None:
                return routed
        return 404, {"error": "not found"}

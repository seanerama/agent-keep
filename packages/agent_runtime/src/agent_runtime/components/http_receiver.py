"""Shared bounded stdlib HTTP receiver — the transport half of ENH-06 / #125,
closing AVAIL-01 (slowloris + connection-flood + unbounded/unaudited parse
faults).

The four HTTP-facing channel components (dev_http, webex_channel,
slack_channel, event_intake) each hand-rolled a near-verbatim
``asyncio.start_server`` parser: ``serve`` / ``_handle_connection`` /
``_handle_request`` / ``_reason``. Those parsers shared the same gaps — no
request deadline (a byte-trickle hung a task forever), no header bounds (an
over-long header line hit the ``StreamReader`` 64 KiB default limit → a
``ValueError`` → an UNAUDITED 500), no connection ceiling (a flood spawned
unbounded tasks). This module lifts that plumbing ONCE and gives it the bounds
they lacked.

Design (stdlib asyncio only — NO HTTP framework, so no runtime dependency
enters any agent image; the module ships in every image as a core module, like
``queues.py``):

- ``BoundedHttpReceiver`` owns ``serve()``, ``_handle_connection`` (write the
  response, ``Connection: close``, always close the writer), the bounded
  request parse, and the ``_reason`` status table.
- ALL routing/application logic is delegated to an injected async ``handler``
  ``(method, path, headers, raw_body) -> (status, body)``. The receiver knows
  nothing about webhooks, signatures, or queues — and it hands the handler the
  EXACT raw body bytes it read (no decode/re-encode) so webex/slack can verify
  an HMAC over the raw body.
- Bounds live on an overridable :class:`ReceiverLimits`; the defaults keep the
  adapters' prior 1 MB body cap. Hostile clients now get deterministic,
  bounded 4xx / connection-close instead of a hang or an unaudited 500:
  414 (request line too long), 431 (header too large / too many / block too
  large), 413 (``Content-Length`` over the cap — was 400, an acknowledged
  change), 400 (malformed / negative ``Content-Length`` or malformed request
  line), 408 (parse deadline / read-timeout slowloris), 503 (over the
  connection ceiling).

Transport-level rejects are NOT audited (they carry no principal and would be a
noise / DoS-amplification vector); they are logged at debug. Audited rejects
stay inside the adapters' handlers (signature/roster/malformed-payload), exactly
as before.

Bind convention unchanged: ``host`` defaults to ``127.0.0.1``; the composer
sets ``DEV_HTTP_HOST=0.0.0.0`` inside a container (the container boundary is the
isolation).
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The injected application handler: the receiver parses the request, enforces
#: the bounds, and delegates routing here. ``raw_body`` is the exact bytes read
#: off the wire (no decode/re-encode — signature verification depends on it).
Handler = Callable[[str, str, dict[str, str], bytes], Awaitable[tuple[int, dict[str, Any]]]]


@dataclass(frozen=True)
class ReceiverLimits:
    """Bounds the shared receiver enforces. Defaults are the AVAIL-01 ceilings;
    an adapter may override any field (e.g. a channel with a documented larger
    body could raise ``max_body_bytes``). All four adapters keep the 1 MB body
    cap they had before this stage."""

    #: Request line (``METHOD SP PATH SP VERSION``) longer than this → 414.
    max_request_line_bytes: int = 8192
    #: Any single header line longer than this → 431.
    max_header_line_bytes: int = 8192
    #: More header lines than this → 431.
    max_header_count: int = 100
    #: Aggregate header block larger than this → 431.
    max_total_header_bytes: int = 65536
    #: ``Content-Length`` over this → 413 (malformed/negative stays 400).
    max_body_bytes: int = 1_000_000
    #: Accept gate: beyond this many in-flight connections the receiver refuses
    #: (503 + close) WITHOUT beginning to parse — the task/connection count
    #: stays bounded under flood; established connections keep being served.
    max_concurrent_connections: int = 128
    #: Deadline on each individual read (readline / readexactly) — a trickle trips it.
    read_timeout_seconds: float = 10.0
    #: TOTAL wall-clock budget for the parse phase (request line + headers +
    #: body) — the slowloris guard. On expiry → 408, before any handler runs.
    header_deadline_seconds: float = 10.0
    #: Deadline on ``writer.drain()`` so a stuck reader cannot pin a writer.
    write_timeout_seconds: float = 10.0


#: Superset reason table covering every status the four adapters + the bounds
#: use. The phrase is cosmetic; the status code is the contract.
_REASON = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    408: "Request Timeout",
    409: "Conflict",
    413: "Payload Too Large",
    414: "URI Too Long",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def reason(status: int) -> str:
    return _REASON.get(status, "Unknown")


class _HttpError(Exception):
    """A bounded, deterministic transport-level rejection (4xx). Carries the
    status the receiver answers with; never audited (no principal)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _Disconnected(Exception):
    """The peer closed the socket mid-parse — clean up, answer nothing."""


async def _suppress(task: "asyncio.Future[Any]") -> None:
    """Cancel a task and swallow its result/exception — no orphan, no
    never-retrieved exception warning."""
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


class BoundedHttpReceiver:
    """A shared, bounded, stdlib-asyncio HTTP/1.1 receiver.

    Construct one per adapter with the adapter's route logic as ``handler``;
    the receiver enforces the transport bounds and calls the handler with the
    parsed ``(method, path, headers, raw_body)``.
    """

    def __init__(
        self,
        handler: Handler,
        *,
        port: int,
        host: str = "127.0.0.1",
        limits: ReceiverLimits | None = None,
        error_label: str = "http request",
    ) -> None:
        self._handler = handler
        self._host = host
        self._port = port
        self._limits = limits or ReceiverLimits()
        self._error_label = error_label
        #: In-flight connections currently past the accept gate (the flood
        #: ceiling is enforced against this). Exposed for the conformance suite
        #: to assert boundedness.
        self._active = 0
        self._max_active = 0

    @property
    def limits(self) -> ReceiverLimits:
        return self._limits

    async def serve(self) -> None:
        # A generous stream buffer limit so a line up to our logical per-line
        # bound never trips the StreamReader's own ValueError before our
        # explicit length check maps it to a deterministic 414/431.
        stream_limit = (
            max(self._limits.max_request_line_bytes, self._limits.max_header_line_bytes) + 16
        )
        server = await asyncio.start_server(
            self._handle_connection, self._host, self._port, limit=stream_limit
        )
        logger.info("%s receiver listening on %s:%s", self._error_label, self._host, self._port)
        async with server:
            await server.serve_forever()

    # ---------------------------------------------------------------- seam

    async def handle_request(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, Any]]:
        """Parse a request off ``reader`` and run the handler directly.

        This is the transport seam the adapters' thin ``_handle_request`` shims
        (and their migrated socket-seam tests) drive with a hand-fed
        ``StreamReader``. It exercises the real bounded parse + the real handler
        — but not the accept gate, the parse deadline, or the disconnect-watch,
        which only make sense over a live socket (a fed reader is complete).
        """
        try:
            method, path, headers, raw_body = await self._parse(reader)
        except _HttpError as exc:
            return exc.status, {"error": exc.message}
        except _Disconnected:
            return 400, {"error": "malformed request"}
        return await self._handler(method, path, headers, raw_body)

    # -------------------------------------------------------- connection

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Accept gate: beyond the ceiling, refuse WITHOUT beginning to parse.
        # The check + increment run with no await between them, so under
        # single-threaded asyncio the ceiling is enforced atomically.
        if self._active >= self._limits.max_concurrent_connections:
            logger.debug("%s: connection ceiling reached — refusing (503)", self._error_label)
            await self._write(writer, 503, {"error": "server busy"})
            await self._close(writer)
            return
        self._active += 1
        self._max_active = max(self._max_active, self._active)
        try:
            result = await self._serve(reader, writer)
            if result is not None:
                status, body = result
                await self._write(writer, status, body)
        except Exception:
            logger.exception("%s failed", self._error_label)
            await self._write(writer, 500, {"error": "internal error"})
        finally:
            self._active -= 1
            await self._close(writer)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[int, dict[str, Any]] | None:
        # Parse phase under the TOTAL parse deadline (the slowloris guard); it
        # completes BEFORE the handler runs, so a parse-phase trickle trips the
        # deadline before any downstream work exists.
        try:
            method, path, headers, raw_body = await asyncio.wait_for(
                self._parse(reader), timeout=self._limits.header_deadline_seconds
            )
        except TimeoutError:
            # asyncio.wait_for raises TimeoutError on expiry (per-read or the
            # total parse deadline); both are the slowloris guard -> 408.
            logger.debug("%s: parse deadline expired (408)", self._error_label)
            return 408, {"error": "request timeout"}
        except _HttpError as exc:
            return exc.status, {"error": exc.message}
        except _Disconnected:
            logger.debug("%s: peer disconnected mid-parse", self._error_label)
            return None  # abrupt disconnect — nothing to answer, just close
        # Handler phase with a client-disconnect watch: dev_http (and webex
        # until Stage 37) await the turn INSIDE the handler, so a client that
        # vanishes mid-turn is NOT caught by the parse deadline (parsing is
        # done). Race the handler against a reader-EOF probe and cancel the
        # in-flight turn if the client disconnects first.
        return await self._run_handler(reader, method, path, headers, raw_body)

    async def _run_handler(
        self,
        reader: asyncio.StreamReader,
        method: str,
        path: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> tuple[int, dict[str, Any]] | None:
        loop = asyncio.get_running_loop()
        handler_task: asyncio.Future[tuple[int, dict[str, Any]]] = asyncio.ensure_future(
            self._handler(method, path, headers, raw_body)
        )
        watcher = loop.create_task(self._watch_disconnect(reader))
        pending: set[asyncio.Future[Any]] = {handler_task, watcher}
        try:
            done, _pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            await _suppress(handler_task)
            await _suppress(watcher)
            raise
        if handler_task in done:
            await _suppress(watcher)
            return handler_task.result()
        # Client disconnected mid-handler: cancel the in-flight turn, no orphan.
        logger.debug(
            "%s: client disconnected mid-handler — cancelling in-flight work", self._error_label
        )
        await _suppress(handler_task)
        return None

    async def _watch_disconnect(self, reader: asyncio.StreamReader) -> None:
        """Complete when the peer closes its side (EOF). Any extra bytes on a
        Connection: close request are unexpected and simply discarded — only an
        actual EOF signals disconnect."""
        while True:
            chunk = await reader.read(256)
            if not chunk:
                return

    # -------------------------------------------------------------- parse

    async def _parse(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
        request_line = await self._read_line(
            reader, self._limits.max_request_line_bytes, too_long_status=414
        )
        request_line = request_line.strip()
        if not request_line:
            raise _HttpError(400, "empty request")
        parts = request_line.split(" ")
        if len(parts) != 3:
            raise _HttpError(400, "malformed request line")
        method, path, _version = parts

        headers: dict[str, str] = {}
        total = 0
        count = 0
        while True:
            line = await self._read_line(
                reader, self._limits.max_header_line_bytes, too_long_status=431
            )
            if line in ("\r\n", "\n", ""):
                break
            count += 1
            total += len(line)
            if count > self._limits.max_header_count:
                raise _HttpError(431, "too many headers")
            if total > self._limits.max_total_header_bytes:
                raise _HttpError(431, "header block too large")
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError:
            raise _HttpError(400, "malformed content-length") from None
        if content_length < 0:
            raise _HttpError(400, "malformed content-length")
        if content_length > self._limits.max_body_bytes:
            # Acknowledged observable change: over the cap is 413 (was 400).
            raise _HttpError(413, "payload too large")

        if content_length > 0:
            try:
                raw_body = await asyncio.wait_for(
                    reader.readexactly(content_length),
                    timeout=self._limits.read_timeout_seconds,
                )
            except asyncio.IncompleteReadError as exc:
                raise _Disconnected() from exc
        else:
            raw_body = b""
        return method, path, headers, raw_body

    async def _read_line(
        self, reader: asyncio.StreamReader, max_bytes: int, *, too_long_status: int
    ) -> str:
        try:
            raw = await asyncio.wait_for(
                reader.readline(), timeout=self._limits.read_timeout_seconds
            )
        except ValueError:
            # The StreamReader's own buffer limit was exceeded before a newline
            # was found — the line is over the bound. Map to a deterministic
            # 414/431 instead of the pre-stage unaudited 500.
            raise _HttpError(too_long_status, "line too long") from None
        # asyncio.TimeoutError propagates to the parse deadline handler (-> 408).
        if len(raw) > max_bytes:
            raise _HttpError(too_long_status, "line too long")
        if raw and not raw.endswith(b"\n"):
            # EOF before a newline — the peer closed mid-line.
            raise _Disconnected()
        return raw.decode("ascii", errors="replace")

    # -------------------------------------------------------------- write

    async def _write(self, writer: asyncio.StreamWriter, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reason(status)}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        )
        try:
            writer.write(head.encode("ascii") + payload)
            await asyncio.wait_for(writer.drain(), timeout=self._limits.write_timeout_seconds)
        except (ConnectionError, TimeoutError, RuntimeError):
            # A stuck or vanished reader must not pin this writer — the
            # connection is closed unconditionally in _close.
            pass

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, RuntimeError):
            pass

"""The stdlib-only asyncio forward proxy — the observed choke point itself.

HTTP proxying (absolute-form request target) + HTTPS via `CONNECT` (target
observed as host:port; v1 observes connection targets, never decrypted
payloads). Enforcement is FAIL-CLOSED and happens BEFORE any upstream
connection or DNS resolution: a non-matching target is refused observably
(403 on absolute-form; CONNECT rejected before tunnel establishment) and a
malformed request is refused with 400 — both audited as denied. Denials
surface to the agent as ordinary HTTP failures, so the agent needs no
knowledge of the proxy beyond its proxy env vars (contract egress-observation
v1, wire rules).

One `egress` audit record per accepted client connection — allowed and denied
alike — appended on close with the relayed byte counts. See the package
docstring for the frozen field names.
"""

import asyncio
import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from keep_egress.records import (
    INVALID_TARGET,
    EgressAuditRecord,
    EgressAuditSink,
    ObservedAgent,
)
from keep_spec.egress import match_allowlist

#: Request head (request line + headers) size cap; beyond it = malformed.
MAX_HEAD_BYTES = 64 * 1024

#: Relay chunk size.
CHUNK_BYTES = 64 * 1024

#: CONNECT authority-form target: host:port, port REQUIRED (RFC 7231 §4.3.6).
#: Bracketed IPv6 literals are outside the sandbox.egress grammar
#: (keep_spec.models.EGRESS_HOST) and are treated as malformed — refused and
#: audited with the safe 'invalid' target, never partially parsed.
_CONNECT_AUTHORITY = re.compile(r"^(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*):(?P<port>[0-9]{1,5})$")

_RESPONSE_400 = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_RESPONSE_403 = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_RESPONSE_502 = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_RESPONSE_200_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"

#: HTTP method token (RFC 9110 tchar subset — practically, methods are
#: ASCII letters); anything else on the request line is malformed, refused
#: and audited with the safe target — junk bytes are never forwarded.
_METHOD_TOKEN = re.compile(rb"^[A-Za-z]{1,32}$")

#: Hop-by-hop headers stripped when forwarding an absolute-form request
#: (Connection: close is injected instead — one origin exchange per client
#: connection, so byte counts close deterministically).
_HOP_BY_HOP = (b"connection", b"proxy-connection", b"keep-alive", b"proxy-authorization")


@dataclass
class _ByteCounter:
    up: int = 0
    down: int = 0


@dataclass(frozen=True)
class _ParsedRequest:
    """A well-formed proxy request: either a CONNECT tunnel or an
    absolute-form HTTP request to forward."""

    kind: str  # "connect" | "absolute"
    host: str
    port: int
    method: bytes = b""
    origin_form: bytes = b""  # path[?query] for absolute-form forwarding
    version: bytes = b""
    header_lines: tuple[bytes, ...] = ()

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    def rewritten_head(self) -> bytes:
        """Absolute-form request rewritten to origin-form for the target
        server, hop-by-hop headers stripped, Connection: close injected."""
        lines = [self.method + b" " + self.origin_form + b" " + self.version]
        for raw in self.header_lines:
            name = raw.split(b":", 1)[0].strip().lower()
            if name in _HOP_BY_HOP:
                continue
            lines.append(raw)
        lines.append(b"Connection: close")
        return b"\r\n".join(lines) + b"\r\n\r\n"


def _parse_request_head(head: bytes) -> _ParsedRequest | None:
    """Parse a proxy request head; None = malformed (refuse + audit denied).

    Accepted forms: `CONNECT host:port HTTP/1.x` and absolute-form
    `METHOD http://host[:port]/path HTTP/1.x`. Origin-form (`GET /path`) is
    NOT a proxy request — a proxy cannot determine its target — and is
    malformed here by design.
    """
    lines = head[:-4].split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3:
        return None
    method, raw_target, version = parts
    if not version.startswith(b"HTTP/") or _METHOD_TOKEN.match(method) is None:
        return None
    try:
        target_text = raw_target.decode("ascii")
    except UnicodeDecodeError:
        return None
    header_lines = tuple(lines[1:])

    if method.upper() == b"CONNECT":
        authority = _CONNECT_AUTHORITY.match(target_text)
        if authority is None:
            return None
        port = int(authority.group("port"))
        if not 1 <= port <= 65535:
            return None
        return _ParsedRequest(kind="connect", host=authority.group("host").lower(), port=port)

    lowered = target_text.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return None
    split = urlsplit(target_text)
    try:
        explicit_port = split.port
    except ValueError:
        return None
    host = (split.hostname or "").lower()
    if not host or "[" in host:
        return None
    port = explicit_port or (443 if split.scheme.lower() == "https" else 80)
    origin_form = split.path or "/"
    if split.query:
        origin_form += f"?{split.query}"
    return _ParsedRequest(
        kind="absolute",
        host=host,
        port=port,
        method=method,
        origin_form=origin_form.encode("ascii", errors="replace"),
        version=version,
        header_lines=header_lines,
    )


class EgressProxy:
    """The forward proxy: allowlist enforcement + audit emission per attempt.

    `allowlist` is `sandbox.egress` from the SAME spec the agent was baked
    from (see the package docstring for how it arrives); `agent` identifies
    the observed agent on every record; `sink` is the proxy's own append-only
    audit file.
    """

    def __init__(
        self,
        allowlist: Sequence[str],
        agent: ObservedAgent,
        sink: EgressAuditSink,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._allowlist = list(allowlist)
        self._agent = agent
        self._sink = sink
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port, limit=MAX_HEAD_BYTES
        )

    @property
    def bound_port(self) -> int:
        assert self._server is not None, "proxy not started"
        socket_port: int = self._server.sockets[0].getsockname()[1]
        return socket_port

    async def serve_forever(self) -> None:
        assert self._server is not None, "proxy not started"
        await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------- connection
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One client connection = one audited attempt, no exceptions: the
        record is appended in the finally path of the connection's lifetime."""
        record: EgressAuditRecord | None = None
        try:
            record = await self._proxy_connection(reader, writer)
        except Exception:
            # An unexpected relay failure must still leave an audit trail —
            # fail-closed applies to observation as much as to enforcement.
            if record is None:
                record = self._record(INVALID_TARGET, "denied", None)
            raise
        finally:
            if record is not None:
                self._sink.append(record)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _proxy_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> EgressAuditRecord:
        request = await self._read_request(reader)
        if request is None:
            # Malformed (or empty) request: refused + audited as denied with
            # the safe target representation.
            await self._respond(writer, _RESPONSE_400)
            return self._record(INVALID_TARGET, "denied", None)

        matched = match_allowlist(self._allowlist, request.host, request.port)
        if matched is None:
            # Fail-closed refusal BEFORE any DNS/upstream contact — observable
            # (403; CONNECT is rejected before tunnel establishment), never a
            # silent drop.
            await self._respond(writer, _RESPONSE_403)
            return self._record(request.target, "denied", None)

        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                request.host, request.port
            )
        except OSError:
            # The allowlist allowed the ATTEMPT; the target was unreachable.
            await self._respond(writer, _RESPONSE_502)
            return self._record(request.target, "allowed", matched)

        counter = _ByteCounter()
        try:
            if request.kind == "connect":
                writer.write(_RESPONSE_200_ESTABLISHED)
                await writer.drain()
            else:
                head = request.rewritten_head()
                upstream_writer.write(head)
                await upstream_writer.drain()
                counter.up += len(head)
            await self._tunnel(reader, writer, upstream_reader, upstream_writer, counter)
        finally:
            upstream_writer.close()
            with contextlib.suppress(Exception):
                await upstream_writer.wait_closed()
        return self._record(request.target, "allowed", matched, counter)

    async def _read_request(self, reader: asyncio.StreamReader) -> _ParsedRequest | None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            return None
        return _parse_request_head(head)

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        counter: _ByteCounter,
    ) -> None:
        """Bidirectional relay; byte counts survive either side failing."""
        await asyncio.gather(
            self._pipe(client_reader, upstream_writer, counter, "up"),
            self._pipe(upstream_reader, client_writer, counter, "down"),
            return_exceptions=True,
        )

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        counter: _ByteCounter,
        direction: str,
    ) -> None:
        try:
            while True:
                chunk = await reader.read(CHUNK_BYTES)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
                if direction == "up":
                    counter.up += len(chunk)
                else:
                    counter.down += len(chunk)
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError, RuntimeError):
                if writer.can_write_eof():
                    writer.write_eof()

    async def _respond(self, writer: asyncio.StreamWriter, response: bytes) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            writer.write(response)
            await writer.drain()

    def _record(
        self,
        target: str,
        verdict: str,
        matched_entry: str | None,
        counter: _ByteCounter | None = None,
    ) -> EgressAuditRecord:
        assert verdict in ("allowed", "denied")
        return EgressAuditRecord(
            agent=self._agent,
            target=target,
            verdict="allowed" if verdict == "allowed" else "denied",
            matched_entry=matched_entry,
            bytes_up=counter.up if counter else 0,
            bytes_down=counter.down if counter else 0,
            run_id=None,  # v1 proxy is not run-aware ("when attributable")
        )

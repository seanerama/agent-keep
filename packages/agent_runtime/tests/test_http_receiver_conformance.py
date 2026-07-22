"""Transport conformance suite for the shared BoundedHttpReceiver (stage 36,
AVAIL-01).

ONE parametrized suite, driven with a RAW asyncio socket client (not httpx) so
partial / slow / malformed bytes can be sent deliberately. It runs against the
bare receiver (a trivial echo handler) AND — where cheap — the dev-http
adapter's composed receiver, mirroring the "one suite, every implementation"
pattern of test_queue_conformance.py. A separate wiring test proves the
carried HTTP-facing adapter shares the same bounded transport.

Every case asserts BOTH a deterministic response/close AND bounded
time/resources (no hang): the whole suite runs under tight custom limits
(sub-second deadlines) so a regression that reintroduces an unbounded wait
fails as a test timeout rather than passing slowly.
"""

import asyncio
from typing import Any

import pytest

from agent_runtime.components.dev_http import DevHttpAdapter
from agent_runtime.components.http_receiver import BoundedHttpReceiver, ReceiverLimits
from agent_runtime.components.memory_queue import InProcessQueue

pytestmark = pytest.mark.asyncio

# Tight limits so slowloris/read deadlines resolve in well under a second.
TEST_LIMITS = ReceiverLimits(
    max_request_line_bytes=200,
    max_header_line_bytes=200,
    max_header_count=20,
    max_total_header_bytes=2048,
    max_body_bytes=1000,
    max_concurrent_connections=8,
    read_timeout_seconds=0.4,
    header_deadline_seconds=0.4,
    write_timeout_seconds=0.4,
)


async def _echo_handler(
    method: str, path: str, headers: dict[str, str], raw_body: bytes
) -> tuple[int, dict[str, Any]]:
    return 200, {"method": method, "path": path, "len": len(raw_body)}


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    """Runs a BoundedHttpReceiver on a real loopback socket for the duration of
    a test, then tears it down (no leaked task)."""

    def __init__(self, receiver: BoundedHttpReceiver) -> None:
        self.receiver = receiver
        self.port = receiver._port
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "_Server":
        self._task = asyncio.get_running_loop().create_task(self.receiver.serve())
        # Wait until the listener is accepting, then reset the flood gauge so
        # the readiness probe does not count toward it.
        for _ in range(200):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            except OSError:
                await asyncio.sleep(0.01)
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            break
        await asyncio.sleep(0.02)
        self.receiver._max_active = 0
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


async def _roundtrip(port: int, data: bytes, *, timeout: float = 2.0) -> bytes:
    """Send raw bytes, read the full response until the server closes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    try:
        resp = await asyncio.wait_for(reader.read(), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
    return resp


def _status(resp: bytes) -> int:
    """Parse the status code off an HTTP/1.1 response line; 0 == closed with no
    response (a deterministic connection-close, also acceptable per the spec)."""
    if not resp:
        return 0
    try:
        return int(resp.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return -1


# --------------------------------------------------------------------------- fixtures


def _bare_receiver() -> BoundedHttpReceiver:
    return BoundedHttpReceiver(_echo_handler, port=_free_port(), limits=TEST_LIMITS)


def _dev_http_receiver() -> BoundedHttpReceiver:
    adapter = DevHttpAdapter(InProcessQueue(), port=_free_port())
    # Swap in the tight test limits while keeping the adapter's real handler.
    adapter._receiver = BoundedHttpReceiver(
        adapter._handle, port=adapter._port, limits=TEST_LIMITS, error_label="dev-http request"
    )
    return adapter._receiver


@pytest.fixture(params=["bare", "dev_http"])
def receiver(request: pytest.FixtureRequest) -> BoundedHttpReceiver:
    return {"bare": _bare_receiver, "dev_http": _dev_http_receiver}[request.param]()


# --------------------------------------------------------------------------- the suite


async def test_healthy_request_gets_a_response(receiver: BoundedHttpReceiver) -> None:
    async with _Server(receiver) as srv:
        resp = await _roundtrip(srv.port, b"GET /healthz HTTP/1.1\r\n\r\n")
    assert _status(resp) == 200


async def test_slow_header_slowloris_times_out(receiver: BoundedHttpReceiver) -> None:
    """Dribble a request line and then stall: the parse deadline trips → 408 (or
    a deterministic close), well within the deadline, no indefinite hang."""
    async with _Server(receiver) as srv:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(b"POST /message HTTP/1.1\r\n")  # no blank line — headers never finish
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(), timeout=2.0)
        writer.close()
    assert _status(resp) in (408, 0)


async def test_slow_body_times_out(receiver: BoundedHttpReceiver) -> None:
    """Valid headers + Content-Length, then trickle less than promised → 408/close."""
    async with _Server(receiver) as srv:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(b"POST /message HTTP/1.1\r\nContent-Length: 500\r\n\r\n" + b"x" * 10)
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(), timeout=2.0)
        writer.close()
    assert _status(resp) in (408, 0)


async def test_oversized_request_line_is_414(receiver: BoundedHttpReceiver) -> None:
    async with _Server(receiver) as srv:
        line = b"GET /" + b"a" * (TEST_LIMITS.max_request_line_bytes + 50) + b" HTTP/1.1\r\n\r\n"
        resp = await _roundtrip(srv.port, line)
    assert _status(resp) == 414


async def test_single_header_line_too_long_is_431(receiver: BoundedHttpReceiver) -> None:
    async with _Server(receiver) as srv:
        req = (
            b"POST /message HTTP/1.1\r\nX-Big: "
            + b"a" * (TEST_LIMITS.max_header_line_bytes + 50)
            + b"\r\n\r\n"
        )
        resp = await _roundtrip(srv.port, req)
    assert _status(resp) == 431


async def test_too_many_headers_is_431(receiver: BoundedHttpReceiver) -> None:
    async with _Server(receiver) as srv:
        headers = b"".join(
            f"X-H{i}: v\r\n".encode() for i in range(TEST_LIMITS.max_header_count + 5)
        )
        resp = await _roundtrip(srv.port, b"POST /message HTTP/1.1\r\n" + headers + b"\r\n")
    assert _status(resp) == 431


async def test_aggregate_header_block_too_large_is_431(receiver: BoundedHttpReceiver) -> None:
    async with _Server(receiver) as srv:
        # Each line under the per-line and count limits, but the block sums over
        # max_total_header_bytes.
        line = b"X-Pad: " + b"a" * 150 + b"\r\n"  # ~159 bytes; well under per-line 200
        headers = line * 15  # ~2385 bytes > 2048
        resp = await _roundtrip(srv.port, b"POST /message HTTP/1.1\r\n" + headers + b"\r\n")
    assert _status(resp) == 431


async def test_malformed_content_length_is_400(receiver: BoundedHttpReceiver) -> None:
    async with _Server(receiver) as srv:
        resp = await _roundtrip(
            srv.port, b"POST /message HTTP/1.1\r\nContent-Length: not-a-number\r\n\r\n"
        )
    assert _status(resp) == 400


async def test_negative_content_length_is_400(receiver: BoundedHttpReceiver) -> None:
    async with _Server(receiver) as srv:
        resp = await _roundtrip(srv.port, b"POST /message HTTP/1.1\r\nContent-Length: -5\r\n\r\n")
    assert _status(resp) == 400


async def test_oversized_content_length_is_413_not_400(receiver: BoundedHttpReceiver) -> None:
    """The acknowledged observable change: over the body cap now returns 413
    (was 400). Deterministic — the receiver answers before reading the body."""
    async with _Server(receiver) as srv:
        over = TEST_LIMITS.max_body_bytes + 1
        resp = await _roundtrip(
            srv.port, f"POST /message HTTP/1.1\r\nContent-Length: {over}\r\n\r\n".encode()
        )
    assert _status(resp) == 413


async def test_abrupt_disconnect_is_clean_and_next_connection_served(
    receiver: BoundedHttpReceiver,
) -> None:
    async with _Server(receiver) as srv:
        # Disconnect mid request-line.
        _, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(b"POST /mess")
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        # The receiver cleaned up; a healthy request still gets served.
        resp = await _roundtrip(srv.port, b"GET /healthz HTTP/1.1\r\n\r\n")
    assert _status(resp) == 200


# --------------------------------------------------------------- connection flood


async def test_connection_flood_stays_bounded_and_refuses_excess() -> None:
    """Fill every connection slot with slowloris connections, then flood beyond
    the ceiling: the in-flight count never exceeds the ceiling and the excess is
    refused deterministically (503)."""
    receiver = _bare_receiver()
    ceiling = receiver.limits.max_concurrent_connections
    held: list[asyncio.StreamWriter] = []
    async with _Server(receiver) as srv:
        # Occupy all slots with connections that stall in the parse phase.
        for _ in range(ceiling):
            _, writer = await asyncio.open_connection("127.0.0.1", srv.port)
            writer.write(b"POST /x HTTP/1.1\r\n")  # partial — holds the slot
            await writer.drain()
            held.append(writer)
        await asyncio.sleep(0.05)
        # Excess connections are refused without beginning to parse.
        statuses = []
        for _ in range(ceiling):
            resp = await _roundtrip(srv.port, b"", timeout=1.0)
            statuses.append(_status(resp))
        assert receiver._max_active <= ceiling
        assert any(s == 503 for s in statuses), statuses
        for writer in held:
            writer.close()


async def test_serves_a_healthy_request_under_partial_load() -> None:
    """With one free slot, a healthy request completes concurrently with load."""
    receiver = _bare_receiver()
    ceiling = receiver.limits.max_concurrent_connections
    held: list[asyncio.StreamWriter] = []
    async with _Server(receiver) as srv:
        for _ in range(ceiling - 1):
            _, writer = await asyncio.open_connection("127.0.0.1", srv.port)
            writer.write(b"POST /x HTTP/1.1\r\n")
            await writer.drain()
            held.append(writer)
        await asyncio.sleep(0.05)
        resp = await _roundtrip(srv.port, b"GET /healthz HTTP/1.1\r\n\r\n")
        assert _status(resp) == 200
        assert receiver._max_active <= ceiling
        for writer in held:
            writer.close()


# ----------------------------------------------------- disconnect cancels the turn


async def test_client_disconnect_mid_handler_cancels_the_turn() -> None:
    """The sync-reply cancellation path: a handler blocked awaiting a turn is
    cancelled when the client disconnects — no orphaned task, no leaked future."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_handler(
        method: str, path: str, headers: dict[str, str], raw_body: bytes
    ) -> tuple[int, dict[str, Any]]:
        started.set()
        try:
            await asyncio.Future()  # never resolves — models an in-flight turn
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return 200, {}  # pragma: no cover - unreachable

    receiver = BoundedHttpReceiver(blocking_handler, port=_free_port(), limits=TEST_LIMITS)
    async with _Server(receiver) as srv:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(b"POST /message HTTP/1.1\r\nContent-Length: 2\r\n\r\nhi")
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=2.0)
        # Client vanishes mid-turn.
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)
        _ = reader
    assert cancelled.is_set()


# ----------------------------------------------------------- raw-body byte-exactness


async def test_handler_receives_exact_raw_body_bytes() -> None:
    """No decode/re-encode drift: the bytes the handler sees equal the bytes
    sent (signature verification for webex/slack depends on this)."""
    captured: dict[str, bytes] = {}

    async def capturing(
        method: str, path: str, headers: dict[str, str], raw_body: bytes
    ) -> tuple[int, dict[str, Any]]:
        captured["body"] = raw_body
        return 200, {"len": len(raw_body)}

    receiver = BoundedHttpReceiver(capturing, port=_free_port(), limits=TEST_LIMITS)
    body = bytes(range(256))[:200]  # arbitrary non-UTF-8-clean bytes
    async with _Server(receiver) as srv:
        resp = await _roundtrip(
            srv.port,
            b"POST /message HTTP/1.1\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body,
        )
    assert _status(resp) == 200
    assert captured["body"] == body


# --------------------------------------------------- the adapters share the seam


def _build_adapters(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, BoundedHttpReceiver]]:
    dev = DevHttpAdapter(InProcessQueue(), port=_free_port())
    return [
        ("dev_http", dev._receiver),
    ]


async def test_adapters_share_the_bounded_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every HTTP-facing adapter's composed receiver enforces the shared bounds:
    an oversized request line is a deterministic 414 (proving the transport
    half of #125 is wired in the adapter, not just in the bare receiver)."""
    for name, rec in _build_adapters(monkeypatch):
        async with _Server(rec) as srv:
            line = b"GET /" + b"a" * (rec.limits.max_request_line_bytes + 50) + b" HTTP/1.1\r\n\r\n"
            resp = await _roundtrip(srv.port, line)
        assert _status(resp) == 414, name

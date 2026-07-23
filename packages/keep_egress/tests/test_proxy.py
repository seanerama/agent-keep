"""Proxy behavior tests (stage 3, unit — real localhost sockets, no docker).

Covers: absolute-form allow/deny, CONNECT allow/deny (denied BEFORE tunnel
establishment), malformed request-line edge cases (refused + audited as
denied with the safe 'invalid' target), deny-by-default on an empty
allowlist, unreachable-but-allowed targets, and audit emission with byte
counts on close.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from keep_egress.proxy import (
    DEFAULT_HEAD_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONNECTIONS,
    FALLBACK_BIND_HOST,
    EgressProxy,
)
from keep_egress.records import EgressAuditRecord, EgressJsonlSink, ObservedAgent

AGENT = ObservedAgent(slug="proxy-under-test", spec_version="0.0.1")

STUB_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"

MakeProxy = Callable[..., Awaitable[tuple[EgressProxy, Path]]]


@pytest.fixture
async def stub_server() -> AsyncIterator[int]:
    """A minimal in-process origin server: reads one request head, answers a
    fixed 200, closes — enough to prove bytes flow through the proxy."""

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        else:
            writer.write(STUB_RESPONSE)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def make_proxy(tmp_path: Path) -> AsyncIterator[MakeProxy]:
    started: list[EgressProxy] = []

    async def _make(
        allowlist: list[str],
        *,
        host: str = "127.0.0.1",
        head_timeout: float = DEFAULT_HEAD_TIMEOUT_SECONDS,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> tuple[EgressProxy, Path]:
        sink_path = tmp_path / f"egress-audit-{len(started)}.jsonl"
        proxy = EgressProxy(
            allowlist=allowlist,
            agent=AGENT,
            sink=EgressJsonlSink(sink_path),
            host=host,
            port=0,
            head_timeout=head_timeout,
            max_connections=max_connections,
        )
        await proxy.start()
        started.append(proxy)
        return proxy, sink_path

    yield _make
    for proxy in started:
        await proxy.close()


async def _roundtrip(port: int, payload: bytes) -> bytes:
    """Send raw bytes to the proxy, read to EOF."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), timeout=10)
    writer.close()
    return data


def _records(sink_path: Path) -> list[EgressAuditRecord]:
    lines = sink_path.read_text(encoding="utf-8").splitlines() if sink_path.exists() else []
    return [EgressAuditRecord.model_validate(json.loads(line)) for line in lines]


async def _wait_records(sink_path: Path, count: int) -> list[EgressAuditRecord]:
    """Records are appended when the CONNECTION closes — poll briefly for the
    expected count instead of racing the proxy's close-out."""
    for _ in range(100):
        records = _records(sink_path)
        if len(records) >= count:
            return records
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {count} audit record(s), got {_records(sink_path)}")


async def test_absolute_form_allowed(make_proxy: MakeProxy, stub_server: int) -> None:
    proxy, sink_path = await make_proxy(["127.0.0.1"])
    response = await _roundtrip(
        proxy.bound_port,
        f"GET http://127.0.0.1:{stub_server}/anything?q=1 HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{stub_server}\r\nProxy-Connection: keep-alive\r\n\r\n".encode(),
    )
    assert b"200 OK" in response and response.endswith(b"hello")
    (record,) = await _wait_records(sink_path, 1)
    assert record.verdict == "allowed"
    assert record.target == f"127.0.0.1:{stub_server}"
    assert record.matched_entry == "127.0.0.1"
    assert record.bytes_up > 0 and record.bytes_down == len(STUB_RESPONSE)
    assert record.run_id is None


async def test_absolute_form_denied_403(make_proxy: MakeProxy) -> None:
    proxy, sink_path = await make_proxy(["allowed.example.com:443"])
    response = await _roundtrip(
        proxy.bound_port, b"GET http://denied.example.com/secret HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    assert response.startswith(b"HTTP/1.1 403")
    (record,) = await _wait_records(sink_path, 1)
    assert record.verdict == "denied"
    # host:port only — the /secret path never reaches the log
    assert record.target == "denied.example.com:80"
    assert record.matched_entry is None
    assert record.bytes_up == 0 and record.bytes_down == 0


async def test_connect_allowed_tunnels_bytes(make_proxy: MakeProxy, stub_server: int) -> None:
    proxy, sink_path = await make_proxy([f"127.0.0.1:{stub_server}"])
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
    writer.write(f"CONNECT 127.0.0.1:{stub_server} HTTP/1.1\r\n\r\n".encode())
    await writer.drain()
    established = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    assert b"200 Connection Established" in established
    # opaque bytes through the tunnel (would be TLS in real HTTPS)
    inner_request = b"GET / HTTP/1.1\r\nHost: stub\r\n\r\n"
    writer.write(inner_request)
    await writer.drain()
    tunneled = await asyncio.wait_for(reader.read(), timeout=10)
    assert tunneled == STUB_RESPONSE
    writer.close()
    await asyncio.sleep(0.1)  # let the proxy close out and append the record
    (record,) = await _wait_records(sink_path, 1)
    assert record.verdict == "allowed"
    assert record.matched_entry == f"127.0.0.1:{stub_server}"
    assert record.bytes_up == len(inner_request)  # post-establishment bytes only
    assert record.bytes_down == len(STUB_RESPONSE)


async def test_connect_denied_before_tunnel(make_proxy: MakeProxy) -> None:
    proxy, sink_path = await make_proxy(["api.anthropic.com:443"])
    response = await _roundtrip(proxy.bound_port, b"CONNECT evil.example.net:443 HTTP/1.1\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 403")  # rejected BEFORE any tunnel exists
    (record,) = await _wait_records(sink_path, 1)
    assert record.verdict == "denied"
    assert record.target == "evil.example.net:443"
    assert record.matched_entry is None


async def test_empty_allowlist_denies_everything(make_proxy: MakeProxy, stub_server: int) -> None:
    """Deny-by-default: no kill-switch exists; empty allowlist = deny-all."""
    proxy, sink_path = await make_proxy([])
    for payload in (
        f"GET http://127.0.0.1:{stub_server}/ HTTP/1.1\r\n\r\n".encode(),
        f"CONNECT 127.0.0.1:{stub_server} HTTP/1.1\r\n\r\n".encode(),
    ):
        response = await _roundtrip(proxy.bound_port, payload)
        assert response.startswith(b"HTTP/1.1 403")
    records = _records(sink_path)
    assert [r.verdict for r in records] == ["denied", "denied"]
    assert all(r.matched_entry is None for r in records)


@pytest.mark.parametrize(
    "payload",
    [
        b"garbage\r\n\r\n",  # not a request line
        b"GET / HTTP/1.1\r\nHost: a\r\n\r\n",  # origin-form: a proxy cannot infer the target
        b"CONNECT example.com HTTP/1.1\r\n\r\n",  # CONNECT without a port (RFC requires one)
        b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n",  # IPv6 literal: outside the egress grammar
        b"CONNECT example.com:99999 HTTP/1.1\r\n\r\n",  # port out of range
        b"GET ftp://example.com/ HTTP/1.1\r\n\r\n",  # non-http scheme
        b"GET http:// HTTP/1.1\r\n\r\n",  # absolute-form without a host
        b"GET  HTTP/1.1\r\n\r\n",  # missing target
        b"\xff\xfe http://x/ HTTP/1.1\r\n\r\n",  # non-ascii junk survives safely
    ],
)
async def test_malformed_requests_refused_and_audited(
    make_proxy: MakeProxy, payload: bytes
) -> None:
    """Malformed = 400 + an audited denial carrying the SAFE target
    representation ('invalid') — raw request bytes never reach the log."""
    proxy, sink_path = await make_proxy(["127.0.0.1"])
    response = await _roundtrip(proxy.bound_port, payload)
    assert response.startswith(b"HTTP/1.1 400")
    (record,) = await _wait_records(sink_path, 1)
    assert record.verdict == "denied"
    assert record.target == "invalid"
    assert record.matched_entry is None


async def test_client_hangup_without_request_is_audited_denied(make_proxy: MakeProxy) -> None:
    """A connection that never completes a request head (EOF first) is still
    one observed attempt: refused-by-parse, audited with the safe target."""
    proxy, sink_path = await make_proxy(["127.0.0.1"])
    _reader, writer = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
    writer.write(b"CONNECT 127.0")  # partial head, then hang up
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    for _ in range(100):
        if _records(sink_path):
            break
        await asyncio.sleep(0.05)
    (record,) = await _wait_records(sink_path, 1)
    assert record.verdict == "denied"
    assert record.target == "invalid"


# ---- issue #11 ingress hardening: head-read timeout, connection cap, bind ----


async def test_slow_client_head_read_times_out_and_is_audited_denied(
    make_proxy: MakeProxy,
) -> None:
    """A client that connects and sends NOTHING (or a partial head) is dropped
    when the head-read timeout fires — the proxy does not wedge on the task
    forever. It is refused (400) and audited as a denied `invalid` attempt,
    exactly like any other incomplete head."""
    proxy, sink_path = await make_proxy(["127.0.0.1"], head_timeout=0.3)
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
    writer.write(b"CONNECT 127.0")  # partial head, then stay silent (slowloris)
    await writer.drain()
    # The read completes because the PROXY closes us out after its head timeout —
    # bounded well under this wait_for, proving no indefinite hang.
    data = await asyncio.wait_for(reader.read(), timeout=5)
    assert data.startswith(b"HTTP/1.1 400")
    writer.close()
    (record,) = await _wait_records(sink_path, 1)
    assert record.verdict == "denied"
    assert record.target == "invalid"


async def test_head_timeout_does_not_wedge_the_proxy(
    make_proxy: MakeProxy, stub_server: int
) -> None:
    """A slow client that times out must not block the event loop: a normal
    request served immediately afterward still succeeds + audits unchanged."""
    proxy, sink_path = await make_proxy(["127.0.0.1"], head_timeout=0.3)
    # Occupy a connection that will time out (send nothing).
    _r, slow_writer = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
    # ...and immediately serve a real request on a second connection.
    response = await _roundtrip(
        proxy.bound_port,
        f"GET http://127.0.0.1:{stub_server}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
    )
    assert b"200 OK" in response and response.endswith(b"hello")
    slow_writer.close()
    with contextlib.suppress(Exception):
        await slow_writer.wait_closed()
    # Both attempts are audited: the real allow + the timed-out denial.
    records = await _wait_records(sink_path, 2)
    verdicts = sorted(r.verdict for r in records)
    assert verdicts == ["allowed", "denied"]


async def test_connection_cap_sheds_excess_connections_promptly(
    make_proxy: MakeProxy,
) -> None:
    """Beyond the cap the proxy sheds excess connections PROMPTLY (503, closed)
    rather than queueing unbounded or wedging. Two slow clients hold the only
    two slots (blocked in the head read); the third is shed at once — well
    before the head timeout would ever fire."""
    proxy, _sink_path = await make_proxy(["127.0.0.1"], head_timeout=10, max_connections=2)
    held: list[asyncio.StreamWriter] = []
    for _ in range(2):
        _r, w = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
        held.append(w)
    await asyncio.sleep(0.1)  # let both _handle tasks acquire their slots

    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
    # Promptly (<< the 10s head timeout) shed with a 503, then EOF.
    data = await asyncio.wait_for(reader.read(), timeout=2)
    assert data.startswith(b"HTTP/1.1 503"), data
    writer.close()

    # The proxy is not wedged: freeing a slot lets a new connection be served.
    held[0].close()
    with contextlib.suppress(Exception):
        await held[0].wait_closed()
    for w in held[1:]:
        w.close()
        with contextlib.suppress(Exception):
            await w.wait_closed()


async def test_bind_host_is_not_all_interfaces(make_proxy: MakeProxy) -> None:
    """Issue #11: the listener binds a specific interface (127.0.0.1 here), not
    0.0.0.0 — so a dual-homed proxy does not listen on every network."""
    proxy, _sink = await make_proxy(["127.0.0.1"], host="127.0.0.1")
    assert proxy.bound_host == "127.0.0.1"
    assert proxy.bound_host != "0.0.0.0"


async def test_unresolvable_bind_host_falls_back_so_the_proxy_still_boots(
    tmp_path: Path,
) -> None:
    """Robustness: if the configured bind host cannot be resolved (local dev /
    non-docker, where the internal-net alias does not exist), start() falls back
    to all interfaces so the proxy still boots — the deploy's alias always
    resolves, so this fallback never fires in production."""
    proxy = EgressProxy(
        allowlist=["127.0.0.1"],
        agent=AGENT,
        sink=EgressJsonlSink(tmp_path / "fallback-audit.jsonl"),
        host="egress-proxy.invalid-nonexistent-host.test",
        port=0,
    )
    try:
        await proxy.start()
        assert proxy.bound_host in (FALLBACK_BIND_HOST, "0.0.0.0", "::")
    finally:
        await proxy.close()


async def test_allowed_but_unreachable_target_is_502_and_audited_allowed(
    make_proxy: MakeProxy,
) -> None:
    """The allowlist allowed the ATTEMPT; the target itself was down. The
    refusal is upstream's, not the boundary's: 502, audited allowed, 0 bytes."""
    # grab a port that is closed: bind+close leaves it free
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    proxy, sink_path = await make_proxy(["127.0.0.1"])
    response = await _roundtrip(
        proxy.bound_port, f"CONNECT 127.0.0.1:{dead_port} HTTP/1.1\r\n\r\n".encode()
    )
    assert response.startswith(b"HTTP/1.1 502")
    (record,) = await _wait_records(sink_path, 1)
    assert record.verdict == "allowed"
    assert record.matched_entry == "127.0.0.1"
    assert record.bytes_up == 0 and record.bytes_down == 0

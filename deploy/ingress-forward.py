"""Ingress-only TCP forwarder for the paired chassis (deploy machinery, ADR
0004 topology half — issue #11).

WHY THIS EXISTS. The worker and mechanic sit on the chassis's `--internal`
docker network so they have NO route to the internet except the audited egress
proxy (contract egress-observation; the Stage-3 physical boundary, `test_egress_
proxy.py::test_no_direct_route_out`). But `--internal` also blocks docker port
PUBLISHING (host -> container ingress needs a bridge that `--internal` firewalls
off), so the operator cannot reach the worker's dev-http surface from the host
to run the frozen `scripts/smoke-chat.sh` / `smoke-mechanic.sh` (they curl
`host:port`). This forwarder resolves that tension WITHOUT weakening the egress
boundary: it is dual-homed (host-published ingress leg + the internal net to
reach the worker/mechanic aliases) and relays host->worker and host->mechanic
ONLY. It gives the worker no pivot: the worker cannot instruct it to connect
anywhere but its FIXED targets, so no unaudited egress path is created — issue
#11's SECURITY intent (no unaudited co-resident egress) is fully preserved even
though the forwarder is a fourth container on the internal net.

It speaks no protocol — opaque byte relay — so it works for dev-http exactly as
docker's own `-p` would, and observes nothing (the audited plane is the proxy).

Usage:
    python ingress-forward.py <listen_port>:<target_host>:<target_port> [...]
e.g.
    python ingress-forward.py 8000:worker:8000 8001:mechanic:8000

stdlib-only (runs in the generic egress-proxy image, which ships python); no
third-party deps, non-root.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any


def _parse(spec: str) -> tuple[int, str, int]:
    listen_s, host, target_s = spec.split(":")
    return int(listen_s), host, int(target_s)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


def _make_handler(
    target_host: str, target_port: int
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Coroutine[Any, Any, None]]:
    async def handle(
        client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                target_host, target_port
            )
        except OSError:
            # Target not up yet (e.g. worker still booting) — drop this client;
            # the operator's smoke retries via /healthz polling.
            client_writer.close()
            return
        await asyncio.gather(
            _pipe(client_reader, upstream_writer),
            _pipe(upstream_reader, client_writer),
        )

    return handle


async def _main(specs: list[str]) -> None:
    servers = []
    for spec in specs:
        listen_port, target_host, target_port = _parse(spec)
        server = await asyncio.start_server(
            _make_handler(target_host, target_port), "0.0.0.0", listen_port
        )
        servers.append(server)
        print(f"ingress-forward: :{listen_port} -> {target_host}:{target_port}", flush=True)
    await asyncio.gather(*(server.serve_forever() for server in servers))


def main(argv: list[str]) -> int:
    specs = argv[1:]
    if not specs:
        print(__doc__, file=sys.stderr)
        return 64
    try:
        asyncio.run(_main(specs))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

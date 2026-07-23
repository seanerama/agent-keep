"""THE Stage-3 integration suite — the egress observation proxy, in the real
container topology (contract egress-observation v1, ADR 0002).

Topology under test (all hermetic — no real network egress is needed):

    [agent container] --(--internal docker network, no route out)--> [proxy]
    [stub  container] --(same internal network, alias stub.allowed.test)
    [proxy container] --(also connected to an egress-capable network:
                         dual-homed, exactly like the deploy)

The agent gets HTTP_PROXY/HTTPS_PROXY pointing at the proxy AND sits on an
`--internal` network with no default route — belt and suspenders; the env var
alone is never the boundary. Proven here:

(a) DENY path: from inside the agent container an outbound attempt to a
    non-allowlisted host is refused (HTTP 403 from the proxy) AND a denied
    `egress` audit record appears in the PROXY's own audit log.
(b) Hermetic ALLOW path: a stub HTTP server on the internal network under the
    allowlisted name (the fixture spec's sandbox.egress) succeeds through the
    proxy AND an allowed record with byte counts appears.
(c) CONNECT: allowed targets tunnel (rejected targets get 403 BEFORE any
    tunnel exists), byte counts on close.
(d) No-direct-route proof: bypassing the proxy env, the agent genuinely
    cannot reach out.
(e) The observably-works asset (scripts/smoke-egress.sh) passes against this
    topology, so it cannot rot before Stage 5 runs it live.

Requires a docker daemon (marked `container`; rides the existing CI container
job). The proxy allowlist arrives EXACTLY as it will in production: the spec
mounted read-only — the same source of truth build-time validation uses.
"""

import json
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from keep_egress.records import EgressAuditRecord

pytestmark = pytest.mark.container

REPO_ROOT = Path(__file__).parents[2]
CHATBOT_SPEC = REPO_ROOT / "specs" / "default-chatbot.yaml"
FIXTURE_SPEC = Path(__file__).parent / "specs" / "egress-proxy-test.yaml"
AGENT_IMAGE = "ghcr.io/seanerama/agent-keep-default-chatbot"
PROXY_IMAGE = "ghcr.io/seanerama/agent-keep-egress-proxy"

PROXY_ALIAS = "egress-proxy"
PROXY_PORT = 3128
STUB_ALIAS = "stub.allowed.test"
STUB_PORT = 8080
DENIED_HOST = "denied.example.com"
PROXY_AUDIT_PATH = "/var/lib/agent-keep/egress-audit.jsonl"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def _exec_py(container: str, script: str) -> subprocess.CompletedProcess[str]:
    """Run a python3 -c script inside a container (never check=True — tests
    assert on returncode/stdout explicitly)."""
    return _docker("exec", container, "python3", "-c", script, check=False)


@pytest.fixture(scope="session")
def proxy_image() -> str:
    """`keep-build build-proxy` — the real CLI, the real docker build."""
    subprocess.run(
        [sys.executable, "-m", "keep_build", "build-proxy"],
        check=True,
        cwd=REPO_ROOT,
    )
    return PROXY_IMAGE


@pytest.fixture(scope="session")
def agent_image() -> str:
    subprocess.run(
        [sys.executable, "-m", "keep_build", "build", str(CHATBOT_SPEC)],
        check=True,
        cwd=REPO_ROOT,
    )
    return AGENT_IMAGE


@pytest.fixture(scope="session")
def egress_networks() -> Iterator[tuple[str, str]]:
    """(internal, external): the agent's network is `--internal` — docker
    installs NO default route out of it. The external one exists solely to
    dual-home the proxy, mirroring the deploy topology."""
    suffix = uuid.uuid4().hex[:8]
    internal = f"keep-egress-int-{suffix}"
    external = f"keep-egress-ext-{suffix}"
    _docker("network", "create", "--internal", internal)
    _docker("network", "create", external)
    try:
        yield internal, external
    finally:
        _docker("network", "rm", internal, check=False)
        _docker("network", "rm", external, check=False)


@pytest.fixture(scope="session")
def proxy_container(
    proxy_image: str,
    egress_networks: tuple[str, str],
    hardened_run_flags: tuple[str, ...],
) -> Iterator[str]:
    """The proxy, dual-homed (internal + egress-capable), configured EXACTLY
    as the deploy will configure it: the fixture spec mounted read-only at
    /etc/agent-keep/spec.yaml. Never publishes a host port."""
    internal, external = egress_networks
    name = f"egress-proxy-it-{uuid.uuid4().hex[:8]}"
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        internal,
        "--network-alias",
        PROXY_ALIAS,
        "-v",
        f"{FIXTURE_SPEC}:/etc/agent-keep/spec.yaml:ro",
        *hardened_run_flags,
        proxy_image,
    )
    _docker("network", "connect", external, name)
    try:
        _wait_for_log(name, "egress proxy: observing agent 'egress-proxy-test'")
        yield name
    finally:
        if "-s" in sys.argv or "--capture=no" in sys.argv:
            logs = _docker("logs", name, check=False)
            print(logs.stdout, logs.stderr)
        _docker("rm", "-f", name, check=False)


@pytest.fixture(scope="session")
def stub_container(proxy_image: str, egress_networks: tuple[str, str]) -> Iterator[str]:
    """The hermetic ALLOW-path origin: a stdlib HTTP server in a container on
    the internal network, reachable ONLY under the allowlisted DNS alias.
    Reuses the proxy image purely as a python-bearing base — the entrypoint is
    overridden; nothing proxy-related runs here."""
    internal, _external = egress_networks
    name = f"egress-stub-it-{uuid.uuid4().hex[:8]}"
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        internal,
        "--network-alias",
        STUB_ALIAS,
        proxy_image,
        "python",
        "-m",
        "http.server",
        str(STUB_PORT),
        "-d",
        "/tmp",
    )
    try:
        yield name
    finally:
        _docker("rm", "-f", name, check=False)


@pytest.fixture(scope="session")
def agent_container(
    agent_image: str,
    egress_networks: tuple[str, str],
    proxy_container: str,
    sqlite_env: tuple[str, str],
    hardened_run_flags: tuple[str, ...],
) -> Iterator[str]:
    """The agent, booted for real (hardened flags, sqlite tier) on the
    internal-only network with proxy env pointing at the paired proxy — the
    Stage-3 composition. No ports published: the only way in is docker exec,
    the only way out is the proxy."""
    internal, _external = egress_networks
    name = f"egress-agent-it-{uuid.uuid4().hex[:8]}"
    image_id = _docker("image", "inspect", "-f", "{{.Id}}", agent_image).stdout.strip()
    proxy_url = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        internal,
        "-e",
        f"AGENT_IMAGE_DIGEST={image_id}",
        "-e",
        f"HTTP_PROXY={proxy_url}",
        "-e",
        f"HTTPS_PROXY={proxy_url}",
        "-e",
        f"http_proxy={proxy_url}",
        "-e",
        f"https_proxy={proxy_url}",
        "-e",
        "NO_PROXY=localhost,127.0.0.1",
        "-e",
        "no_proxy=localhost,127.0.0.1",
        *sqlite_env,
        *hardened_run_flags,
        agent_image,
    )
    try:
        _wait_agent_healthy(name)
        yield name
    finally:
        if "-s" in sys.argv or "--capture=no" in sys.argv:
            logs = _docker("logs", name, check=False)
            print(logs.stdout, logs.stderr)
        _docker("rm", "-f", name, check=False)


def _wait_for_log(name: str, needle: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = _docker("logs", name, check=False)
        if needle in logs.stdout:
            return
        time.sleep(0.5)
    logs = _docker("logs", name, check=False)
    raise AssertionError(
        f"container {name} never logged {needle!r}\n--- logs ---\n{logs.stdout}\n{logs.stderr}"
    )


def _wait_agent_healthy(name: str, timeout: float = 60.0) -> None:
    """No published ports on an internal network — health is proven from
    INSIDE (NO_PROXY covers localhost, so this never touches the proxy)."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = _exec_py(
            name,
            "import urllib.request;"
            "urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2);"
            "print('HEALTHY')",
        )
        if result.returncode == 0 and "HEALTHY" in result.stdout:
            return
        last = result.stderr
        time.sleep(0.5)
    logs = _docker("logs", name, check=False)
    raise AssertionError(
        f"agent never became healthy: {last}\n--- logs ---\n{logs.stdout}\n{logs.stderr}"
    )


def _proxy_audit_records(proxy_name: str) -> list[dict[str, object]]:
    raw = _docker("exec", proxy_name, "cat", PROXY_AUDIT_PATH, check=False).stdout
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _wait_audit_record(
    proxy_name: str,
    target: str,
    verdict: str,
    timeout: float = 15.0,
    action: str | None = None,
) -> dict[str, object]:
    """Newest matching record; the on-close `connect` record appends on
    connection CLOSE, so poll. `action` optionally narrows to `connect`/`open`
    (issue #24: an allowed CONNECT now yields BOTH an `open` and a `connect`
    record for the same target+verdict)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [
            record
            for record in _proxy_audit_records(proxy_name)
            if record["target"] == target
            and record["verdict"] == verdict
            and (action is None or record["action"] == action)
        ]
        if matches:
            return matches[-1]
        time.sleep(0.3)
    raise AssertionError(
        f"no egress audit record target={target!r} verdict={verdict!r} "
        f"action={action!r} in:\n"
        + "\n".join(json.dumps(r) for r in _proxy_audit_records(proxy_name))
    )


# --------------------------------------------------------------------- tests


def test_deny_path_refused_and_audited(agent_container: str, proxy_container: str) -> None:
    """(a) THE CI deny check: a non-allowlisted host is refused at the proxy
    (observable 403, surfacing as an ordinary HTTP failure in the agent) and
    the attempt lands in the proxy's audit log as a denied `egress` record."""
    result = _exec_py(
        agent_container,
        "import urllib.request, urllib.error\n"
        "try:\n"
        f"    urllib.request.urlopen('http://{DENIED_HOST}/', timeout=15)\n"
        "except urllib.error.HTTPError as e:\n"
        "    print('REFUSED', e.code)\n"
        "else:\n"
        "    print('UNEXPECTED-SUCCESS')\n",
    )
    assert result.returncode == 0, result.stderr
    assert "REFUSED 403" in result.stdout, result.stdout

    record = _wait_audit_record(proxy_container, f"{DENIED_HOST}:80", "denied")
    validated = EgressAuditRecord.model_validate(record)  # audit-record v1, kind `egress`
    assert validated.event == "egress" and validated.action == "connect"
    assert validated.matched_entry is None
    assert validated.run_id is None  # v1 proxy is not run-aware ("when attributable")
    assert validated.agent.slug == "egress-proxy-test"
    assert validated.bytes_up == 0 and validated.bytes_down == 0


def test_allow_path_hermetic_stub(
    agent_container: str, proxy_container: str, stub_container: str
) -> None:
    """(b) the allowlisted in-network stub succeeds THROUGH the proxy, and the
    allowed record carries byte counts."""
    result = _exec_py(
        agent_container,
        "import urllib.request\n"
        f"resp = urllib.request.urlopen('http://{STUB_ALIAS}:{STUB_PORT}/', timeout=15)\n"
        "body = resp.read()\n"
        "assert resp.status == 200, resp.status\n"
        "assert len(body) > 0\n"
        "print('ALLOWED', len(body))\n",
    )
    assert result.returncode == 0, result.stderr
    assert "ALLOWED" in result.stdout, result.stdout

    record = _wait_audit_record(proxy_container, f"{STUB_ALIAS}:{STUB_PORT}", "allowed")
    validated = EgressAuditRecord.model_validate(record)
    assert validated.matched_entry == f"{STUB_ALIAS}:{STUB_PORT}"  # the spec's entry, verbatim
    assert validated.bytes_up > 0
    assert validated.bytes_down > 0


def test_connect_tunnel_allowed_and_denied(
    agent_container: str, proxy_container: str, stub_container: str
) -> None:
    """(c) CONNECT semantics from inside the agent: allowlisted target tunnels
    opaque bytes; non-allowlisted target is 403'd BEFORE any tunnel exists."""
    result = _exec_py(
        agent_container,
        "import socket\n"
        f"s = socket.create_connection(('{PROXY_ALIAS}', {PROXY_PORT}), timeout=10)\n"
        f"s.sendall(b'CONNECT {STUB_ALIAS}:{STUB_PORT} HTTP/1.1\\r\\n\\r\\n')\n"
        "established = s.recv(1024)\n"
        "assert b'200 Connection Established' in established, established\n"
        f"s.sendall(b'GET / HTTP/1.0\\r\\nHost: {STUB_ALIAS}\\r\\n\\r\\n')\n"
        "data = b''\n"
        "while True:\n"
        "    chunk = s.recv(65536)\n"
        "    if not chunk:\n"
        "        break\n"
        "    data += chunk\n"
        "s.close()\n"
        "assert data.startswith(b'HTTP/1.0 200'), data[:64]\n"
        "d = socket.create_connection(('" + PROXY_ALIAS + f"', {PROXY_PORT}), timeout=10)\n"
        f"d.sendall(b'CONNECT {DENIED_HOST}:443 HTTP/1.1\\r\\n\\r\\n')\n"
        "refusal = d.recv(1024)\n"
        "assert refusal.startswith(b'HTTP/1.1 403'), refusal\n"
        "d.close()\n"
        "print('TUNNEL-OK')\n",
    )
    assert result.returncode == 0, result.stderr
    assert "TUNNEL-OK" in result.stdout, result.stdout

    # The on-close `connect` record carries the final bytes (an allowed CONNECT
    # now ALSO emits an earlier `open` record — narrow to `connect` for bytes).
    allowed = _wait_audit_record(
        proxy_container, f"{STUB_ALIAS}:{STUB_PORT}", "allowed", action="connect"
    )
    assert int(str(allowed["bytes_down"])) > 0
    denied = _wait_audit_record(proxy_container, f"{DENIED_HOST}:443", "denied")
    assert denied["matched_entry"] is None
    assert denied["action"] == "connect"  # denied refused before establish — no `open`


def test_connect_open_record_is_realtime_before_close(
    agent_container: str, proxy_container: str, stub_container: str
) -> None:
    """(issue #24 — THE point) An allowed CONNECT tunnel emits an `action: open`
    record IN REAL TIME at establish — readable from the proxy audit while the
    tunnel is still open, BEFORE the on-close `connect` record exists. Then on
    close the `connect` record with final bytes appears, sharing connection_id.

    Proven with a held-open tunnel: from inside the agent, establish CONNECT,
    push bytes, then HOLD the socket (sleep) without closing. While it is held,
    the proxy audit already shows the `open` record (bytes 0) and NO `connect`
    record for that connection (the pooled/held connection defers the close
    record — the exact bug this stage fixes). On close, the `connect` record
    lands with the same connection_id and non-zero bytes."""
    hold_seconds = 6
    script = (
        "import socket, time\n"
        f"s = socket.create_connection(('{PROXY_ALIAS}', {PROXY_PORT}), timeout=10)\n"
        f"s.sendall(b'CONNECT {STUB_ALIAS}:{STUB_PORT} HTTP/1.1\\r\\n\\r\\n')\n"
        "est = s.recv(1024)\n"
        "assert b'200 Connection Established' in est, est\n"
        f"s.sendall(b'GET / HTTP/1.0\\r\\nHost: {STUB_ALIAS}\\r\\n\\r\\n')\n"
        "data = s.recv(65536)\n"
        "assert data.startswith(b'HTTP/1.0 200'), data[:64]\n"
        "print('TUNNEL-HELD', flush=True)\n"
        f"time.sleep({hold_seconds})\n"
        "s.close()\n"
        "print('TUNNEL-CLOSED', flush=True)\n"
    )
    proc = subprocess.Popen(
        ["docker", "exec", agent_container, "python3", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    target = f"{STUB_ALIAS}:{STUB_PORT}"
    try:
        # Wait for the in-agent tunnel to be established + held open.
        assert proc.stdout is not None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if "TUNNEL-HELD" in line:
                break
            if proc.poll() is not None:
                raise AssertionError(f"holder exited early: {proc.communicate()}")
        else:
            raise AssertionError("tunnel never reported HELD")

        # REAL TIME: the `open` record is already readable while the tunnel is
        # still open — the whole point of the fix.
        opened = _wait_audit_record(proxy_container, target, "allowed", action="open")
        validated_open = EgressAuditRecord.model_validate(opened)
        assert validated_open.action == "open"
        assert validated_open.matched_entry == target
        assert validated_open.bytes_up == 0 and validated_open.bytes_down == 0
        cid = validated_open.connection_id

        # ...and the on-close `connect` record does NOT exist yet (deferred until
        # this held connection closes — eventually-consistent WITHOUT the open
        # record, which is exactly why issue #24 needs the real-time `open`).
        connects_now = [
            r
            for r in _proxy_audit_records(proxy_container)
            if r["target"] == target and r["action"] == "connect" and r["connection_id"] == cid
        ]
        assert connects_now == [], f"close record leaked before close: {connects_now}"
    finally:
        proc.wait(timeout=30)

    # On close, the `connect` record with final bytes lands, same connection_id.
    # Poll for THIS connection's close record specifically (the session-scoped
    # audit log holds other tests' connect records too).
    deadline = time.monotonic() + 15
    closed = None
    while time.monotonic() < deadline:
        matches = [
            r
            for r in _proxy_audit_records(proxy_container)
            if r["action"] == "connect" and r["connection_id"] == cid
        ]
        if matches:
            closed = matches[-1]
            break
        time.sleep(0.3)
    assert closed is not None, f"no close record for connection_id={cid}"
    validated_close = EgressAuditRecord.model_validate(closed)
    assert validated_close.target == target
    assert validated_close.verdict == "allowed"
    assert validated_close.connection_id == cid  # correlates the open+close pair
    assert validated_close.bytes_up > 0 and validated_close.bytes_down > 0


def test_no_direct_route_out(agent_container: str) -> None:
    """(d) belt AND suspenders: bypassing the proxy env vars entirely, the
    agent has no route out of the --internal network — a direct connection
    attempt fails at the network layer, not at the proxy."""
    result = _exec_py(
        agent_container,
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=5)\n"
        "except OSError as exc:\n"
        "    print('NO-ROUTE', type(exc).__name__)\n"
        "else:\n"
        "    print('ROUTE-EXISTS')\n",
    )
    assert result.returncode == 0, result.stderr
    assert "NO-ROUTE" in result.stdout, result.stdout
    assert "ROUTE-EXISTS" not in result.stdout


def test_proxy_binds_internal_interface_not_all(proxy_container: str) -> None:
    """(issue #11) The proxy binds the INTERNAL-net interface ONLY, not 0.0.0.0:
    KEEP_EGRESS_HOST defaults to the proxy's own internal-net alias
    (`egress-proxy`), which docker embedded DNS resolves to its internal-net IP
    alone — even though the proxy is dual-homed onto the egress net. So the
    control port listens on the internal interface and is NOT reachable from a
    co-resident container on the egress net, matching contract egress-observation
    §Exposes ('reachable ONLY from the paired agent'). Proven from INSIDE the
    container by reading its listening sockets."""
    result = _exec_py(
        proxy_container,
        "import socket, struct\n"
        f"want = socket.gethostbyname('{PROXY_ALIAS}')\n"
        "def ip(h):\n"
        "    return socket.inet_ntoa(struct.pack('<I', int(h, 16)))\n"
        "listen = []\n"
        "with open('/proc/net/tcp') as f:\n"
        "    next(f)\n"
        "    for line in f:\n"
        "        p = line.split()\n"
        "        addr_hex, port_hex = p[1].split(':')\n"
        f"        if int(port_hex, 16) == {PROXY_PORT} and p[3] == '0A':\n"
        "            listen.append(ip(addr_hex))\n"
        "print('WANT', want)\n"
        "print('LISTEN', listen)\n"
        "assert '0.0.0.0' not in listen, ('bound on all interfaces', listen)\n"
        "assert listen == [want], ('not bound to internal-net IP only', want, listen)\n"
        "print('BIND-OK')\n",
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "BIND-OK" in result.stdout, result.stdout


def test_proxy_audit_is_the_proxys_own_file(agent_container: str, proxy_container: str) -> None:
    """The proxy's audit file is its OWN append-only plane — the worker's
    audit.jsonl inside the agent container is a different file on a different
    filesystem (no write collision by construction)."""
    records = _proxy_audit_records(proxy_container)
    assert records, "proxy audit log is empty after the suite exercised it"
    assert all(record["event"] == "egress" for record in records)
    # and the agent container has no egress-audit file at all
    result = _docker(
        "exec", agent_container, "cat", "/var/lib/agent-keep/egress-audit.jsonl", check=False
    )
    assert result.returncode != 0


def test_smoke_egress_script_passes(agent_container: str, proxy_container: str) -> None:
    """(e) the observably-works asset passes against the running topology —
    the same script the Operator runs in Stage 5's live smoke."""
    result = subprocess.run(
        [
            str(REPO_ROOT / "scripts" / "smoke-egress.sh"),
            agent_container,
            f"docker:{proxy_container}",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"smoke-egress.sh failed (rc={result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "SMOKE PASS" in result.stdout

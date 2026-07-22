"""THE Stage-5 CI-side proof of the DEPLOY topology (distinct from the live
smoke, which the Operator runs over the unreachable tailnet).

CI cannot reach the NSAF dev server, but it CAN stand the FULL paired chassis up
locally with the same images `keep-build` bakes and prove the deploy topology
end to end — the honest local evidence that the systemd unit
(deploy/systemd/agent-keep@.service) wires a real, working, egress-bounded
chassis. Stage-3 (test_egress_proxy) proved worker+proxy; Stage-4
(test_mechanic_container) proved worker+mechanic; NEITHER stood up ALL of
worker + proxy + mechanic + ingress on the two-network topology at once. This
module does, and runs all THREE frozen smoke scripts against it:

  worker (default-chatbot)  -> agent-keep net (--internal, alias `worker`), NO
                               route out except HTTP(S)_PROXY -> the proxy; audit
                               single-file-bound into the shared bundle dir.
  egress-proxy              -> internal (alias egress-proxy) + external
                               (dual-homed); the fixture spec mounted read-only;
                               own audit on its own file.
  mechanic                  -> internal only (alias `mechanic`), the bundle dir
                               mounted READ-ONLY; own audit on its own tmpfs.
  ingress-forwarder         -> external (host-published) + internal; the real
                               deploy/ingress-forward.py relay, mapping
                               127.0.0.1:<wport>->worker:8000 and
                               127.0.0.1:<mport>->mechanic:8000.

Proven:
(a) smoke-chat.sh passes against the worker THROUGH the forwarder (dev-http
    round-trip + a run-correlated model_call audit line in the bundle);
(b) smoke-egress.sh passes: a non-allowlisted host is refused AND a denied
    `egress` record lands in the proxy's own audit log — the DENY half of the
    boundary the live smoke proves against the real provider;
(c) smoke-mechanic.sh passes against the mechanic THROUGH the forwarder (cited
    reply + the mechanic's own run-correlated audit line);
(d) the worker has NO direct route out (bypassing the proxy env fails at the
    network layer) — the physical boundary the forwarder does NOT weaken.

Hermetic: the worker + mechanic run the STATIC provider (no key, no real
network) — this is the "prove the pipe + prove the DENY path" deploy the runbook
calls the first live step; the real-Anthropic ALLOW path is the Operator's live
smoke (a key + the tailnet), which CI structurally cannot run. Requires a docker
daemon (marked `container`); reuses the session hardened-flags fixture.
"""

import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.container

REPO_ROOT = Path(__file__).parents[2]
WORKER_SPEC = REPO_ROOT / "specs" / "default-chatbot.yaml"
FIXTURE_SPEC = Path(__file__).parent / "specs" / "egress-proxy-test.yaml"
RELAY = REPO_ROOT / "deploy" / "ingress-forward.py"
SCRIPTS = REPO_ROOT / "scripts"

WORKER_IMAGE = "ghcr.io/seanerama/agent-keep-default-chatbot"
PROXY_IMAGE = "ghcr.io/seanerama/agent-keep-egress-proxy"
MECHANIC_IMAGE = "ghcr.io/seanerama/agent-keep-mechanic"
WORKER_SLUG = "default-chatbot"

WORKER_AUDIT_PATH = "/var/lib/agent-keep/audit.jsonl"
BUNDLE_MOUNT = "/srv/worker-bundle"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http_ok(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last = exc
        time.sleep(0.5)
    raise AssertionError(f"{url} never became ready: {last}")


def _wait_for_log(name: str, needle: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = _docker("logs", name, check=False)
        if needle in logs.stdout or needle in logs.stderr:
            return
        time.sleep(0.5)
    logs = _docker("logs", name, check=False)
    raise AssertionError(f"{name} never logged {needle!r}\n{logs.stdout}\n{logs.stderr}")


# ------------------------------------------------------------------ images/fixtures


@pytest.fixture(scope="module")
def images() -> tuple[str, str, str]:
    subprocess.run([sys.executable, "-m", "keep_build", "build-proxy"], check=True, cwd=REPO_ROOT)
    for spec in (WORKER_SPEC, REPO_ROOT / "specs" / "mechanic.yaml"):
        subprocess.run(
            [sys.executable, "-m", "keep_build", "build", str(spec)], check=True, cwd=REPO_ROOT
        )
    return WORKER_IMAGE, PROXY_IMAGE, MECHANIC_IMAGE


@pytest.fixture(scope="module")
def bundle_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The Stage-4 bundle arrangement the helper `prep-bundle` reproduces."""
    directory = tmp_path_factory.mktemp("paired-bundle")
    directory.chmod(0o755)
    spec_copy = directory / f"{WORKER_SLUG}.yaml"
    shutil.copyfile(WORKER_SPEC, spec_copy)
    spec_copy.chmod(0o644)
    audit = directory / f"{WORKER_SLUG}.audit.jsonl"
    audit.touch()
    audit.chmod(0o666)
    return directory


@pytest.fixture(scope="module")
def topology(
    images: tuple[str, str, str],
    bundle_dir: Path,
    hardened_run_flags: tuple[str, ...],
) -> Iterator[dict[str, object]]:
    """Stand up the FULL deploy topology: two networks + proxy + mechanic +
    ingress + worker, mirroring deploy/systemd/agent-keep@.service."""
    worker_image, proxy_image, mechanic_image = images
    suffix = uuid.uuid4().hex[:8]
    net = f"keep-paired-net-{suffix}"  # --internal
    egress = f"keep-paired-egress-{suffix}"
    names: list[str] = []
    wport, mport = _free_port(), _free_port()

    def drop_keep_tmpfs(flags: list[str]) -> list[str]:
        idx = flags.index("/var/lib/agent-keep:mode=1777")
        del flags[idx - 1 : idx + 1]
        return flags

    try:
        _docker("network", "create", "--internal", net)
        _docker("network", "create", egress)

        # proxy: dual-homed, spec ro, own audit on its own file (tmpfs ok in CI)
        proxy = f"keep-proxy-{suffix}"
        names.append(proxy)
        _docker(
            "run",
            "-d",
            "--name",
            proxy,
            "--network",
            net,
            "--network-alias",
            "egress-proxy",
            "-v",
            f"{FIXTURE_SPEC}:/etc/agent-keep/spec.yaml:ro",
            *hardened_run_flags,
            proxy_image,
        )
        _docker("network", "connect", egress, proxy)
        _wait_for_log(proxy, "egress proxy: observing agent")

        # mechanic: internal only, bundle ro, own audit on its own tmpfs
        mechanic = f"keep-mechanic-{suffix}"
        names.append(mechanic)
        mech_id = _docker("image", "inspect", "-f", "{{.Id}}", mechanic_image).stdout.strip()
        _docker(
            "run",
            "-d",
            "--name",
            mechanic,
            "--network",
            net,
            "--network-alias",
            "mechanic",
            "-e",
            f"AGENT_IMAGE_DIGEST={mech_id}",
            "-e",
            "SQLITE_PATH=/tmp/agent-keep-mechanic-sessions.sqlite3",
            "-e",
            f"MECHANIC_WORKER_DIR={BUNDLE_MOUNT}",
            "-v",
            f"{bundle_dir}:{BUNDLE_MOUNT}:ro",
            *hardened_run_flags,
            mechanic_image,
        )

        # ingress forwarder: the REAL relay, host-published, then joins -net
        ingress = f"keep-ingress-{suffix}"
        names.append(ingress)
        _docker(
            "run",
            "-d",
            "--name",
            ingress,
            "--network",
            egress,
            "-p",
            f"127.0.0.1:{wport}:8000",
            "-p",
            f"127.0.0.1:{mport}:8001",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--read-only",
            "--tmpfs",
            "/tmp:mode=1777",
            "-v",
            f"{RELAY}:/ingress-forward.py:ro",
            proxy_image,
            "python",
            "/ingress-forward.py",
            "8000:worker:8000",
            "8001:mechanic:8000",
        )
        _docker("network", "connect", net, ingress)

        # worker: internal ONLY, proxy env, audit single-file bind into the bundle
        worker = f"keep-worker-{suffix}"
        names.append(worker)
        worker_id = _docker("image", "inspect", "-f", "{{.Id}}", worker_image).stdout.strip()
        proxy_url = "http://egress-proxy:3128"
        worker_flags = drop_keep_tmpfs(list(hardened_run_flags))
        _docker(
            "run",
            "-d",
            "--name",
            worker,
            "--network",
            net,
            "--network-alias",
            "worker",
            "-e",
            f"AGENT_IMAGE_DIGEST={worker_id}",
            "-e",
            "SQLITE_PATH=/tmp/agent-keep-sessions.sqlite3",
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
            *worker_flags,
            "-v",
            f"{bundle_dir / f'{WORKER_SLUG}.audit.jsonl'}:{WORKER_AUDIT_PATH}",
            worker_image,
        )

        # Health via the forwarder-published host ports (the operator's path).
        _wait_http_ok(f"http://127.0.0.1:{wport}/healthz")
        _wait_http_ok(f"http://127.0.0.1:{mport}/healthz")
        yield {
            "worker": worker,
            "proxy": proxy,
            "mechanic": mechanic,
            "ingress": ingress,
            "wport": wport,
            "mport": mport,
        }
    finally:
        for name in names:
            if "-s" in sys.argv or "--capture=no" in sys.argv:
                logs = _docker("logs", name, check=False)
                print(name, logs.stdout, logs.stderr)
            _docker("rm", "-f", name, check=False)
        _docker("network", "rm", net, check=False)
        _docker("network", "rm", egress, check=False)


def _run_smoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPTS / args[0]), *args[1:]],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=150,
    )


# --------------------------------------------------------------------------- tests


def test_smoke_chat_through_the_ingress_forwarder(topology: dict[str, object]) -> None:
    """(a) dev-http round-trip reaches the --internal worker via the forwarder;
    the run-correlated model_call audit line lands in the shared bundle file."""
    result = _run_smoke(
        "smoke-chat.sh",
        f"127.0.0.1:{topology['wport']}",
        f"docker:{topology['worker']}",
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "SMOKE PASS" in result.stdout


def test_smoke_egress_deny_path_audited(topology: dict[str, object]) -> None:
    """(b) the DENY half of the boundary — a non-allowlisted host is refused at
    the proxy and a denied `egress` record lands in the proxy's audit log."""
    result = _run_smoke(
        "smoke-egress.sh",
        str(topology["worker"]),
        f"docker:{topology['proxy']}",
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "SMOKE PASS" in result.stdout


def test_smoke_mechanic_through_the_ingress_forwarder(topology: dict[str, object]) -> None:
    """(c) the mechanic (also --internal) answers a cited question via the
    forwarder, its own run-correlated audit line written on its own plane."""
    # Drive one worker message first so the bundle has audit lines to cite.
    _run_smoke("smoke-chat.sh", f"127.0.0.1:{topology['wport']}")
    result = _run_smoke(
        "smoke-mechanic.sh",
        f"127.0.0.1:{topology['mport']}",
        f"docker:{topology['mechanic']}",
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "SMOKE PASS" in result.stdout


def test_worker_has_no_direct_route_out(topology: dict[str, object]) -> None:
    """(d) belt AND suspenders: bypassing the proxy env, the worker cannot reach
    the internet — the forwarder does NOT give it a route out."""
    result = _docker(
        "exec",
        str(topology["worker"]),
        "python3",
        "-c",
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=5)\n"
        "except OSError as exc:\n"
        "    print('NO-ROUTE', type(exc).__name__)\n"
        "else:\n"
        "    print('ROUTE-EXISTS')\n",
        check=False,
    )
    assert "NO-ROUTE" in result.stdout, result.stdout
    assert "ROUTE-EXISTS" not in result.stdout

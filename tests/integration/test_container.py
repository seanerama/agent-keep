"""THE Stage-2 integration test — the container CI job's substance.

keep-build build -> docker run (hardened) -> /healthz -> POST /message, then
assert:
(a) the deterministic static-provider reply,
(b) an audit-record v1 JSONL line for the model call carrying THE
    run-correlation key (trigger.message_id == the reply's message_id —
    contract audit-record, 2026-07-14 amendment) with real token accounting,
(c) the container process runs as non-root (uid 10001),
(d) absence semantics — unselected components (anthropic_provider,
    local_tools, single_session, ...) are NOT in the image, and the tool
    layer (executor + approval endpoints) is genuinely absent, not disabled.

No mocks inside the container. Requires a docker daemon (marked `container`;
CI runs it as a dedicated job — it is never skipped in the default CI path).
Hermetic: the static provider makes no network call and needs no secret.
"""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from keep_build.composer import image_fs_scan_script

pytestmark = pytest.mark.container

REPO_ROOT = Path(__file__).parents[2]
CHATBOT_SPEC = REPO_ROOT / "specs" / "default-chatbot.yaml"
IMAGE = "ghcr.io/seanerama/agent-keep-default-chatbot"
EXPECTED_REPLY = "Hello from the Agent Keep default chatbot. The chassis is live."


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


@pytest.fixture(scope="session")
def built_image() -> str:
    """`keep-build build specs/default-chatbot.yaml` — the real CLI, the real
    docker build."""
    subprocess.run(
        [sys.executable, "-m", "keep_build", "build", str(CHATBOT_SPEC)],
        check=True,
        cwd=REPO_ROOT,
    )
    return IMAGE


@pytest.fixture(scope="session")
def container(
    built_image: str, sqlite_env: tuple[str, str], hardened_run_flags: tuple[str, ...]
) -> Iterator[tuple[str, int, str]]:
    port = _free_port()
    name = f"default-chatbot-it-{uuid.uuid4().hex[:8]}"
    image_id = _docker("image", "inspect", "-f", "{{.Id}}", built_image).stdout.strip()
    # Boot the runner UNDER the hardened deploy flags (--read-only, --cap-drop
    # ALL, no-new-privileges, pids/memory ceilings) with the same writable
    # mounts the deploy grants. If a flag or a missing writable mount broke
    # the runtime, /healthz below would never come up — that is the proof.
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:8000",
        "-e",
        f"AGENT_IMAGE_DIGEST={image_id}",
        *sqlite_env,  # the sqlite tier is real; boot refuses without it
        *hardened_run_flags,
        built_image,
    )
    try:
        yield name, port, image_id
    finally:
        if "-s" in sys.argv or "--capture=no" in sys.argv:
            logs = _docker("logs", name, check=False)
            print(logs.stdout, logs.stderr)
        _docker("rm", "-f", name, check=False)


def _wait_healthz(port: int, name: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.5)
    logs = _docker("logs", name, check=False)
    raise AssertionError(
        f"/healthz never became ready: {last_error}\n--- container logs ---\n"
        f"{logs.stdout}\n{logs.stderr}"
    )


@pytest.fixture(scope="session")
def exercised(container: tuple[str, int, str]) -> dict[str, object]:
    """Wait for /healthz, POST one real message, return the reply payload."""
    name, port, _image_id = container
    _wait_healthz(port, name)
    body = json.dumps(
        {"text": "hello, chassis", "conversation_id": "it-1", "sender_id": "integration-test"}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/message",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        assert resp.status == 200
        payload: dict[str, object] = json.loads(resp.read())
    return payload


def test_deterministic_static_reply(exercised: dict[str, object]) -> None:
    """(a) the static provider's scripted reply comes back, exactly."""
    assert exercised["reply"] == EXPECTED_REPLY


def test_audit_line_carries_run_correlation_key(
    container: tuple[str, int, str], exercised: dict[str, object]
) -> None:
    """(b) an audit-record v1 line exists for the model call, and its
    trigger.message_id IS the run-correlation key: equal to the internal
    message id the round-trip returned (one run = one activation — the
    heartbeat/audit join is a key equality, never a timestamp heuristic)."""
    name, _port, image_id = container
    raw = _docker("exec", name, "cat", "/var/lib/agent-keep/audit.jsonl").stdout
    lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
    model_calls = [record for record in lines if record["event"] == "model_call"]
    assert model_calls, f"no model_call audit record found in: {raw!r}"
    record = model_calls[-1]
    assert record["trigger"] is not None
    assert record["trigger"]["message_id"] == exercised["message_id"]
    assert record["trigger"]["trigger_id"] is None
    assert record["trigger"]["purpose"]
    assert record["agent"]["slug"] == "default-chatbot"
    # EQUALITY with the digest the fixture injected — no sentinel masquerade:
    # the runner refuses to start rather than fake this field.
    assert record["agent"]["image_digest"] == image_id
    assert record["action"]["name"] == "static"
    assert record["outcome"]["status"] == "ok"
    # Token/budget accounting is ON (spec models.budgets): real counts.
    assert record["cost"]["tokens_in"] > 0 and record["cost"]["tokens_out"] > 0


def test_container_runs_as_non_root(container: tuple[str, int, str]) -> None:
    """(c) the container process uid == 10001 (never 0)."""
    name, _port, _image_id = container
    uid = _docker("exec", name, "id", "-u").stdout.strip()
    assert uid != "0"
    assert uid == "10001"


def test_unselected_components_are_absent_from_image(built_image: str) -> None:
    """(d) absence grep: components the spec did not select are NOT in the
    image — genuinely absent, not disabled. anthropic_provider is the sharp
    case: it exists in the repo's component library (the live flip needs it)
    but the static-provider spec leaves it OUT of the image."""
    absent_modules = ["anthropic_provider", "local_tools", "worker_analyzer", "single_session"]
    for module in absent_modules:
        # the module genuinely exists in the component library (repo side)...
        repo_module = (
            REPO_ROOT / "packages/agent_runtime/src/agent_runtime/components" / f"{module}.py"
        )
        assert repo_module.is_file(), f"{module} missing from the repo component library"

        # ...but importing it inside the image must fail,
        result = _docker(
            "run",
            "--rm",
            built_image,
            "python",
            "-c",
            f"import agent_runtime.components.{module}",
            check=False,
        )
        assert result.returncode != 0, f"{module} was importable inside the image"
        assert "ModuleNotFoundError" in result.stderr

    # ...and no trace of any of them exists anywhere in the image filesystem
    # (the shared scan script — one implementation, not hand-copied snippets).
    scan = _docker(
        "run",
        "--rm",
        built_image,
        "python",
        "-c",
        image_fs_scan_script(absent_modules),
        check=False,
    )
    assert scan.returncode == 0, f"unselected-component traces found in image: {scan.stdout}"

    # the selected components ARE present (sanity check of the same mechanism)
    ok = _docker(
        "run",
        "--rm",
        built_image,
        "python",
        "-c",
        (
            "import agent_runtime.components.dev_http, agent_runtime.components.memory_queue, "
            "agent_runtime.components.model_router, agent_runtime.components.sqlite_persistence, "
            "agent_runtime.components.static_provider, agent_runtime.components.jsonl_audit"
        ),
        check=False,
    )
    assert ok.returncode == 0, ok.stderr

    # absence applies to libraries too: the static-only image ships no httpx.
    freeze = _docker("run", "--rm", built_image, "python", "-m", "pip", "freeze", check=False)
    installed = {line.split("==")[0].lower() for line in freeze.stdout.splitlines() if "==" in line}
    assert "httpx" not in installed, "httpx present in a static-provider-only image"


def test_tool_layer_is_absent_from_tool_less_image(
    built_image: str, sqlite_env: tuple[str, str], hardened_run_flags: tuple[str, ...]
) -> None:
    """(d) the default chatbot grants no tools, so the image contains NO
    executor module and NO pending-approval endpoints — absent, not disabled."""
    result = _docker(
        "run", "--rm", built_image, "python", "-c", "import agent_runtime.executor", check=False
    )
    assert result.returncode != 0, "executor was importable inside the tool-less image"
    assert "ModuleNotFoundError" in result.stderr

    scan = _docker(
        "run",
        "--rm",
        built_image,
        "python",
        "-c",
        image_fs_scan_script(["executor", "gateway"], roots=("/app",)),
        check=False,
    )
    assert scan.returncode == 0, f"tool/gateway traces found in tool-less image: {scan.stdout}"

    # ...and the approval endpoints genuinely do not exist at runtime either
    port = _free_port()
    name = f"default-chatbot-absence-{uuid.uuid4().hex[:8]}"
    image_id = _docker("image", "inspect", "-f", "{{.Id}}", built_image).stdout.strip()
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:8000",
        "-e",
        f"AGENT_IMAGE_DIGEST={image_id}",
        *sqlite_env,
        *hardened_run_flags,
        built_image,
    )
    try:
        _wait_healthz(port, name)
        request = urllib.request.Request(f"http://127.0.0.1:{port}/pending", method="GET")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 404
    finally:
        _docker("rm", "-f", name, check=False)


def test_smoke_chat_script_passes_against_the_container(
    container: tuple[str, int, str], exercised: dict[str, object]
) -> None:
    """The observably-works asset (`scripts/smoke-chat.sh`) passes against the
    running container — the same script the Operator runs post-deploy in
    Stage 5, proven here so it cannot rot. The `docker:<name>` audit source
    exercises the new-audit-line assertion end to end."""
    name, port, _image_id = container
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "smoke-chat.sh"), f"127.0.0.1:{port}", f"docker:{name}"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"smoke-chat.sh failed (rc={result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "SMOKE PASS" in result.stdout

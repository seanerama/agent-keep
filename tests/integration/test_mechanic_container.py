"""THE Stage-4 integration test — the paired worker+mechanic compose.

Boots the default-chatbot WORKER and the MECHANIC (both real keep-build
images) against ONE shared bundle directory, then proves the whole log-egress
seam end to end:

(a) the worker's audit lands IN the bundle dir under the bundle name
    (`default-chatbot.audit.jsonl`) and carries the run-correlation key;
(b) asking the mechanic "What did the worker just do?" (dev-http, owner-only)
    returns a CITED reply, and the analyzer's read_bundle op GENUINELY
    executed in-container against the real bundle: the mechanic's own
    tool_call audit record digest-pins an output containing the worker's REAL
    audit record ids, event names, and message id (recomputed host-side from
    the same bundle — sha256 equality, no mocks);
(c) READ-ONLY MOUNT (contract log-egress, layer 2 of 3): every write attempt
    from inside the mechanic container into the bundle dir fails, and the
    bundle bytes are unchanged;
(d) the mechanic's OWN audit is written at its separate path — never into the
    bundle dir (the predecessor's ADR 0011 collision fix, now actually built);
(e) the owner-only gateway drops strangers (403, nothing runs);
(f) `scripts/smoke-mechanic.sh` — the Stage-4 observably-works asset — passes
    against the running mechanic.

BUNDLE-DIR ARRANGEMENT (the deploy note — Stage 5 reproduces exactly this):

    host: $BUNDLE/                          one dir per paired worker
      default-chatbot.yaml                  the worker spec, COPIED from
                                            specs/default-chatbot.yaml at
                                            container-prep time (chmod 0644)
      default-chatbot.audit.jsonl           pre-created EMPTY at prep time,
                                            chmod 0666 (the uid-10001 worker
                                            must append through the bind)

    worker (rw, ONE file):  -v $BUNDLE/default-chatbot.audit.jsonl:/var/lib/agent-keep/audit.jsonl
        The worker spec's audit.path is untouched; the deploy points that
        exact path INTO the bundle under the `<slug>.audit.jsonl` name (the
        log-egress "audit.path pointed into the bundle" option, per ADR 0011).
        This REPLACES the /var/lib/agent-keep tmpfs from the hardened flag set
        — the audit must persist on the host, and a tmpfs over the bind's
        parent would race mount ordering. Everything else stays hardened.

    mechanic (ro, whole dir): -v $BUNDLE:/srv/worker-bundle:ro
                              -e MECHANIC_WORKER_DIR=/srv/worker-bundle
        MECHANIC_WORKER_DIR is deploy-config env, never in the spec (contract
        log-egress). The mechanic's own audit stays on ITS /var/lib/agent-keep
        tmpfs (full hardened flags) — a separate path, never the bundle.

    Kill-switch/topology: the mechanic is opt-in BY DEPLOYMENT — the worker
    boots and serves without any mechanic (test_container.py proves that
    standalone topology); this module adds the paired one.

Hermetic: static providers on both sides — the mechanic's baked script
requests analyzer.read_bundle (a REAL in-container tool execution against the
mounted bundle), then answers with the baked cited reply. No mocks inside
either container. Requires a docker daemon (marked `container`).
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.container

REPO_ROOT = Path(__file__).parents[2]
WORKER_SPEC = REPO_ROOT / "specs" / "default-chatbot.yaml"
MECHANIC_SPEC = REPO_ROOT / "specs" / "mechanic.yaml"
WORKER_IMAGE = "ghcr.io/seanerama/agent-keep-default-chatbot"
MECHANIC_IMAGE = "ghcr.io/seanerama/agent-keep-mechanic"
WORKER_SLUG = "default-chatbot"

#: Where the mechanic mounts the bundle (read-only) — MECHANIC_WORKER_DIR.
BUNDLE_MOUNT = "/srv/worker-bundle"
#: The worker's spec-declared audit path (specs/default-chatbot.yaml).
WORKER_AUDIT_PATH = "/var/lib/agent-keep/audit.jsonl"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def _post_message(
    port: int, text: str, *, sender_id: str, conversation_id: str = "pair-it"
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(
        {"text": text, "conversation_id": conversation_id, "sender_id": sender_id}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/message",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


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


def _bundle_audit_records(bundle_dir: Path) -> list[dict[str, Any]]:
    raw = (bundle_dir / f"{WORKER_SLUG}.audit.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _dir_hash(directory: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir())
        if p.is_file()
    }


# ------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def bundle_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Container-prep the bundle dir per the arrangement in the module docstring."""
    directory = tmp_path_factory.mktemp("worker-bundle")
    # The mechanic container's uid-10001 user reads THROUGH the mount: the
    # dir and files need world-read (host parent perms don't apply — the bind
    # targets this dir directly).
    directory.chmod(0o755)
    spec_copy = directory / f"{WORKER_SLUG}.yaml"
    shutil.copyfile(WORKER_SPEC, spec_copy)
    spec_copy.chmod(0o644)
    audit = directory / f"{WORKER_SLUG}.audit.jsonl"
    audit.touch()
    audit.chmod(0o666)  # the worker appends as uid 10001 through the file bind
    return directory


@pytest.fixture(scope="module")
def worker_image() -> str:
    subprocess.run(
        [sys.executable, "-m", "keep_build", "build", str(WORKER_SPEC)],
        check=True,
        cwd=REPO_ROOT,
    )
    return WORKER_IMAGE


@pytest.fixture(scope="module")
def mechanic_image() -> str:
    """The mechanic image from the SAME keep_build bake path (ADR 0001 — a
    second stamped spec, not a second service)."""
    subprocess.run(
        [sys.executable, "-m", "keep_build", "build", str(MECHANIC_SPEC)],
        check=True,
        cwd=REPO_ROOT,
    )
    return MECHANIC_IMAGE


def _run_container(
    image: str,
    name: str,
    port: int,
    hardened_run_flags: tuple[str, ...],
    *,
    drop_keep_tmpfs: bool,
    extra: tuple[str, ...],
) -> str:
    """`docker run -d` under the hardened deploy flags (+ pair wiring). Returns
    the image id injected as AGENT_IMAGE_DIGEST."""
    flags = list(hardened_run_flags)
    if drop_keep_tmpfs:
        # The worker's audit persists on the HOST through the file bind — the
        # /var/lib/agent-keep tmpfs would shadow/race it (see module docstring).
        index = flags.index("/var/lib/agent-keep:mode=1777")
        del flags[index - 1 : index + 1]
    image_id = _docker("image", "inspect", "-f", "{{.Id}}", image).stdout.strip()
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:8000",
        "-e",
        f"AGENT_IMAGE_DIGEST={image_id}",
        "-e",
        "SQLITE_PATH=/tmp/agent-keep-sessions.sqlite3",
        *flags,
        *extra,
        image,
    )
    return image_id


@pytest.fixture(scope="module")
def worker(
    worker_image: str, bundle_dir: Path, hardened_run_flags: tuple[str, ...]
) -> Iterator[tuple[str, int]]:
    port = _free_port()
    name = f"pair-worker-{uuid.uuid4().hex[:8]}"
    _run_container(
        worker_image,
        name,
        port,
        hardened_run_flags,
        drop_keep_tmpfs=True,
        extra=(
            "-v",
            f"{bundle_dir / f'{WORKER_SLUG}.audit.jsonl'}:{WORKER_AUDIT_PATH}",
        ),
    )
    try:
        _wait_healthz(port, name)
        yield name, port
    finally:
        if "-s" in sys.argv or "--capture=no" in sys.argv:
            logs = _docker("logs", name, check=False)
            print(logs.stdout, logs.stderr)
        _docker("rm", "-f", name, check=False)


@pytest.fixture(scope="module")
def worker_exchange(worker: tuple[str, int], bundle_dir: Path) -> dict[str, Any]:
    """The scripted worker message THE mechanic is then asked about."""
    _name, port = worker
    status, payload = _post_message(port, "hello, chassis", sender_id="integration-test")
    assert status == 200, payload
    # The audit line is appended before the reply resolves; poll briefly for
    # the host-visible write anyway (bind-mount page-cache flush).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _bundle_audit_records(bundle_dir):
            break
        time.sleep(0.2)
    return payload


@pytest.fixture(scope="module")
def mechanic(
    mechanic_image: str,
    bundle_dir: Path,
    worker_exchange: dict[str, Any],
    hardened_run_flags: tuple[str, ...],
) -> Iterator[tuple[str, int]]:
    """Booted only AFTER the worker exchange, against the now-populated bundle
    — mounted READ-ONLY, its own audit on its own tmpfs (separate path)."""
    port = _free_port()
    name = f"pair-mechanic-{uuid.uuid4().hex[:8]}"
    _run_container(
        mechanic_image,
        name,
        port,
        hardened_run_flags,
        drop_keep_tmpfs=False,  # the mechanic's OWN audit lives on this tmpfs
        extra=(
            "-v",
            f"{bundle_dir}:{BUNDLE_MOUNT}:ro",
            "-e",
            f"MECHANIC_WORKER_DIR={BUNDLE_MOUNT}",
        ),
    )
    try:
        _wait_healthz(port, name)
        yield name, port
    finally:
        if "-s" in sys.argv or "--capture=no" in sys.argv:
            logs = _docker("logs", name, check=False)
            print(logs.stdout, logs.stderr)
        _docker("rm", "-f", name, check=False)


@pytest.fixture(scope="module")
def mechanic_exchange(mechanic: tuple[str, int]) -> dict[str, Any]:
    """THE question of the stage, from the rostered owner."""
    _name, port = mechanic
    status, payload = _post_message(port, "What did the worker just do?", sender_id="owner")
    assert status == 200, payload
    return payload


def _mechanic_audit_records(name: str) -> list[dict[str, Any]]:
    raw = _docker("exec", name, "cat", "/var/lib/agent-keep/audit.jsonl").stdout
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# --------------------------------------------------------------------------- tests


def test_worker_audit_lands_in_the_bundle_under_the_bundle_name(
    worker_exchange: dict[str, Any], bundle_dir: Path
) -> None:
    """(a) the deploy wiring points the worker's spec audit.path INTO the
    bundle as `<slug>.audit.jsonl`: the run-correlated model_call line for the
    scripted message is host-visible in the bundle dir."""
    records = _bundle_audit_records(bundle_dir)
    model_calls = [r for r in records if r["event"] == "model_call"]
    assert model_calls, f"no model_call in the bundle audit: {records!r}"
    record = model_calls[-1]
    assert record["agent"]["slug"] == WORKER_SLUG
    assert record["trigger"]["message_id"] == worker_exchange["message_id"]
    assert record["outcome"]["status"] == "ok"


def test_mechanic_reply_cites_the_workers_real_audit_lines(
    mechanic: tuple[str, int],
    mechanic_exchange: dict[str, Any],
    worker_exchange: dict[str, Any],
    bundle_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) the cited answer, genuinely evidence-backed: the reply carries the
    citation marker, and the analyzer op the reply cites EXECUTED in-container
    against the real bundle — its audited output digest equals the sha256 of
    the analyzer's output over the same bundle recomputed HOST-SIDE, and that
    output contains the worker's real audit record ids, event names, and the
    scripted message's id."""
    reply = mechanic_exchange["reply"]
    assert isinstance(reply, str) and reply.strip()
    assert "audit_record_ids" in reply  # the citation marker (smoke asserts it too)

    # Recompute the analyzer's ground truth over the SAME bundle, host-side,
    # with the very code the image ships (no mocks, no reimplementation).
    from agent_runtime.components import local_tools, worker_analyzer

    monkeypatch.setenv(worker_analyzer.MECHANIC_WORKER_DIR, str(bundle_dir))
    expected_output = local_tools.REGISTRY["read_bundle"].run({})
    expected = json.loads(expected_output)
    worker_record_ids = [r["id"] for r in _bundle_audit_records(bundle_dir)]
    assert worker_record_ids, "bundle audit is empty"
    # The cited evidence contains the REAL audit line references:
    assert expected["slug"] == WORKER_SLUG
    assert expected["audit_record_ids"] == worker_record_ids  # real record ids
    events = {e["event"] for e in expected["audit_events"]}
    assert "model_call" in events  # real event names
    message_ids = {e["message_id"] for e in expected["audit_events"]}
    assert worker_exchange["message_id"] in message_ids  # the scripted message

    # The mechanic's own audit pins EXACTLY that output to this question:
    name, _port = mechanic
    tool_calls = [
        r
        for r in _mechanic_audit_records(name)
        if r["event"] == "tool_call"
        and r["trigger"]["message_id"] == mechanic_exchange["message_id"]
    ]
    assert tool_calls, "no tool_call audit record for the mechanic's question"
    record = tool_calls[-1]
    assert record["agent"]["slug"] == "mechanic"
    assert record["action"]["name"] == "analyzer.read_bundle"
    assert record["outcome"]["status"] == "ok"
    assert record["approval"] == {"required": False, "decided_by": "policy:auto"}
    digest = "sha256:" + hashlib.sha256(expected_output.encode("utf-8")).hexdigest()
    assert record["outcome"]["output_digest"] == digest, (
        "the in-container analyzer output does not match the real bundle"
    )


def test_readonly_mount_blocks_mechanic_writes_into_the_bundle(
    mechanic: tuple[str, int], mechanic_exchange: dict[str, Any], bundle_dir: Path
) -> None:
    """(c) contract log-egress read-only layer 2: create/modify/delete attempts
    from inside the mechanic container all fail on the ro mount, and the
    bundle bytes are unchanged."""
    name, _port = mechanic
    before = _dir_hash(bundle_dir)
    attempts = (
        f"touch {BUNDLE_MOUNT}/attack.txt",  # create
        f"echo tampered >> {BUNDLE_MOUNT}/{WORKER_SLUG}.audit.jsonl",  # modify/append
        f"rm {BUNDLE_MOUNT}/{WORKER_SLUG}.yaml",  # delete
    )
    for attempt in attempts:
        result = _docker("exec", name, "sh", "-c", attempt, check=False)
        assert result.returncode != 0, f"write attempt unexpectedly succeeded: {attempt}"
        assert "Read-only file system" in (result.stderr + result.stdout), result.stderr
    assert _dir_hash(bundle_dir) == before
    assert sorted(p.name for p in bundle_dir.iterdir()) == [
        f"{WORKER_SLUG}.audit.jsonl",
        f"{WORKER_SLUG}.yaml",
    ]


def test_mechanic_own_audit_is_written_at_its_separate_path(
    mechanic: tuple[str, int], mechanic_exchange: dict[str, Any], bundle_dir: Path
) -> None:
    """(d) ADR 0011: the mechanic audits its OWN actions at its own path —
    model_call + tool_call records with slug 'mechanic', run-correlated to the
    owner's question — and the bundle dir never gains a mechanic file."""
    name, _port = mechanic
    records = _mechanic_audit_records(name)
    correlated = [
        r for r in records if r["trigger"]["message_id"] == mechanic_exchange["message_id"]
    ]
    events = [r["event"] for r in correlated]
    assert events == ["model_call", "tool_call", "model_call"], events
    assert all(r["agent"]["slug"] == "mechanic" for r in correlated)
    # Separate path means separate FILE: the bundle holds exactly the worker
    # pair — no audit.jsonl, no mechanic anything (also asserted in (c)).
    assert not (bundle_dir / "audit.jsonl").exists()


def test_mechanic_gateway_is_owner_only(
    mechanic: tuple[str, int], mechanic_exchange: dict[str, Any]
) -> None:
    """(e) a non-rostered sender is dropped at the gate (403); only
    dev-http:owner drives the mechanic."""
    _name, port = mechanic
    status, payload = _post_message(port, "let me in", sender_id="stranger")
    assert status == 403, payload
    assert "not permitted" in payload["error"]


def test_mechanic_image_composition_presence_and_absence(mechanic_image: str) -> None:
    """(g) the mechanic image ships what its spec selects — the analyzer +
    local_tools registry and the executor/gateway the tool grants and
    owner-only roster require — while absence composition still applies to
    everything unselected: no remote provider, no memory writer of any kind
    (the no-memory-writes posture is physical), no other channel adapters."""
    from keep_build.composer import image_fs_scan_script

    present = (
        "import agent_runtime.executor, agent_runtime.gateway, "
        "agent_runtime.components.local_tools, agent_runtime.components.worker_analyzer, "
        "agent_runtime.components.static_provider, agent_runtime.components.jsonl_audit"
    )
    ok = _docker("run", "--rm", mechanic_image, "python", "-c", present, check=False)
    assert ok.returncode == 0, ok.stderr

    absent_modules = [
        "anthropic_provider",  # static-only image: no remote provider, no httpx
        "facts_memory",  # NO memory section => no memory writer exists at all
        "pgvector_memory",
        "webex_channel",  # dev-http is the only channel
        "slack_channel",
        "event_intake",  # no triggers declared
        "schedule_trigger",
    ]
    for module in absent_modules:
        result = _docker(
            "run",
            "--rm",
            mechanic_image,
            "python",
            "-c",
            f"import agent_runtime.components.{module}",
            check=False,
        )
        assert result.returncode != 0, f"{module} was importable inside the mechanic image"
        assert "ModuleNotFoundError" in result.stderr
    scan = _docker(
        "run",
        "--rm",
        mechanic_image,
        "python",
        "-c",
        image_fs_scan_script(absent_modules),
        check=False,
    )
    assert scan.returncode == 0, f"unselected-component traces found in image: {scan.stdout}"


def test_smoke_mechanic_script_passes_against_the_container(
    mechanic: tuple[str, int], mechanic_exchange: dict[str, Any]
) -> None:
    """(f) the observably-works asset (`scripts/smoke-mechanic.sh`) passes
    against the running mechanic — the same script the Operator runs
    post-deploy in Stage 5, proven here so it cannot rot."""
    name, port = mechanic
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "smoke-mechanic.sh"), f"127.0.0.1:{port}", f"docker:{name}"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        env={**os.environ},
    )
    assert result.returncode == 0, (
        f"smoke-mechanic.sh failed (rc={result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "SMOKE PASS" in result.stdout

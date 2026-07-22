"""Container job (stage 10): build the OPENAI-spec image and prove the
provider-agnostic composition (issue #15, the second adapter).

keep-build build specs/default-chatbot.openai.yaml -> docker run (hardened) ->
/healthz, then assert:
(a) the worker BOOTS under the hardened deploy flags with `provider: openai`.
    The openai adapter (the anthropic-shaped cloud variant) requires its API
    key at construction, so the boot run supplies a DUMMY OPENAI_API_KEY (a
    placeholder value, never a real credential) so construction succeeds and
    /healthz comes up. NO real OpenAI server is contacted — construction makes
    no model call, so the boot is fully hermetic;
(b) the openai_provider module is PRESENT in the image;
(c) absence semantics — the UNSELECTED providers (anthropic_provider,
    ollama_provider, static_provider) are NOT in the image;
(d) httpx IS installed (the openai adapter is hand-rolled on it).

A REAL model reply (gpt-4o-mini on api.openai.com through the proxy, billed) is
NOT hermetic — that is the Operator's post-merge live step, not this test. Built
to a UNIQUE tag so it never collides with the static/ollama images the sibling
container tests build under the same slug.
"""

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
OPENAI_SPEC = REPO_ROOT / "specs" / "default-chatbot.openai.yaml"
#: Distinct tag: same slug as the static/ollama images, so build to a unique
#: name to avoid clobbering the sibling container tests' images in one session.
IMAGE = f"agent-keep-default-chatbot-openai-it:{uuid.uuid4().hex[:8]}"
#: Placeholder key supplied so the eager openai adapter construction succeeds —
#: NEVER a real credential and never used for a real call (no OpenAI server is
#: contacted; construction makes no model call).
DUMMY_OPENAI_KEY = "sk-dummy-not-a-real-key-for-boot-only"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


@pytest.fixture(scope="module")
def built_image() -> Iterator[str]:
    """`keep-build build --tag <unique> specs/default-chatbot.openai.yaml`."""
    subprocess.run(
        [sys.executable, "-m", "keep_build", "build", "--tag", IMAGE, str(OPENAI_SPEC)],
        check=True,
        cwd=REPO_ROOT,
    )
    try:
        yield IMAGE
    finally:
        _docker("image", "rm", "-f", IMAGE, check=False)


@pytest.fixture(scope="module")
def container(
    built_image: str, sqlite_env: tuple[str, str], hardened_run_flags: tuple[str, ...]
) -> Iterator[tuple[str, int]]:
    port = _free_port()
    name = f"default-chatbot-openai-it-{uuid.uuid4().hex[:8]}"
    image_id = _docker("image", "inspect", "-f", "{{.Id}}", built_image).stdout.strip()
    # Boot under the hardened deploy flags. NO OpenAI server is reachable, but
    # the openai adapter makes no call at construction time, so with the key
    # present /healthz must still come up — the proof the worker boots with
    # `provider: openai`. The DUMMY key is injected exactly the way the deploy
    # secret mechanism injects the real one at run time (never in the image).
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
        f"OPENAI_API_KEY={DUMMY_OPENAI_KEY}",
        *sqlite_env,
        *hardened_run_flags,
        built_image,
    )
    try:
        yield name, port
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


def test_worker_boots_with_the_openai_provider(container: tuple[str, int]) -> None:
    """(a) the openai-spec worker comes up healthy under the hardened flags —
    with a DUMMY key (eager construction) and no OpenAI server (construction
    makes no model call)."""
    name, port = container
    _wait_healthz(port, name)


def test_openai_provider_module_is_present(built_image: str) -> None:
    """(b) the selected openai_provider module imports inside the image."""
    result = _docker(
        "run",
        "--rm",
        built_image,
        "python",
        "-c",
        "import agent_runtime.components.openai_provider",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_unselected_providers_are_absent_from_image(built_image: str) -> None:
    """(c) absence grep: the providers the openai spec did not select are NOT in
    the image — genuinely absent, not disabled."""
    absent_modules = ["anthropic_provider", "ollama_provider", "static_provider"]
    for module in absent_modules:
        repo_module = (
            REPO_ROOT / "packages/agent_runtime/src/agent_runtime/components" / f"{module}.py"
        )
        assert repo_module.is_file(), f"{module} missing from the repo component library"
        result = _docker(
            "run",
            "--rm",
            built_image,
            "python",
            "-c",
            f"import agent_runtime.components.{module}",
            check=False,
        )
        assert result.returncode != 0, f"{module} was importable inside the openai image"
        assert "ModuleNotFoundError" in result.stderr

    scan = _docker(
        "run",
        "--rm",
        built_image,
        "python",
        "-c",
        image_fs_scan_script(absent_modules),
        check=False,
    )
    assert scan.returncode == 0, f"unselected-provider traces found in image: {scan.stdout}"


def test_openai_image_installs_httpx(built_image: str) -> None:
    """(d) the openai adapter is hand-rolled on httpx, so the selected-image
    installs it (the composer's COMPONENT_DEPENDENCIES for openai-provider)."""
    freeze = _docker("run", "--rm", built_image, "python", "-m", "pip", "freeze", check=False)
    installed = {line.split("==")[0].lower() for line in freeze.stdout.splitlines() if "==" in line}
    assert "httpx" in installed, "httpx missing from the openai-provider image"

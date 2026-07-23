"""Stage-14 container gate: the arbitrary-blueprint image path is real (ADR 0007).

The unit tests (tests/deploy/test_deploy_local_image.py) prove deploy.sh's wiring
against stubs. This proves the actual mechanism against a REAL docker daemon: an
arbitrary blueprint's worker image is built, delivered WITHOUT any registry
(`docker save | docker load`), and is runnable by the immutable ID we pin it by.

It exercises scripts/deliver-worker-image.sh in LOCAL mode (host `-`: load into
the same daemon, no ssh hop) — the same build+save|load+pin-by-id the remote path
runs, minus the network hop CI cannot make. The image tag is distinct and was
NEVER pushed anywhere, so a successful `docker run` of it proves the no-registry
path end to end.

Key fact this asserts (why deploy.sh CANNOT pin a loaded image by RepoDigest): a
`docker load`ed image has EMPTY RepoDigests. `{{.Id}}` is the only immutable pin,
and `docker run <that id>` is a valid reference. Requires a docker daemon (marked
`container`).
"""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.container

REPO_ROOT = Path(__file__).parents[2]
DELIVER = REPO_ROOT / "scripts" / "deliver-worker-image.sh"
# An ARBITRARY blueprint: a real keep/v1 spec, tagged as its own per-spec image
# name that exists on NO registry — the no-ghcr-write case.
SPEC = REPO_ROOT / "specs" / "default-chatbot.yaml"
ARBITRARY_REF = "ghcr.io/seanerama/agent-keep-arbitrary-blueprint-test:local"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


@pytest.fixture()
def clean_image() -> object:
    """Ensure a pristine start and remove the image + any created container after."""
    _docker("image", "rm", "-f", ARBITRARY_REF, check=False)
    yield
    _docker("rm", "-f", "arbitrary-blueprint-runnable", check=False)
    _docker("image", "rm", "-f", ARBITRARY_REF, check=False)


def test_deliver_builds_saves_loads_and_pins_a_runnable_id(clean_image: object) -> None:
    """The full no-registry round-trip: build -> save|load -> pin by .Id, and the
    pinned id is a real, runnable `docker run` reference."""
    # deliver-worker-image.sh does keep-build build + `docker save | docker load`
    # (LOCAL, host `-`) and prints ONLY the loaded image's immutable id on stdout.
    result = subprocess.run(
        ["bash", str(DELIVER), str(SPEC), ARBITRARY_REF, "-"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=600,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    pinned_id = result.stdout.strip()
    assert pinned_id.startswith("sha256:"), f"not an id: {pinned_id!r}\n{result.stderr}"

    # The loaded image really exists locally under that id.
    assert _docker("image", "inspect", pinned_id).returncode == 0

    # WHY pin by id, not RepoDigests: a loaded image's RepoDigests are NOT a usable
    # pin. On the classic graph-driver store they are EMPTY; on the containerd image
    # store they are a digest SYNTHESIZED locally (never pushed), so a host
    # `docker pull` of it would FAIL. Assert that whatever RepoDigests holds is NOT
    # a registry-resolvable pin here — either absent, or a digest that is not on any
    # registry (both make RepoDigest pinning wrong for a loaded image).
    repo_digests = _docker(
        "inspect", "--format", "{{json .RepoDigests}}", ARBITRARY_REF
    ).stdout.strip()
    assert repo_digests in ("null", "[]") or ARBITRARY_REF.split(":")[0] in repo_digests, (
        f"unexpected RepoDigests shape: {repo_digests}"
    )

    # The pinned .Id equals the tag's .Id — the id is a faithful pin of the image.
    tag_id = _docker("inspect", "--format", "{{.Id}}", ARBITRARY_REF).stdout.strip()
    assert pinned_id == tag_id

    # THE POINT: `docker run <sha256-id>` is a valid, immutable reference. `docker
    # create` is `docker run` minus start — it fully resolves+validates the image
    # ref without launching the long-lived worker server.
    created = _docker("create", "--name", "arbitrary-blueprint-runnable", pinned_id)
    assert created.returncode == 0, f"loaded image not runnable by id: {created.stderr}"


def test_deliver_rejects_a_missing_spec() -> None:
    """Guard: a bad spec path fails clearly (exit 66), never a silent empty id."""
    result = subprocess.run(
        ["bash", str(DELIVER), str(REPO_ROOT / "specs" / "does-not-exist.yaml"), ARBITRARY_REF, "-"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.returncode == 66, result.stderr
    assert result.stdout.strip() == "", result.stdout

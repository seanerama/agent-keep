"""Stage-15 walking-skeleton proof (ADR 0007, "two inputs → deployed").

THE capstone evidence: a SINGLE `scripts/deploy-agent.sh <blueprint> LOCAL`
invocation — no manual steps between the two inputs — builds the worker image
from the blueprint, stands the audited paired topology (worker + egress-proxy +
mechanic + ingress) up on the local docker daemon, and a smoke passes.

CI cannot reach a real remote host and a bare local daemon has no systemd, so —
exactly as the sanctioned CI pattern (tests/integration/test_paired_topology.py)
does — the LOCAL path composes the trio directly instead of driving deploy.sh's
systemd engine. The deploy.sh-invocation wiring (the ssh path) is covered by the
stub-ssh unit tests in tests/deploy/test_deploy_agent.py.

Hermetic: the default chatbot runs the STATIC provider (no key, no real network),
so this proves the pipe end to end with no secret. Requires a docker daemon
(marked `container`).
"""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.container

REPO_ROOT = Path(__file__).parents[2]
DEPLOY_AGENT = REPO_ROOT / "scripts" / "deploy-agent.sh"
SPEC = REPO_ROOT / "specs" / "default-chatbot.yaml"
SLUG = "default-chatbot"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


@pytest.fixture()
def clean_worker_image() -> object:
    """Remove the locally-built worker tag before AND after, so the run proves the
    no-registry build+load path from scratch and leaves nothing behind."""
    ref = f"ghcr.io/seanerama/agent-keep-{SLUG}:local"
    _docker("image", "rm", "-f", ref, check=False)
    yield
    _docker("image", "rm", "-f", ref, check=False)


def test_deploy_agent_local_stands_up_the_chassis_and_smokes(clean_worker_image: object) -> None:
    """One invocation: (blueprint, LOCAL) → audited paired topology + SMOKE PASS.

    deploy-agent.sh tears the trio down on exit, so this asserts the single
    invocation SUCCEEDS end to end (build+load → topology → liveness → smoke) —
    the observably-works acceptance for the north star.
    """
    result = subprocess.run(
        ["bash", str(DEPLOY_AGENT), str(SPEC), "LOCAL"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=900,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
    # the frozen smoke passed against the worker stood up from the built+loaded image
    assert "SMOKE PASS" in result.stdout, result.stdout
    # the entry point reports a clear DEPLOYED line with the derived slug
    assert f"DEPLOYED: {SLUG} on LOCAL" in result.stdout, result.stdout
    # slug was derived from the blueprint (operator passed only spec + target)
    assert f"slug={SLUG}" in result.stdout, result.stdout

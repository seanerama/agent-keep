"""Stage-6 CI-side gate for issue #13 Defect 2: the deploy MUST reject a
non-running egress-proxy.

The proxy is the audited egress boundary. It is launched fire-and-forget by the
systemd unit (`ExecStartPre=docker run -d`, returns 0 on detach); the old
deploy.sh verify step only curled worker + mechanic /healthz, so a proxy that
crashed at boot (the empty-KEEP_EGRESS_PORT ValueError) still read as "deploy
verified live". The fix is scripts/assert-proxy-running.sh, piped over ssh in
deploy.sh's verify step. deploy.sh itself is bash that runs over a live ssh
connection to a tailnet host CI cannot reach, so the honest, portable proof is
to exercise the EXTRACTED liveness helper directly with a stubbed `docker` on
PATH: it is the exact command deploy.sh runs on the host.

(The one-line wiring in deploy.sh — piping this script over ssh — is covered by
shellcheck and by the render/topology gates; its runtime behaviour against a
real dead proxy is what test_paired_topology's proxy-liveness assertion guards.)

Pure Python / pytest, no docker (default marker — runs in the `test` job).
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
HELPER = REPO_ROOT / "scripts" / "assert-proxy-running.sh"


def _write_fake_docker(dir_path: Path, running_value: str, *, exit_code: int = 0) -> None:
    """Drop a fake `docker` on PATH whose `inspect` mimics State.Running output.

    `running_value` is what `docker inspect -f '{{.State.Running}}' <name>` prints
    (e.g. "true" for a live container, "false" for a crashed one); `exit_code`
    lets us mimic docker's non-zero exit for an ABSENT container.
    """
    fake = dir_path / "docker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "inspect" ]; then\n'
        f"  printf '%s\\n' '{running_value}'\n"
        f"  exit {exit_code}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_helper(fake_bin: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    # Keep the test fast: one attempt, no settle sleep.
    env["PROXY_LIVENESS_ATTEMPTS"] = "1"
    env["PROXY_LIVENESS_INTERVAL"] = "0"
    return subprocess.run(
        ["bash", str(HELPER), "agent-keep-default-chatbot-proxy"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_running_proxy_passes(tmp_path: Path) -> None:
    """A container reporting State.Running=true → helper exits 0 (deploy proceeds)."""
    _write_fake_docker(tmp_path, "true")
    result = _run_helper(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "is running" in result.stdout


def test_crashed_proxy_fails_the_deploy(tmp_path: Path) -> None:
    """A container reporting State.Running=false (crashed after detach) → non-zero,
    with a clear message pointing at `docker logs`. This is the regression: on the
    old deploy.sh this state read as 'verified live'."""
    _write_fake_docker(tmp_path, "false")
    result = _run_helper(tmp_path)
    assert result.returncode != 0
    assert "NOT running" in result.stderr
    assert "docker logs agent-keep-default-chatbot-proxy" in result.stderr


def test_absent_proxy_fails_the_deploy(tmp_path: Path) -> None:
    """An ABSENT container (docker inspect exits non-zero, no output) → non-zero;
    `set -e` inside the helper must not be tripped by the failing inspect."""
    _write_fake_docker(tmp_path, "", exit_code=1)
    result = _run_helper(tmp_path)
    assert result.returncode != 0
    assert "NOT running" in result.stderr

"""Stage-13 gate: scripts/bootstrap-host.sh renders the sudoers with the ACTUAL
ssh user (no literal `<deploy-user>` installed) and drives the host in the right
order — the risky, security-relevant part of the "two inputs → deployed"
bootstrap (ADR 0007).

bootstrap-host.sh drives a real host over ssh/scp; a fresh host is unreachable in
CI, so — exactly like tests/deploy/test_deploy_secret_injection.py — we put a
fake `ssh` (records each remote command to a log) and a fake `scp` (captures the
transferred files) on PATH and assert:
  - the `<deploy-user>` placeholder is substituted to the real ssh user BEFORE
    install (the CAPTURED sudoers has the real user, never the literal token);
  - helper install → sudoers install → `visudo -c` are driven in that order;
  - a re-run is idempotent (docker install is SKIPPED when docker is present);
  - a fresh install DOES run the docker convenience script;
  - a missing <ssh-target> arg fails non-zero with usage.

Pure Python / pytest, no docker, no network, no real host (default marker — the
`test` job).
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap-host.sh"

FAKE_USER = "deploybot"


def _write_stub_bin(dir_path: Path, *, docker_present: bool) -> tuple[Path, Path]:
    """Fake `ssh` + `scp` on PATH.

    `ssh` appends the remote command (its last arg) to ssh.log and fakes the two
    outputs the script consumes: `whoami` → the fake user, `command -v docker` →
    exit 0/1 per `docker_present`. Everything else exits 0.

    `scp` copies every local-file argument into a capture dir so the test can read
    back the sudoers bytes that were actually transferred.

    Returns (ssh.log, capture_dir).
    """
    log = dir_path / "ssh.log"
    capture = dir_path / "captured"
    capture.mkdir()
    docker_rc = 0 if docker_present else 1
    ssh = dir_path / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        # last arg is the remote command string; earlier args are host/opts/-t
        'cmd="${*: -1}"\n'
        f'printf "%s\\n" "$cmd" >> "{log}"\n'
        # login derivation
        'if [[ "$cmd" == "whoami" ]]; then\n'
        f'  echo "{FAKE_USER}"\n'
        "  exit 0\n"
        "fi\n"
        # docker presence probe
        'if [[ "$cmd" == *"command -v docker"* ]]; then\n'
        f"  exit {docker_rc}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    scp = dir_path / "scp"
    scp.write_text(
        "#!/usr/bin/env bash\n"
        # capture every local file arg (the dest arg has a ':' so it is skipped)
        'for a in "$@"; do\n'
        '  if [ -f "$a" ]; then cp "$a" "'
        f"{capture}"
        '/"; fi\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for f in (ssh, scp):
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return log, capture


def _run_bootstrap(
    bin_dir: Path, *, target: str | None = "op@fresh-host"
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    args = ["bash", str(BOOTSTRAP)]
    if target is not None:
        args.append(target)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )


def _cmds(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _captured_sudoers(capture: Path) -> str:
    """The transferred sudoers file — identified by its rule signature."""
    for f in capture.iterdir():
        text = f.read_text(encoding="utf-8")
        if "ALL=(root) NOPASSWD" in text:
            return text
    raise AssertionError(f"no sudoers file captured in {capture}: {list(capture.iterdir())}")


def test_deploy_user_substituted_before_install(tmp_path: Path) -> None:
    """The installed sudoers must carry the REAL ssh user, never the literal
    `<deploy-user>` placeholder — the security-critical substitution."""
    log, capture = _write_stub_bin(tmp_path, docker_present=True)
    result = _run_bootstrap(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    sudoers = _captured_sudoers(capture)
    assert f"{FAKE_USER} ALL=(root) NOPASSWD: /usr/local/sbin/agent-keep-deploy" in sudoers
    assert "<deploy-user>" not in sudoers


def test_install_and_visudo_driven_in_order(tmp_path: Path) -> None:
    """The staged sudoers is validated (`visudo -cf`) BEFORE it is installed to
    /etc/sudoers.d/ (a malformed drop-in there can break sudo host-wide), then
    helper install → sudoers install → full `visudo -c` recheck."""
    log, _ = _write_stub_bin(tmp_path, docker_present=True)
    result = _run_bootstrap(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    blob = "\n".join(_cmds(log))
    prevalidate_idx = blob.index("visudo -cf /tmp/sudoers-agent-keep")
    helper_idx = blob.index("install -o root -g root -m 0755 /tmp/agent-keep-deploy")
    sudoers_idx = blob.index("install -o root -g root -m 0440 /tmp/sudoers-agent-keep")
    final_visudo_idx = blob.rindex("visudo -c")  # the full-config recheck (last)
    # staged file validated before it lands in /etc/sudoers.d/
    assert prevalidate_idx < sudoers_idx, blob
    # helper before sudoers before the final full recheck
    assert helper_idx < sudoers_idx < final_visudo_idx, blob


def test_rerun_is_idempotent_docker_install_skipped(tmp_path: Path) -> None:
    """When docker is already present, the convenience script must NOT run
    (idempotent re-run) — but the helper/sudoers are still re-installed."""
    log, _ = _write_stub_bin(tmp_path, docker_present=True)
    result = _run_bootstrap(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    cmds = _cmds(log)
    assert not any("get.docker.com" in c for c in cmds), cmds
    # helper/sudoers install still happens on every run
    assert any("install -o root -g root -m 0755 /tmp/agent-keep-deploy" in c for c in cmds)
    assert "HOST READY" in result.stdout


def test_fresh_host_runs_docker_install(tmp_path: Path) -> None:
    """When docker is absent, the official convenience script IS run and the user
    is added to the docker group."""
    log, _ = _write_stub_bin(tmp_path, docker_present=False)
    result = _run_bootstrap(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    cmds = _cmds(log)
    assert any("get.docker.com" in c for c in cmds), cmds
    assert any(f"usermod -aG docker {FAKE_USER}" in c for c in cmds), cmds
    assert "HOST READY" in result.stdout


def test_conformance_probe_uses_sudo_n(tmp_path: Path) -> None:
    """The final helper reachability check must use `sudo -n` (non-interactive) —
    proving the NOPASSWD sudoers is live, not a password prompt."""
    log, _ = _write_stub_bin(tmp_path, docker_present=True)
    result = _run_bootstrap(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert any(
        "sudo -n /usr/local/sbin/agent-keep-deploy preflight-check" in c for c in _cmds(log)
    ), _cmds(log)


def test_missing_target_fails_with_usage(tmp_path: Path) -> None:
    """No <ssh-target> arg → non-zero exit with usage guidance."""
    _write_stub_bin(tmp_path, docker_present=True)
    result = _run_bootstrap(tmp_path, target=None)
    assert result.returncode != 0
    assert "usage: scripts/bootstrap-host.sh" in result.stderr

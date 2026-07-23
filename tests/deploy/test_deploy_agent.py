"""Stage-15 gate: scripts/deploy-agent.sh — THE single (blueprint, target) deploy
entry point (ADR 0007, "two inputs → deployed").

deploy-agent.sh is THIN orchestration: it derives the slug from the blueprint,
ensures the target is conformant (bootstrap-if-needed), resolves the worker image,
and drives the real engine (deploy.sh) — threading provider secrets on stdin only.

Like the other deploy stub tests (test_bootstrap_host / test_deploy_local_image /
test_deploy_secret_injection), a real ssh host is unreachable in CI, so we put a
fake `ssh`/`scp`/`docker`/`keep-build` on PATH and let the REAL bootstrap-host.sh
and deploy.sh run underneath deploy-agent.sh — proving the ORCHESTRATION end to
end against stubs. We assert:
  - a NOT-ready target (preflight-check fails first) triggers bootstrap-host.sh,
    THEN deploy.sh;
  - a READY target (preflight-check passes) SKIPS bootstrap, straight to deploy.sh
    (idempotent);
  - the slug is DERIVED from the spec (the operator passes only spec + target);
  - KEEP_DEPLOY_SECRETS=1 threads stdin secret(s) to deploy.sh on stdin, and the
    secret VALUE never appears in stdout/stderr or the argv log (no-leak);
  - default image mode BUILDS+LOADS the worker (keep-build + docker load, no
    worker pull); the published opt-in (KEEP_WORKER_VERSION) uses registry pull;
  - missing args → non-zero exit with usage.

The stub `ssh` fakes exactly the commands whose stdout the scripts consume. The
`preflight-check` result is driven by a mode file: READY → always 0; NOT-ready →
the FIRST probe (deploy-agent's own) fails, later ones pass (so bootstrap's final
conformance check and deploy.sh's pre-flight both succeed after bootstrap ran).

Pure Python / pytest, no docker, no network, no real host (default marker).
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
DEPLOY_AGENT = REPO_ROOT / "scripts" / "deploy-agent.sh"
SPEC = REPO_ROOT / "specs" / "default-chatbot.yaml"
SLUG = "default-chatbot"

LOADED_ID = "sha256:" + "a1" * 32
PULLED_DIGEST = "ghcr.io/seanerama/stub@sha256:" + "00" * 32
FAKE_USER = "deploybot"


def _write_stub_bin(dir_path: Path, *, ready: bool) -> dict[str, Path]:
    """Fake ssh/scp/docker/keep-build on PATH.

    Returns paths of the logs the tests read back:
      ssh_log        every remote command string driven, in order.
      env_capture    the env-file body piped to the `write-env` helper.
      keepbuild_log  one line per keep-build invocation (empty if never built).
    `ready` picks the preflight-check behaviour (see module docstring).
    """
    ssh_log = dir_path / "ssh.log"
    env_capture = dir_path / "env.captured"
    keepbuild_log = dir_path / "keepbuild.log"
    preflight_count = dir_path / "preflight.count"
    ready_flag = "1" if ready else "0"

    ssh = dir_path / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        'cmd="${*: -1}"\n'
        f'printf "%s\\n" "$cmd" >> "{ssh_log}"\n'
        # conformance probe: deploy-agent + bootstrap + deploy.sh all run
        # `... preflight-check ...`. READY → always 0. NOT-ready → first call
        # (deploy-agent's own probe) fails; later calls pass.
        'if [[ "$cmd" == *"preflight-check"* ]]; then\n'
        f'  if [[ "{ready_flag}" == "1" ]]; then exit 0; fi\n'
        f'  n=$(cat "{preflight_count}" 2>/dev/null || echo 0); n=$((n+1))\n'
        f'  printf "%s" "$n" > "{preflight_count}"\n'
        '  if [[ "$n" -le 1 ]]; then exit 7; fi\n'
        "  exit 0\n"
        "fi\n"
        # bootstrap: login derivation + docker presence (present → skip install)
        'if [[ "$cmd" == "whoami" ]]; then\n'
        f'  echo "{FAKE_USER}"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$cmd" == *"command -v docker"* ]]; then\n'
        "  exit 0\n"
        "fi\n"
        # loaded-image pin (.Id) — checked before RepoDigests
        'if [[ "$cmd" == *".Id"* ]]; then\n'
        f'  echo "{LOADED_ID}"\n'
        "  exit 0\n"
        "fi\n"
        # registry pin (proxy/mechanic, and worker in published mode)
        'if [[ "$cmd" == *"RepoDigests"* ]]; then\n'
        f'  echo "{PULLED_DIGEST}"\n'
        "  exit 0\n"
        "fi\n"
        # worker delivery stream
        'if [[ "$cmd" == *"docker load"* ]]; then\n'
        "  cat >/dev/null\n"
        "  exit 0\n"
        "fi\n"
        # proxy-liveness assert-proxy-running.sh piped as `bash -s`
        'if [[ "$cmd" == *"bash -s"* || "$*" == *"bash -s"* ]]; then\n'
        "  exit 0\n"
        "fi\n"
        # healthz curls
        'if [[ "$cmd" == *"healthz"* ]]; then\n'
        '  echo \'{"status": "ok"}\'\n'
        "  exit 0\n"
        "fi\n"
        # write-env captures the env body; append-env is stdin-only (drain, do NOT
        # log — the no-leak guarantee)
        f'if [[ "$cmd" == *"write-env"* ]]; then\n'
        f'  cat >> "{env_capture}"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$cmd" == *"append-env"* ]]; then\n'
        "  cat >/dev/null\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    scp = dir_path / "scp"
    scp.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

    docker = dir_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "save" ]]; then\n'
        '  printf "IMG"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    keepbuild = dir_path / "keep-build"
    keepbuild.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{keepbuild_log}"\nexit 0\n',
        encoding="utf-8",
    )

    for f in (ssh, scp, docker, keepbuild):
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return {
        "ssh_log": ssh_log,
        "env_capture": env_capture,
        "keepbuild_log": keepbuild_log,
    }


def _run(
    bin_dir: Path,
    *,
    target: str = "op@fresh-host",
    spec: str | None = str(SPEC),
    stdin: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    # The repo venv python imports keep_spec (slug derivation) regardless of the
    # stubbed PATH — pin it so the test never depends on venv activation order.
    env["KEEP_PYTHON"] = str(REPO_ROOT / ".venv" / "bin" / "python")
    if extra_env:
        env.update(extra_env)
    args = ["bash", str(DEPLOY_AGENT)]
    if spec is not None:
        args.append(spec)
    args.append(target)
    return subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=90,
    )


def _lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# ----------------------------------------------------- conformance / bootstrap


def test_not_ready_target_bootstraps_then_deploys(tmp_path: Path) -> None:
    """A NOT-ready target runs bootstrap-host.sh FIRST, then deploy.sh."""
    logs = _write_stub_bin(tmp_path, ready=False)
    result = _run(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    cmds = _lines(logs["ssh_log"])
    # bootstrap ran: it derives the login (whoami) and installs the helper
    assert any(c == "whoami" for c in cmds), cmds
    boot_idx = next(i for i, c in enumerate(cmds) if c == "whoami")
    # deploy.sh ran AFTER bootstrap: the env write happens in the deploy phase
    deploy_idx = next(i for i, c in enumerate(cmds) if "write-env" in c)
    assert boot_idx < deploy_idx, cmds
    assert "DEPLOYED:" in result.stdout


def test_ready_target_skips_bootstrap(tmp_path: Path) -> None:
    """A READY target (preflight-check passes) goes straight to deploy.sh —
    bootstrap is a no-op (idempotent)."""
    logs = _write_stub_bin(tmp_path, ready=True)
    result = _run(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    cmds = _lines(logs["ssh_log"])
    # bootstrap's tell-tale remote commands must NOT appear
    assert not any(c == "whoami" for c in cmds), cmds
    assert not any("agent-keep-deploy /tmp" in c or "usermod" in c for c in cmds), cmds
    # but the deploy DID run
    assert any("write-env" in c for c in cmds), cmds
    assert "DEPLOYED:" in result.stdout


# ------------------------------------------------------------- slug derivation


def test_slug_is_derived_from_the_spec_not_passed(tmp_path: Path) -> None:
    """The operator passes only (spec, target); the slug is read from the spec and
    threaded through to deploy.sh's per-slug helper verbs."""
    logs = _write_stub_bin(tmp_path, ready=True)
    result = _run(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"slug={SLUG}" in result.stdout, result.stdout
    cmds = _lines(logs["ssh_log"])
    # deploy.sh drives per-slug helper verbs with the DERIVED slug
    assert any(f"write-env {SLUG}" in c for c in cmds), cmds
    assert any(f"service {SLUG}" in c and "restart" in c for c in cmds), cmds


# ------------------------------------------------------------------- secrets


def test_secret_threaded_to_deploy_on_stdin_and_never_leaks(tmp_path: Path) -> None:
    """KEEP_DEPLOY_SECRETS=1 pipes the stdin secret through to deploy.sh (which
    drives append-env), and the VALUE never appears in stdout/stderr/argv log."""
    logs = _write_stub_bin(tmp_path, ready=True)
    secret_value = "sk-ant-THE-SECRET-VALUE"
    result = _run(
        tmp_path,
        stdin=f"ANTHROPIC_API_KEY={secret_value}\n",
        extra_env={"KEEP_DEPLOY_SECRETS": "1"},
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    cmds = _lines(logs["ssh_log"])
    # the secret was injected (append-env driven) before the worker start (restart)
    append_idx = next(i for i, c in enumerate(cmds) if "append-env" in c)
    restart_idx = next(i for i, c in enumerate(cmds) if f"service {SLUG}" in c and "restart" in c)
    assert append_idx < restart_idx, cmds
    # NO-LEAK: value absent from stdout, stderr, and the argv log (stdin-only)
    assert secret_value not in result.stdout
    assert secret_value not in result.stderr
    assert secret_value not in logs["ssh_log"].read_text(encoding="utf-8")


def test_secrets_flag_with_empty_stdin_fails(tmp_path: Path) -> None:
    """KEEP_DEPLOY_SECRETS=1 with empty stdin fails non-zero with guidance."""
    _write_stub_bin(tmp_path, ready=True)
    result = _run(tmp_path, stdin="", extra_env={"KEEP_DEPLOY_SECRETS": "1"})
    assert result.returncode != 0
    assert "stdin was empty" in result.stderr


# ---------------------------------------------------------------- image mode


def test_default_mode_builds_and_loads_the_worker(tmp_path: Path) -> None:
    """Default (no KEEP_WORKER_VERSION): the worker is BUILT from the blueprint and
    LOADED (keep-build + docker load), never pulled."""
    logs = _write_stub_bin(tmp_path, ready=True)
    result = _run(tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert _lines(logs["keepbuild_log"]), "keep-build never ran in default (local) mode"
    cmds = _lines(logs["ssh_log"])
    assert any("docker load" in c for c in cmds), cmds
    assert not any("docker pull" in c and f"agent-keep-{SLUG}" in c for c in cmds), cmds


def test_published_mode_pulls_the_registry_tag(tmp_path: Path) -> None:
    """KEEP_WORKER_VERSION=<tag> uses the registry-pull path: pull+pin the worker,
    never build/load it locally."""
    logs = _write_stub_bin(tmp_path, ready=True)
    result = _run(tmp_path, extra_env={"KEEP_WORKER_VERSION": "edge"})
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not _lines(logs["keepbuild_log"]), "keep-build ran in published mode"
    cmds = _lines(logs["ssh_log"])
    assert any(
        "docker pull" in c and f"agent-keep-{SLUG}" in c and "RepoDigests" in c for c in cmds
    ), cmds
    assert not any("docker load" in c for c in cmds), cmds


# --------------------------------------------------------------------- usage


def test_missing_args_fails_with_usage(tmp_path: Path) -> None:
    """No args → non-zero exit with usage guidance."""
    _write_stub_bin(tmp_path, ready=True)
    result = subprocess.run(
        ["bash", str(DEPLOY_AGENT)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert result.returncode != 0
    assert "usage: scripts/deploy-agent.sh" in result.stderr


def test_missing_blueprint_file_fails(tmp_path: Path) -> None:
    """A blueprint path that does not exist fails clearly (non-zero)."""
    _write_stub_bin(tmp_path, ready=True)
    result = _run(tmp_path, spec=str(REPO_ROOT / "specs" / "does-not-exist.yaml"))
    assert result.returncode != 0
    assert "not found" in result.stderr

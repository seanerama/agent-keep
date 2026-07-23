"""Stage-7 gate: deploy.sh injects provider secrets BEFORE the worker starts.

A live worker builds its provider eagerly at boot and refuses to start without
its secret, so `deploy.sh` must write the secret into the env file before it
starts the unit — not after (issue #13's Step-B ordering wrinkle). This proves
the ordering and the provider-agnostic, no-leak handling by running the real
deploy.sh against a stubbed `ssh` on PATH (the tailnet host is unreachable in
CI; the stub records the helper verbs deploy.sh drives, in order).

Pure Python / pytest, no docker, no network (default marker — the `test` job).
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "deploy.sh"
DEPLOY_AGENT = REPO_ROOT / "scripts" / "deploy-agent.sh"
SPEC = REPO_ROOT / "specs" / "default-chatbot.yaml"


def _write_stub_bin(dir_path: Path) -> Path:
    """Fake `ssh` + `scp` on PATH. `ssh` appends its command to a log and fakes
    the two commands whose stdout deploy.sh consumes (docker pull/inspect digest
    resolution) so the script reaches the secrets step. Records verb ORDER."""
    log = dir_path / "ssh.log"
    ssh = dir_path / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        # last arg is the remote command string; earlier args are host/opts
        'cmd="${*: -1}"\n'
        f'printf "%s\\n" "$cmd" >> "{log}"\n'
        # digest resolution: `docker pull -q ... && docker inspect --format ...`
        'if [[ "$cmd" == *"docker inspect"* ]]; then\n'
        '  echo "ghcr.io/seanerama/stub@sha256:'
        '0000000000000000000000000000000000000000000000000000000000000000"\n'
        "  exit 0\n"
        "fi\n"
        # proxy-liveness: deploy.sh pipes assert-proxy-running.sh as `bash -s`
        'if [[ "$cmd" == *"bash -s"* || "$*" == *"bash -s"* ]]; then\n'
        "  exit 0\n"
        "fi\n"
        # healthz curls
        'if [[ "$cmd" == *"healthz"* ]]; then\n'
        '  echo \'{"status": "ok"}\'\n'
        "  exit 0\n"
        "fi\n"
        # write-env / append-env consume stdin; drain it so the pipe closes clean
        'if [[ "$cmd" == *"write-env"* || "$cmd" == *"append-env"* ]]; then\n'
        "  cat >/dev/null\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    scp = dir_path / "scp"
    scp.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for f in (ssh, scp):
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return log


def _run_deploy(
    bin_dir: Path, *, secrets_stdin: str | None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["DEPLOY_HOST"] = "stub@stub-host"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(DEPLOY), "default-chatbot", "edge"],
        input=secrets_stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )


def _verbs(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_secret_is_injected_before_worker_start(tmp_path: Path) -> None:
    """append-env must be driven BEFORE `service … restart` — the whole point."""
    log = _write_stub_bin(tmp_path)
    secret = "ANTHROPIC_API_KEY=sk-ant-THE-SECRET-VALUE"
    result = _run_deploy(
        tmp_path, secrets_stdin=secret + "\n", extra_env={"KEEP_DEPLOY_SECRETS": "1"}
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    verbs = _verbs(log)
    append_idx = next(i for i, c in enumerate(verbs) if "append-env" in c)
    restart_idx = next(
        i for i, c in enumerate(verbs) if "service default-chatbot" in c and "restart" in c
    )
    write_idx = next(i for i, c in enumerate(verbs) if "write-env" in c)
    assert write_idx < append_idx < restart_idx, verbs


def test_secret_value_never_appears_in_output(tmp_path: Path) -> None:
    """The secret VALUE must not leak into deploy.sh's stdout/stderr or argv log."""
    log = _write_stub_bin(tmp_path)
    secret_value = "sk-ant-THE-SECRET-VALUE"
    result = _run_deploy(
        tmp_path,
        secrets_stdin=f"ANTHROPIC_API_KEY={secret_value}\n",
        extra_env={"KEEP_DEPLOY_SECRETS": "1"},
    )
    assert result.returncode == 0
    assert secret_value not in result.stdout
    assert secret_value not in result.stderr
    # argv log records the remote command strings; append-env is stdin-only, so
    # the value must not appear there either.
    assert secret_value not in log.read_text(encoding="utf-8")


def test_no_flag_means_no_secret_injection(tmp_path: Path) -> None:
    """Without KEEP_DEPLOY_SECRETS=1 the deploy runs and never calls append-env."""
    log = _write_stub_bin(tmp_path)
    result = _run_deploy(tmp_path, secrets_stdin=None)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not any("append-env" in c for c in _verbs(log))


def test_flag_with_empty_stdin_fails_clearly(tmp_path: Path) -> None:
    """KEEP_DEPLOY_SECRETS=1 with empty stdin must fail non-zero with guidance."""
    _write_stub_bin(tmp_path)
    result = _run_deploy(tmp_path, secrets_stdin="", extra_env={"KEEP_DEPLOY_SECRETS": "1"})
    assert result.returncode != 0
    assert "stdin was empty" in result.stderr


# ── Stage-24 gate: reject empty-valued / malformed secret lines at BOTH entry
# points, BEFORE any build/ssh/remote action (live finding F1: an empty-valued
# ANTHROPIC_API_KEY= line shipped to the host and the worker crash-looped).
# Same stub-bin idiom: bad lines must exit 64 with ZERO ssh commands driven.


def _run_deploy_agent(
    bin_dir: Path, *, secrets_stdin: str | None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real scripts/deploy-agent.sh against the same stubbed PATH.

    The stage-24 guard fires right after stdin is read — before slug derivation,
    build, bootstrap, or any ssh — so the rejection matrix needs nothing beyond
    the stub bin (which must record ZERO commands)."""
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(DEPLOY_AGENT), str(SPEC), "stub@stub-host"],
        input=secrets_stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )


def test_empty_value_line_rejected_deploy(tmp_path: Path) -> None:
    """The F1 regression (deploy.sh): `ANTHROPIC_API_KEY=` (empty value) → exit
    64, stderr NAMES the var, and NO ssh action was attempted."""
    log = _write_stub_bin(tmp_path)
    result = _run_deploy(
        tmp_path,
        secrets_stdin="ANTHROPIC_API_KEY=\n",  # gitleaks:allow
        extra_env={"KEEP_DEPLOY_SECRETS": "1"},
    )
    assert result.returncode == 64, f"{result.stdout}\n{result.stderr}"
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert "line 1" in result.stderr
    assert _verbs(log) == [], "ssh was driven despite an invalid secret line"


def test_empty_value_line_rejected_deploy_agent(tmp_path: Path) -> None:
    """The F1 regression (scripts/deploy-agent.sh): same empty-valued line →
    exit 64, var named, no ssh/build action."""
    log = _write_stub_bin(tmp_path)
    result = _run_deploy_agent(
        tmp_path,
        secrets_stdin="ANTHROPIC_API_KEY=\n",  # gitleaks:allow
        extra_env={"KEEP_DEPLOY_SECRETS": "1"},
    )
    assert result.returncode == 64, f"{result.stdout}\n{result.stderr}"
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert "line 1" in result.stderr
    assert _verbs(log) == [], "ssh was driven despite an invalid secret line"


def test_line_without_equals_rejected_both(tmp_path: Path) -> None:
    """A line with no `=` at all → exit 64 on BOTH entry points; the raw line is
    NEVER echoed (no-secrets-in-logs applies to error paths)."""
    log = _write_stub_bin(tmp_path)
    for runner in (_run_deploy, _run_deploy_agent):
        result = runner(
            tmp_path,
            secrets_stdin="not-a-var-line\n",
            extra_env={"KEEP_DEPLOY_SECRETS": "1"},
        )
        assert result.returncode == 64, f"{runner.__name__}: {result.stderr}"
        assert "malformed" in result.stderr
        assert "not-a-var-line" not in result.stderr
        assert "not-a-var-line" not in result.stdout
        assert _verbs(log) == []


def test_invalid_var_name_rejected_both(tmp_path: Path) -> None:
    """An invalid env var name (`9BAD=x`) → exit 64 on BOTH entry points."""
    log = _write_stub_bin(tmp_path)
    for runner in (_run_deploy, _run_deploy_agent):
        result = runner(
            tmp_path,
            secrets_stdin="9BAD=x\n",
            extra_env={"KEEP_DEPLOY_SECRETS": "1"},
        )
        assert result.returncode == 64, f"{runner.__name__}: {result.stderr}"
        assert "malformed" in result.stderr
        assert _verbs(log) == []


def test_one_bad_line_rejects_the_whole_batch(tmp_path: Path) -> None:
    """All-or-nothing: one GOOD line + one bad line → exit 64 on BOTH entry
    points, nothing shipped, and neither value nor raw line leaks to output."""
    log = _write_stub_bin(tmp_path)
    good_value = "sk-ant-GOOD-VALUE"
    for runner in (_run_deploy, _run_deploy_agent):
        result = runner(
            tmp_path,
            secrets_stdin=f"GOOD_KEY={good_value}\nANTHROPIC_API_KEY=\n",
            extra_env={"KEEP_DEPLOY_SECRETS": "1"},
        )
        assert result.returncode == 64, f"{runner.__name__}: {result.stderr}"
        assert "ANTHROPIC_API_KEY" in result.stderr
        assert "line 2" in result.stderr
        assert good_value not in result.stdout
        assert good_value not in result.stderr
        assert _verbs(log) == [], "something was shipped despite a bad secret line"

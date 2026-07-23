"""Stage-14 gate: deploy.sh's LOCAL-IMAGE mode (ADR 0007 arbitrary-blueprint path).

Deploy ANY blueprint WITHOUT a registry write: instead of pulling the worker tag
from ghcr, `deploy.sh` (when KEEP_WORKER_LOCAL_IMAGE=1) BUILDS the worker from the
spec locally, streams it to the host (`docker save | ssh docker load` — no file, no
registry), and pins the LOADED image by its immutable ID. A loaded image has NO
RepoDigests (it was never pulled/pushed), so it CANNOT be pinned via
`{{index .RepoDigests 0}}` — `{{.Id}}` is the pin. This proves, against a stubbed
`ssh`/`scp`/`docker`/`keep-build` on PATH (mirroring test_deploy_secret_injection):

  local-image mode (flag ON):
    (a) builds + `docker save | ssh docker load`s the worker (no registry),
    (b) does NOT `docker pull` the worker,
    (c) pins the worker by `.Id`, not RepoDigests (env carries the id-form ref),
    (d) STILL pulls proxy + mechanic (generic published images);
  registry-pull mode (flag OFF, unchanged):
    (e) still `docker pull`s the worker and pins it via RepoDigests (digest-form ref),
    (f) never builds/loads locally.

The stub `docker` returns an EMPTY RepoDigests but a real `.Id` for the loaded
worker — exactly what a `docker load`ed image looks like — so the pin-by-id path
is proven, not assumed. Pure Python / pytest, no docker, no network (default
marker — the `test` job).
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "deploy.sh"

#: The `.Id` the stub reports for the LOADED worker image (content-addressed id).
LOADED_ID = "sha256:" + "a1" * 32
#: The RepoDigest the stub reports for a PULLED image (proxy/mechanic, and the
#: worker in flag-OFF mode).
PULLED_DIGEST = "ghcr.io/seanerama/stub@sha256:" + "00" * 32
WORKER_IMG = "ghcr.io/seanerama/agent-keep-default-chatbot"


def _write_stub_bin(dir_path: Path) -> tuple[Path, Path, Path]:
    """Fake `ssh`/`scp`/`docker`/`keep-build` on PATH.

    Returns (ssh_log, env_capture, keepbuild_log):
      - ssh_log        every remote command string deploy.sh drives, in order.
      - env_capture    the env-file body piped to the `write-env` helper.
      - keepbuild_log  a line per `keep-build` invocation (empty if never built).
    """
    ssh_log = dir_path / "ssh.log"
    env_capture = dir_path / "env.captured"
    keepbuild_log = dir_path / "keepbuild.log"

    ssh = dir_path / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        # last arg is the remote command string; earlier args are host/opts
        'cmd="${*: -1}"\n'
        f'printf "%s\\n" "$cmd" >> "{ssh_log}"\n'
        # loaded-image pin: `docker inspect --format {{.Id}}` -> the immutable id.
        # Checked BEFORE RepoDigests so the id path is unambiguous.
        'if [[ "$cmd" == *".Id"* ]]; then\n'
        f'  echo "{LOADED_ID}"\n'
        "  exit 0\n"
        "fi\n"
        # registry pin: `docker pull -q ... && docker inspect --format RepoDigests`.
        'if [[ "$cmd" == *"RepoDigests"* ]]; then\n'
        f'  echo "{PULLED_DIGEST}"\n'
        "  exit 0\n"
        "fi\n"
        # worker delivery: `ssh <host> docker load` — drain the streamed image.
        'if [[ "$cmd" == *"docker load"* ]]; then\n'
        "  cat >/dev/null\n"
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
        # write-env consumes stdin — capture the env body for assertions.
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

    # LOCAL docker (control machine) — the deliver helper's `docker save`. Emits a
    # byte so the save|load pipe carries a stream; anything else is a harmless 0.
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

    # keep-build: record the build, do nothing else (no real docker build).
    keepbuild = dir_path / "keep-build"
    keepbuild.write_text(
        "#!/usr/bin/env bash\n" f'printf "%s\\n" "$*" >> "{keepbuild_log}"\n' "exit 0\n",
        encoding="utf-8",
    )

    for f in (ssh, scp, docker, keepbuild):
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return ssh_log, env_capture, keepbuild_log


def _run_deploy(bin_dir: Path, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["DEPLOY_HOST"] = "stub@stub-host"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(DEPLOY), "default-chatbot", "edge"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )


def _verbs(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _env_body(env_capture: Path) -> str:
    return env_capture.read_text(encoding="utf-8") if env_capture.exists() else ""


# ------------------------------------------------------------- local-image (ON)


def test_local_mode_builds_and_loads_the_worker_without_pulling_it(tmp_path: Path) -> None:
    """(a) builds + save|load-delivers the worker; (b) never pulls the worker."""
    ssh_log, _, keepbuild_log = _write_stub_bin(tmp_path)
    result = _run_deploy(tmp_path, extra_env={"KEEP_WORKER_LOCAL_IMAGE": "1"})
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    verbs = _verbs(ssh_log)
    # (a) the worker was built locally and streamed to the host via `docker load`.
    assert _verbs(keepbuild_log), "keep-build was never invoked in local-image mode"
    assert any("docker load" in c for c in verbs), verbs
    # (b) NO `docker pull` of the worker image ran.
    assert not any(
        "docker pull" in c and "agent-keep-default-chatbot" in c for c in verbs
    ), f"worker was pulled in local-image mode: {verbs}"


def test_local_mode_pins_worker_by_id_not_repodigests(tmp_path: Path) -> None:
    """(c) the env carries the id-form worker ref (pinned by .Id), and the digest
    field equals that id (the id IS the digest for a loaded image)."""
    ssh_log, env_capture, _ = _write_stub_bin(tmp_path)
    result = _run_deploy(tmp_path, extra_env={"KEEP_WORKER_LOCAL_IMAGE": "1"})
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    body = _env_body(env_capture)
    assert f"WORKER_IMAGE_REF={LOADED_ID}" in body, body
    assert f"WORKER_IMAGE_DIGEST={LOADED_ID}" in body, body
    # a loaded-image ref is a bare id, NOT a repo@digest — no registry pin leaked in.
    assert "WORKER_IMAGE_REF=ghcr.io" not in body, body
    # the worker pin used .Id, never RepoDigests.
    verbs = _verbs(ssh_log)
    assert any(".Id" in c for c in verbs), verbs
    assert not any(
        "RepoDigests" in c and "agent-keep-default-chatbot" in c for c in verbs
    ), verbs


def test_local_mode_still_pulls_proxy_and_mechanic(tmp_path: Path) -> None:
    """(d) proxy + mechanic are generic published images — still pulled+pinned."""
    ssh_log, env_capture, _ = _write_stub_bin(tmp_path)
    result = _run_deploy(tmp_path, extra_env={"KEEP_WORKER_LOCAL_IMAGE": "1"})
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    verbs = _verbs(ssh_log)
    assert any("docker pull" in c and "agent-keep-egress-proxy" in c for c in verbs), verbs
    assert any("docker pull" in c and "agent-keep-mechanic" in c for c in verbs), verbs
    # and their refs are digest-pinned (RepoDigests), unchanged.
    body = _env_body(env_capture)
    assert f"PROXY_IMAGE_REF={PULLED_DIGEST}" in body, body
    assert f"MECHANIC_IMAGE_REF={PULLED_DIGEST}" in body, body


# --------------------------------------------------------- registry-pull (OFF)


def test_flag_off_pulls_and_pins_worker_via_repodigests(tmp_path: Path) -> None:
    """(e)/(f) the default path is UNCHANGED: pull the worker, pin by RepoDigest,
    never build/load locally."""
    ssh_log, env_capture, keepbuild_log = _write_stub_bin(tmp_path)
    result = _run_deploy(tmp_path)  # no KEEP_WORKER_LOCAL_IMAGE
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    verbs = _verbs(ssh_log)
    # (e) the worker was pulled and pinned by RepoDigest.
    assert any(
        "docker pull" in c and "agent-keep-default-chatbot" in c and "RepoDigests" in c
        for c in verbs
    ), verbs
    body = _env_body(env_capture)
    assert f"WORKER_IMAGE_REF={PULLED_DIGEST}" in body, body
    # (f) nothing was built or loaded locally.
    assert not _verbs(keepbuild_log), "keep-build ran in registry-pull mode"
    assert not any("docker load" in c for c in verbs), verbs

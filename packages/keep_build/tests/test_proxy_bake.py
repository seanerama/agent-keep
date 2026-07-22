"""Unit tests for the egress-proxy bake path (`keep_build.egress_proxy` +
`keep-build build-proxy --context-only`) — no docker daemon needed."""

from importlib.metadata import version
from pathlib import Path

from keep_build.cli import main
from keep_build.composer import BASE_IMAGE
from keep_build.egress_proxy import (
    PROXY_IMAGE,
    emit_proxy_build_context,
    proxy_dockerfile,
)


def test_proxy_image_identity() -> None:
    assert PROXY_IMAGE == "ghcr.io/seanerama/agent-keep-egress-proxy"


def test_proxy_dockerfile_discipline() -> None:
    """Same build posture as the agent image: digest-pinned base, pinned deps,
    non-root uid 10001, and the proxy entrypoint — with NO spec baked in (the
    allowlist arrives at run time as the mounted spec)."""
    dockerfile = proxy_dockerfile()
    assert f"FROM {BASE_IMAGE}" in dockerfile
    assert "@sha256:" in dockerfile
    assert f'"pydantic=={version("pydantic")}"' in dockerfile
    assert f'"pyyaml=={version("pyyaml")}"' in dockerfile
    # named 'egress', not 'proxy' — Debian bases already ship a system user
    # called proxy (uid 13); the runtime user must be OUR non-root uid 10001
    assert "useradd --create-home --uid 10001 egress" in dockerfile
    assert "USER egress" in dockerfile
    assert "EXPOSE 3128" in dockerfile
    assert 'CMD ["python", "-m", "keep_egress.runner"]' in dockerfile
    # spec-independent: nothing copies a spec.yaml into the image
    assert "spec.yaml" not in dockerfile.replace("mounted spec.yaml", "")


def test_emit_proxy_build_context(tmp_path: Path) -> None:
    context = tmp_path / "ctx"
    emit_proxy_build_context(context)
    assert (context / "Dockerfile").is_file()
    # the two packages the image ships — validator + proxy...
    assert (context / "keep_spec" / "models.py").is_file()
    assert (context / "keep_spec" / "egress.py").is_file()
    assert (context / "keep_egress" / "proxy.py").is_file()
    assert (context / "keep_egress" / "runner.py").is_file()
    assert (context / "keep_egress" / "records.py").is_file()
    # ...and NEVER the agent's runtime (the choke point lives outside it)
    assert not (context / "agent_runtime").exists()
    assert not (context / "spec.yaml").exists()


def test_emit_reconciles_owned_paths(tmp_path: Path) -> None:
    """Re-emission over a retained context leaves no stale owned files behind
    (the composer's clean-then-write rule, applied to this bake path)."""
    context = tmp_path / "ctx"
    emit_proxy_build_context(context)
    stale = context / "keep_egress" / "stale_module.py"
    stale.write_text("# stale\n", encoding="utf-8")
    unrelated = context / "user-notes.txt"
    unrelated.write_text("keep me\n", encoding="utf-8")
    emit_proxy_build_context(context)
    assert not stale.exists()
    assert unrelated.exists()


def test_cli_build_proxy_context_only(tmp_path: Path, capsys: object) -> None:
    context = tmp_path / "cli-ctx"
    rc = main(["build-proxy", "--context-only", "--context-dir", str(context)])
    assert rc == 0
    assert (context / "Dockerfile").is_file()
    assert (context / "keep_egress" / "proxy.py").is_file()

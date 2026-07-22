"""Unit tests for keep_build — the wiring/composition proof the stage requires:
the composed component set matches the spec (absence semantics), with no
docker involved (the container job proves the same invariant against the real
image).
"""

from pathlib import Path

import pytest

from agent_runtime.wiring import component_module_names, select_components
from keep_build.composer import (
    CORE_MODULES,
    dockerfile_for,
    emit_build_context,
    expected_component_modules,
    image_fs_scan_script,
    image_tag,
    runtime_dependencies_for,
    unselected_component_modules,
)
from keep_spec import AgentSpec, load_spec

REPO_ROOT = Path(__file__).parents[3]
SPEC_PATH = REPO_ROOT / "specs" / "default-chatbot.yaml"

#: The exact component set the default-chatbot spec declares — reviewed here,
#: enforced by the composer and the runner alike (one wiring source of truth).
EXPECTED_COMPONENTS = [
    "dev-http-channel",
    "in-process-queue",
    "jsonl-audit",
    "model-router",  # budgets are on -> the router ships and meters tokens
    "prompt-assembler",
    "sqlite-persistence",
    "static-provider",
]
EXPECTED_MODULES = [
    "dev_http",
    "jsonl_audit",
    "memory_queue",
    "model_router",
    "prompt_assembler",
    "sqlite_persistence",
    "static_provider",
]

#: components/ files the composer ships in EVERY image (stdlib plumbing listed
#: in CORE_MODULES) — counted with the component files, never the core files.
ALWAYS_SHIPPED_COMPONENT_FILES = {
    Path(module).name for module in CORE_MODULES if module.startswith("components/")
}


@pytest.fixture(scope="module")
def spec() -> AgentSpec:
    return load_spec(SPEC_PATH)


def test_locked_image_identity(spec: AgentSpec) -> None:
    """ADR 0001: images extend the slug — ghcr.io/seanerama/agent-keep-<slug>."""
    assert image_tag(spec) == "ghcr.io/seanerama/agent-keep-default-chatbot"


def test_composed_component_set_matches_the_spec(spec: AgentSpec) -> None:
    assert select_components(spec) == EXPECTED_COMPONENTS
    assert component_module_names(spec) == EXPECTED_MODULES
    assert expected_component_modules(spec) == set(EXPECTED_MODULES)


def test_unselected_components_include_the_sharp_absences(spec: AgentSpec) -> None:
    """The set whose absence the container job greps for: the remote provider,
    the tool registry, and the non-durable session manager are all unselected."""
    unselected = unselected_component_modules(spec)
    assert {"anthropic_provider", "local_tools", "single_session"} <= unselected
    assert unselected.isdisjoint(EXPECTED_MODULES)


def test_static_only_image_pins_no_http_client(spec: AgentSpec) -> None:
    """Absence applies to libraries too: no httpx in a static-provider image."""
    assert runtime_dependencies_for(spec) == ("pydantic", "pyyaml")
    assert "httpx" not in dockerfile_for(spec)


def test_dockerfile_shape(spec: AgentSpec) -> None:
    dockerfile = dockerfile_for(spec)
    assert dockerfile.count("FROM ") == 1
    assert "FROM python:3.12-slim@sha256:" in dockerfile  # digest-pinned base
    assert "useradd --create-home --uid 10001 agent" in dockerfile  # non-root
    assert "USER agent" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert "/var/lib/agent-keep" in dockerfile  # audit dir owned by the agent
    assert 'CMD ["python", "-m", "agent_runtime.runner", "/app/spec.yaml"]' in dockerfile


def test_emitted_context_is_exactly_the_selection(spec: AgentSpec, tmp_path: Path) -> None:
    """Absence semantics on disk: the emitted build context carries the core
    modules + EXACTLY the selected components — no executor, no gateway, no
    unselected component module, no trace of any of them."""
    emit_build_context(spec, SPEC_PATH, tmp_path)

    components_dir = tmp_path / "agent_runtime" / "components"
    actual_files = {p.name for p in components_dir.glob("*.py")}
    expected_files = (
        {f"{name}.py" for name in EXPECTED_MODULES}
        | {"__init__.py"}
        | ALWAYS_SHIPPED_COMPONENT_FILES
    )
    assert actual_files == expected_files

    core_files = {p.name for p in (tmp_path / "agent_runtime").glob("*.py")}
    assert core_files == {Path(m).name for m in CORE_MODULES if "/" not in m}
    assert "executor.py" not in core_files  # no tools granted -> no tool layer
    assert "gateway.py" not in core_files  # no allowlist -> no gateway

    # no filesystem trace of ANY unselected component module in the context
    for module in unselected_component_modules(spec):
        assert not list(tmp_path.rglob(f"{module}*")), f"trace of unselected {module}"

    # the validator and the spec itself ride whole
    assert (tmp_path / "keep_spec" / "models.py").is_file()
    assert (tmp_path / "spec.yaml").read_text(encoding="utf-8") == SPEC_PATH.read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "Dockerfile").is_file()


def test_reemission_reconciles_a_stale_context(spec: AgentSpec, tmp_path: Path) -> None:
    """A reused context dir represents EXACTLY one spec: a stale module from a
    broader previous emission is removed, unrelated user files are kept."""
    emit_build_context(spec, SPEC_PATH, tmp_path)
    stale = tmp_path / "agent_runtime" / "components" / "anthropic_provider.py"
    stale.write_text("# stale broad-spec leftover\n", encoding="utf-8")
    unrelated = tmp_path / "operator-notes.txt"
    unrelated.write_text("mine\n", encoding="utf-8")

    emit_build_context(spec, SPEC_PATH, tmp_path)
    assert not stale.exists()
    assert unrelated.read_text(encoding="utf-8") == "mine\n"


def test_image_fs_scan_script_finds_and_clears(tmp_path: Path) -> None:
    """The shared absence-grep program: exits 1 with hits printed when a trace
    exists under the scan roots, 0 when clean (exercised against a temp tree —
    the container job runs the same program inside the image)."""
    import subprocess
    import sys

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "anthropic_provider.py").write_text("x = 1\n", encoding="utf-8")
    script = image_fs_scan_script(["anthropic_provider"], roots=(str(tmp_path),))
    hit = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert hit.returncode == 1
    assert "anthropic_provider.py" in hit.stdout

    (tmp_path / "sub" / "anthropic_provider.py").unlink()
    clean = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert clean.returncode == 0

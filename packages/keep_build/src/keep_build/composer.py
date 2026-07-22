"""Composer — turn a validated keep/v1 AgentSpec into a docker build context.

Adapted from the Foundry's `foundry.composer` (read-only source at
~/projects/Agent-Factorio, carried per ADR 0001) only as far as Agent Keep
needs: bake ONE spec into one image — no fleet, no template catalog, no
interview. The mechanism is unchanged:

Absence semantics (contract agent-spec, rule 2): the build context receives
the agent_runtime CORE modules plus ONLY the component modules the spec
selects. An unselected component (e.g. anthropic_provider when the spec chose
the static provider) is physically absent from the context and therefore from
the image — not disabled, absent.
"""

import shutil
from importlib.metadata import version
from pathlib import Path

import agent_runtime
import keep_spec
from agent_runtime.wiring import (
    COMPONENT_REGISTRY,
    ComponentNotImplementedError,
    component_module_names,
    ensure_buildable,
    select_components,
    tool_execution_required,
)
from keep_spec import AgentSpec, DevHttpChannel

#: Locked image identity (ADR 0001): images extend the slug —
#: ghcr.io/seanerama/agent-keep-<slug>.
REGISTRY_PREFIX = "ghcr.io/seanerama/agent-keep"

#: agent_runtime modules every composed image ships (interfaces + core loop).
#: The Foundry's list, minus modules the transplant left behind (no
#: embedding.py — pgvector/retrieval were not carried) and plus lifecycle.py
#: (the run-lifecycle@1 contract models live in agent_runtime; shape-only, no
#: emitter — see contracts/run-lifecycle.md).
CORE_MODULES = [
    "__init__.py",
    "messages.py",
    "audit.py",
    "provider.py",
    "queues.py",
    "sessions.py",
    # The facts seam: the narrow FactsBackend contract the persistence tiers
    # implement — core so the tier modules can name the seam whether or not a
    # facts component ships (the Foundry precedent, stage 24 there).
    "facts.py",
    # run-lifecycle@1 contract models (RunState/RunHeartbeat) — shape-only,
    # drift-guarded like every other contract model; no emitter exists.
    "lifecycle.py",
    # The shared bounded HTTP receiver: stdlib-only transport plumbing every
    # HTTP-facing channel component imports. Core so it ships in every image
    # like queues.py; adds NO runtime dependency.
    "components/http_receiver.py",
    # The shared channel lifecycle (async-ack + reply delivery + seen-id
    # cache) hardened HTTP channels import. Stdlib-only, same posture.
    "components/channel_lifecycle.py",
    "core.py",
    "wiring.py",
    "runner.py",
]

#: Runtime deps installed into the image, pinned to the workspace's resolved
#: versions (dockerfile_for emits `pip install "name==ver"`).
RUNTIME_DEPENDENCIES = ("pydantic", "pyyaml")

#: Extra deps a component pulls into the image WHEN selected (absence
#: semantics apply to libraries too: a static-only image ships no HTTP
#: client). Only components the transplant carried are listed.
COMPONENT_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "anthropic-provider": ("httpx",),
}

#: Extra components/ modules a selected component imports at MODULE scope and
#: must therefore ship alongside it (they ride ONLY when their owner is
#: selected): local_tools merges the read-only analyzer ops into its registry.
COMPONENT_MODULE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "local-tools": ("worker_analyzer",),
}

#: Context-root paths emit_build_context generates and therefore OWNS. Every
#: emission removes these before writing so a reused `--context-dir`
#: represents EXACTLY one spec — a narrow spec re-emitted over a broad spec's
#: retained context cannot leave a stale component module behind for the
#: Dockerfile's `COPY agent_runtime/` to carry into the image (absence is the
#: security control, contract rule 2). Unrelated user files are left alone.
OWNED_CONTEXT_PATHS = (
    "Dockerfile",
    "spec.yaml",
    "keep_spec",
    "agent_runtime",
)

#: Image filesystem roots the absence grep walks — the app payload, the pip
#: site-packages, and the non-root user's home (single source of truth shared
#: by the container integration test).
IMAGE_SCAN_ROOTS = ("/app", "/usr/local/lib", "/home")


def runtime_dependencies_for(spec: AgentSpec) -> tuple[str, ...]:
    """Base runtime deps plus the deps of the spec's selected components,
    deduplicated (two components may pull the same library)."""
    extra = {
        dep
        for component_id in select_components(spec)
        for dep in COMPONENT_DEPENDENCIES.get(component_id, ())
    }
    return RUNTIME_DEPENDENCIES + tuple(sorted(extra))


def image_tag(spec: AgentSpec) -> str:
    return f"{REGISTRY_PREFIX}-{spec.metadata.slug}"


def expected_component_modules(spec: AgentSpec) -> set[str]:
    """Module stems the composer copies into agent_runtime/components/."""
    module_deps = {
        module
        for component_id in select_components(spec)
        for module in COMPONENT_MODULE_DEPENDENCIES.get(component_id, ())
    }
    return set(component_module_names(spec)) | module_deps


def unselected_component_modules(spec: AgentSpec) -> set[str]:
    """Every component module in the library that this spec does NOT select —
    the set whose ABSENCE from the image is the security control."""
    return set(COMPONENT_REGISTRY.values()) - expected_component_modules(spec)


def image_fs_scan_script(stems: list[str], roots: tuple[str, ...] = IMAGE_SCAN_ROOTS) -> str:
    """A `python -c` program that greps the image filesystem for any file whose
    name starts with one of `stems` and exits 1 if it finds a trace.

    Carried from the Foundry's `foundry.conformance` — the exact absence proof
    the container CI job runs against the built image. `roots` is overridable
    only so a non-container unit test can exercise the generated program
    against a temp tree.
    """
    return (
        "import pathlib; "
        f"roots = {[str(r) for r in roots]!r}; "
        f"stems = {list(stems)!r}; "
        "files = [p for r in roots if pathlib.Path(r).exists() "
        "for p in pathlib.Path(r).rglob('*')]; "
        "hits = sorted({str(p) for p in files for s in stems if p.name.startswith(s)}); "
        "print(chr(10).join(hits)); "
        "raise SystemExit(1 if hits else 0)"
    )


def dockerfile_for(spec: AgentSpec) -> str:
    """The generated Dockerfile: digest-pinned base, pinned deps, non-root
    uid 10001, absence-composed payload. dev-http is the only channel the
    chassis serves (ADR 0003); anything else fails loudly here exactly as it
    does in ensure_buildable."""
    pins = " ".join(f'"{name}=={version(name)}"' for name in runtime_dependencies_for(spec))
    audit_dir = str(Path(spec.spec.observability.audit.path).parent)
    channel = spec.spec.channels[0]
    if not isinstance(channel, DevHttpChannel):
        raise ComponentNotImplementedError(
            f"component not implemented: channel adapter '{channel.type}' (spec.channels[0])"
        )
    expose = str(channel.port)
    return f"""\
# Generated by keep_build for agent '{spec.metadata.slug}' (specVersion \
{spec.metadata.specVersion}).
# Base image pinned by digest (carried from the Foundry, SUPPLY-01): tag
# python:3.12-slim as resolved on 2026-07-11. To update: `docker pull
# python:3.12-slim && docker inspect \\
# --format='{{{{index .RepoDigests 0}}}}' python:3.12-slim`, then replace the digest.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

RUN pip install --no-cache-dir {pins}

# Non-root runtime user; the audit sink directory is theirs to append to.
RUN useradd --create-home --uid 10001 agent \\
    && mkdir -p {audit_dir} \\
    && chown agent:agent {audit_dir}

WORKDIR /app
COPY keep_spec/ /app/keep_spec/
COPY agent_runtime/ /app/agent_runtime/
COPY spec.yaml /app/spec.yaml

ENV PYTHONPATH=/app \\
    PYTHONUNBUFFERED=1 \\
    DEV_HTTP_HOST=0.0.0.0

USER agent
EXPOSE {expose}
CMD ["python", "-m", "agent_runtime.runner", "/app/spec.yaml"]
"""


def emit_build_context(spec: AgentSpec, spec_path: Path, context_dir: Path) -> None:
    """Write the Dockerfile + spec + selected sources into `context_dir`.

    Raises ComponentNotImplementedError first if the spec selects components
    this library version lacks — never a silent partial image.
    """
    ensure_buildable(spec)
    context_dir.mkdir(parents=True, exist_ok=True)

    # Reconcile the destination before writing so the emitted context
    # represents EXACTLY this spec (clean-then-write; any exception during the
    # write propagates — a half-updated context is raised, never reused).
    for name in OWNED_CONTEXT_PATHS:
        target = context_dir / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()

    (context_dir / "Dockerfile").write_text(dockerfile_for(spec), encoding="utf-8")
    shutil.copyfile(spec_path, context_dir / "spec.yaml")

    # keep_spec ships whole — it is the validator, not a component library.
    spec_src = Path(keep_spec.__file__).parent
    shutil.copytree(
        spec_src,
        context_dir / "keep_spec",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )

    # agent_runtime: core modules + ONLY the selected components.
    runtime_src = Path(agent_runtime.__file__).parent
    runtime_dst = context_dir / "agent_runtime"
    components_dst = runtime_dst / "components"
    components_dst.mkdir(parents=True, exist_ok=True)
    for module in CORE_MODULES:
        shutil.copyfile(runtime_src / module, runtime_dst / module)
    if tool_execution_required(spec):
        # The tool executor (and with it the approval endpoints) ships ONLY
        # when the spec grants tools; a tool-less agent has no executor
        # module in its image at all (absence semantics, rule 2).
        shutil.copyfile(runtime_src / "executor.py", runtime_dst / "executor.py")
    if spec.spec.gateway.allowlist is not None:
        # Gateway allowlist enforcement ships ONLY when the spec declares an
        # identity layer; a rosterless agent has no gateway module at all.
        shutil.copyfile(runtime_src / "gateway.py", runtime_dst / "gateway.py")
    shutil.copyfile(runtime_src / "components" / "__init__.py", components_dst / "__init__.py")
    for module_name in sorted(expected_component_modules(spec)):
        filename = f"{module_name}.py"
        shutil.copyfile(runtime_src / "components" / filename, components_dst / filename)

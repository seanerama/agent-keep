"""keep_build — bake ONE validated keep/v1 spec into an absence-composed image.

The smallest faithful adaptation of the Foundry's build machinery (ADR 0001:
carried, not rewritten; narrowed, not extended): composer + CLI only. The
component selection itself lives in `agent_runtime.wiring` — the same single
source of truth the runner imports at boot, so a compose/boot mismatch fails
loudly instead of silently degrading.
"""

from keep_build.composer import (
    CORE_MODULES,
    REGISTRY_PREFIX,
    dockerfile_for,
    emit_build_context,
    expected_component_modules,
    image_fs_scan_script,
    image_tag,
    runtime_dependencies_for,
    unselected_component_modules,
)

__all__ = [
    "CORE_MODULES",
    "REGISTRY_PREFIX",
    "dockerfile_for",
    "emit_build_context",
    "expected_component_modules",
    "image_fs_scan_script",
    "image_tag",
    "runtime_dependencies_for",
    "unselected_component_modules",
]

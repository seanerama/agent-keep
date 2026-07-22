"""local-tools component — the in-process local tool registry (stage 6).

The registry the tool executor selects from: the harmless demo tools that make
the executor buildable and testable without MCP (stage 7 plugs MCP servers into
the same executor seam), PLUS the read-only worker-analyzer operations (stage 33
— the Mechanic spine; see `worker_analyzer`). The spec's grants select FROM this
registry; a tool without a grant is never registered with the executor and
therefore does not exist in the model-visible tool list (absence, not denial —
contract agent-spec, rule 2).

Registration wiring (ADR 0010): this module imports `worker_analyzer` and merges
its `build_tools()` into `REGISTRY` at the bottom. Registration is thus a side
effect of importing `local_tools` — exactly what `wiring._local_tool_names()`
and `ensure_buildable` do — so the analyzer ops are present at build-check time.
The dependency is one-way (`local_tools` → `worker_analyzer`); the analyzer
never imports this module at module scope, so there is no import cycle.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_runtime.components import worker_analyzer


class ToolInputError(ValueError):
    """The tool rejected its arguments (reported to the model as an error result)."""


@dataclass(frozen=True)
class LocalTool:
    """One locally-implemented tool: model-visible description + the callable."""

    name: str  # tool name within the registry (the spec grant's `name`)
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON-Schema-style properties
    run: Callable[[Mapping[str, Any]], str] = lambda args: ""
    #: Per-op read-only evidence (#109), mirroring MCP's `annotations.readOnlyHint`.
    #: A read-only SCOPE grant binding a `read_only=False` op is REFUSED by the
    #: executor (`build_executor`), just like the MCP read-only boundary. Defaults
    #: False (least assumption): a new op must opt IN to being callable under a
    #: read-only grant. Every op the registry ships today is genuinely read-only.
    read_only: bool = False


def _clock_now(args: Mapping[str, Any]) -> str:
    if args:
        raise ToolInputError(f"clock.now takes no arguments; got {sorted(args)}")
    return datetime.now(UTC).isoformat()


MAX_REPEATS = 10


def _echo_repeat(args: Mapping[str, Any]) -> str:
    unknown = sorted(set(args) - {"text", "times"})
    if unknown:
        raise ToolInputError(f"echo.repeat: unknown argument(s) {unknown}")
    text = args.get("text")
    if not isinstance(text, str) or not text:
        raise ToolInputError("echo.repeat requires a non-empty string 'text'")
    times = args.get("times", 1)
    if isinstance(times, bool) or not isinstance(times, int) or not 1 <= times <= MAX_REPEATS:
        raise ToolInputError(f"echo.repeat: 'times' must be an integer in 1-{MAX_REPEATS}")
    return " ".join([text] * times)


#: The exhaustive registry this component offers. Wiring validates grants
#: against these names at build AND boot; the executor registers only the
#: granted subset.
REGISTRY: dict[str, LocalTool] = {
    "clock.now": LocalTool(
        name="clock.now",
        description="Current UTC time as an RFC 3339 timestamp. Takes no arguments.",
        parameters={},
        run=_clock_now,
        read_only=True,  # reads the clock, mutates nothing
    ),
    "echo.repeat": LocalTool(
        name="echo.repeat",
        description=f"Repeat 'text' 'times' times (1-{MAX_REPEATS}), space-separated.",
        parameters={
            "text": {"type": "string", "description": "Text to repeat."},
            "times": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_REPEATS,
                "description": "Repetition count (default 1).",
            },
        },
        run=_echo_repeat,
        read_only=True,  # pure function of its arguments, mutates nothing
    ),
}

# Merge the read-only worker-analyzer ops (stage 33) into the one registry. This
# runs on import of local_tools, so the analyzer ops are visible to the executor,
# `_local_tool_names()`, and `ensure_buildable` exactly like the demo tools.
REGISTRY.update(worker_analyzer.build_tools())

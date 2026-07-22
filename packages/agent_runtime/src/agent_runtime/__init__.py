"""agent_runtime — the component library agents are composed from.

Core modules (always shipped): messages, audit, provider, queues, sessions,
core, wiring, runner. Component modules live under `agent_runtime.components`,
one module per component, so the composer can include or exclude each
individually — an unselected component is ABSENT from the built image
(contract: agent-spec, binding rule 2).
"""

__version__ = "0.8.0"

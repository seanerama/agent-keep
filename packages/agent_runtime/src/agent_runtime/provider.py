"""Model provider interface (ADR 0004 — `static` is a first-class implementation).

Every provider — hermetic or real — implements this same interface; the spec's
models section selects which provider component ships in the image.

Stage 6 (tool executor) extends this module ADDITIVELY: prompts may carry the
model-visible tool list (built from the spec's grants — an ungranted tool does
not exist here) and replies may carry tool-call requests. Tool-less prompts and
replies are bit-identical to the pre-stage-6 shapes (every new field defaults
to empty).

Stage 9 (router + budgets) extends it ADDITIVELY again: the per-session budget
verdict types live here because they ride the same seam — the core consults
the router (components/model_router) before invoking a provider, and a spec
without tiers/budgets wires no router at all (absence semantics).
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class PromptMessage:
    role: Literal["user", "assistant", "tool"]
    text: str


@dataclass(frozen=True)
class ToolDescriptor:
    """One model-visible tool. Only granted tools are ever described (absence,
    not denial), and pinned constraint parameters are stripped from
    `parameters` — the model cannot see, let alone set, a pinned value."""

    name: str  # fully qualified '<server>.<tool>' as declared in the spec
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON-Schema-style properties


@dataclass(frozen=True)
class ToolCallRequest:
    """A tool call the model requests; resolved by the tool executor."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """The executor's answer to one ToolCallRequest, fed back to the model."""

    call_id: str
    name: str
    status: Literal["ok", "error", "denied", "pending_approval"]
    output: str


@dataclass(frozen=True)
class AssembledPrompt:
    """Output of the prompt assembler: system context + conversation turns.

    Untrusted content is already marked (fenced) by the assembler before it
    reaches any provider — see components/prompt_assembler.py. `tools` is
    attached by the core (from the executor's granted registry), never by the
    assembler; empty means no tool exists for this call.
    """

    system: str
    messages: list[PromptMessage] = field(default_factory=list)
    tools: tuple[ToolDescriptor, ...] = ()


@dataclass(frozen=True)
class ProviderReply:
    text: str
    tokens_in: int
    tokens_out: int
    tool_calls: tuple[ToolCallRequest, ...] = ()


class ModelProvider(Protocol):
    name: str

    async def complete(self, prompt: AssembledPrompt) -> ProviderReply: ...


class BudgetExceededError(RuntimeError):
    """A session's model budget is exhausted and the spec says refuse.

    Raised by the core BEFORE the provider is invoked (the refused call is
    still audited: `model_call` with `outcome.status: denied` — contract
    audit-record). The message loop stays alive; the triggering message fails
    gracefully with this error's text.
    """


@dataclass(frozen=True)
class BudgetVerdict:
    """The router's answer to "may this session spend more model tokens?".

    - ``ok``     — under budget (or no budget declared); call proceeds.
    - ``warn``   — over budget with ``onExceed: warn``; call proceeds, the
                   core logs and writes a ``budget_warning`` audit record.
    - ``refuse`` — over budget with ``onExceed: block``; the core writes a
                   denied ``model_call`` record and raises BudgetExceededError.
    """

    action: Literal["ok", "warn", "refuse"]
    note: str = ""

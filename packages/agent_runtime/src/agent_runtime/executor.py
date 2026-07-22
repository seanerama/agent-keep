"""Tool executor — the security heart of the runtime (blueprint: capabilities/executor).

Resolves model tool-call requests against the spec's granted tools ONLY (a
tool without a grant does not exist — absence, not denial), FORCES per-grant
constraint pins server-side, enforces `approval.policy` (default-deny
`allowlist-confirm-rest`), and audits every path per contracts/audit-record.md:

- unknown tool          -> `tool_call` denied, error result back to the model
- pinned-param override -> `tool_call` denied, error result back to the model
- autoApprove match     -> executed; `tool_call` ok/error, decided_by `policy:auto`
- everything else       -> parked; `approval` event with status `pending_approval`,
  then `approve/deny(call_id, decided_by)` writes the terminal `tool_call`
  record carrying the human decision — with the ORIGINAL trigger.

A call without a trigger is REFUSED before anything runs (the audit-record
contract: an unrecordable call must not execute). This module ships in an
image only when the spec grants tools (absence semantics); MCP servers plug
into this same seam through the stage-7 mcp-manager component, which also
owns their lifecycle — `ToolExecutor.close()` tears the transports down.
"""

import hashlib
import hmac
import json
import os
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from agent_runtime.audit import (
    Action,
    AgentIdentity,
    Approval,
    AuditRecord,
    AuditSink,
    Outcome,
    Trigger,
    TriggerRefusedError,
)
from agent_runtime.provider import ToolCallRequest, ToolDescriptor, ToolResult
from agent_runtime.wiring import ComponentNotImplementedError, load_component
from keep_spec import AgentSpec, LocalTransport

#: decided_by value when the approval policy decided without a human.
POLICY_AUTO = "policy:auto"


class PendingCallError(LookupError):
    """No parked call with that id."""


class AlreadyDecidedError(RuntimeError):
    """The parked call was already approved or denied."""


class ToolDeniedError(RuntimeError):
    """A tool binding refused the call as a POLICY denial, not a tool failure.

    Raised from inside a GrantedTool.run implementation — e.g. the MCP
    manager's read-only scope boundary — and recorded by the executor as
    outcome 'denied' (never 'error'): the tool did not run at all."""


@dataclass(frozen=True)
class GrantedTool:
    """One tool the spec grants, bound to its implementation."""

    name: str  # fully qualified '<server>.<tool>'
    scope: str
    description: str
    parameters: dict[str, Any]
    constraints: Mapping[str, str | int | bool] | None
    run: Callable[[Mapping[str, Any]], str]


@dataclass
class PendingCall:
    """A parked tool call awaiting a human decision; keeps its ORIGINAL trigger."""

    call_id: str
    call: ToolCallRequest
    arguments: dict[str, Any]  # constraint pins already forced
    trigger: Trigger
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    state: Literal["pending", "approved", "denied"] = "pending"
    decided_by: str | None = None
    result: ToolResult | None = None


def _arguments_digest(arguments: Mapping[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _arguments_summary(name: str, arguments: Mapping[str, Any]) -> str:
    """Redacted summary: argument NAMES only — digests-not-payloads (contract)."""
    keys = ", ".join(sorted(arguments)) or "none"
    return f"tool call {name}; argument(s): {keys}"


class ToolExecutor:
    """Registry of granted tools; builds the model-visible tool list FROM THE SPEC."""

    def __init__(
        self,
        *,
        identity: AgentIdentity,
        audit_sink: AuditSink,
        tools: Iterable[GrantedTool],
        auto_approve: Collection[str],
        closers: Iterable[Callable[[], None]] = (),
    ) -> None:
        self._identity = identity
        self._audit_sink = audit_sink
        self._closers = list(closers)
        self._tools: dict[str, GrantedTool] = {}
        for tool in tools:
            if tool.name in self._tools:
                # A last-wins overwrite would silently bind calls to the
                # wrong tool (#34) — refuse the double-bind at construction.
                raise ValueError(f"duplicate tool name in the registry: '{tool.name}'")
            self._tools[tool.name] = tool
        undeclared = sorted(set(auto_approve) - set(self._tools))
        if undeclared:
            # The spec cross-validates this; re-checked here so a hand-wired
            # executor cannot pre-approve a tool that does not exist.
            raise ValueError(f"autoApprove names ungranted tool(s): {undeclared}")
        self._auto_approve = frozenset(auto_approve)
        self._pending: dict[str, PendingCall] = {}

    # ------------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Tear down transport resources behind the bindings (stage 7: the MCP
        manager's child processes/sessions). Idempotent; local tools need none."""
        closers, self._closers = self._closers, []
        for closer in closers:
            closer()

    # ------------------------------------------------------------- model-visible list

    def visible_tools(self) -> tuple[ToolDescriptor, ...]:
        """Granted tools only; pinned constraint params are stripped from the schema."""
        descriptors = []
        for tool in self._tools.values():
            pins = tool.constraints or {}
            descriptors.append(
                ToolDescriptor(
                    name=tool.name,
                    description=tool.description,
                    parameters={k: v for k, v in tool.parameters.items() if k not in pins},
                )
            )
        return tuple(descriptors)

    # ------------------------------------------------------------------ execution path

    async def execute(self, call: ToolCallRequest, trigger: Trigger | None) -> ToolResult:
        """Resolve one model tool-call request: grant lookup, pins, approval gate."""
        if trigger is None:
            raise TriggerRefusedError(
                "tool call refused: no trigger — an unrecordable call must not execute"
            )
        tool = self._tools.get(call.name)
        if tool is None:
            output = f"unknown tool '{call.name}': no such grant exists in this agent's spec"
            self._record_tool_call(
                name=call.name,
                arguments=call.arguments,
                trigger=trigger,
                status="denied",
                approval=Approval(required=False, decided_by=POLICY_AUTO),
            )
            return ToolResult(call_id=call.id, name=call.name, status="denied", output=output)

        pins = dict(tool.constraints or {})
        overridden = sorted(
            key for key, pin in pins.items() if key in call.arguments and call.arguments[key] != pin
        )
        if overridden:
            output = (
                f"constraint violation: parameter(s) {overridden} are pinned by the spec "
                "and cannot be overridden"
            )
            self._record_tool_call(
                name=call.name,
                arguments=call.arguments,  # digest what was ATTEMPTED
                trigger=trigger,
                status="denied",
                approval=Approval(required=False, decided_by=POLICY_AUTO),
            )
            return ToolResult(call_id=call.id, name=call.name, status="denied", output=output)

        arguments = {**call.arguments, **pins}  # pins are FORCED server-side

        if call.name in self._auto_approve:
            return self._run(
                tool,
                call,
                arguments,
                trigger,
                approval=Approval(required=False, decided_by=POLICY_AUTO),
            )

        # Default-deny: park the call, queryable + decidable via approve/deny.
        pending_id = str(uuid4())
        self._pending[pending_id] = PendingCall(
            call_id=pending_id, call=call, arguments=arguments, trigger=trigger
        )
        self._audit_sink.append(
            AuditRecord(
                agent=self._identity,
                event="approval",
                trigger=trigger,
                action=Action(
                    name=call.name,
                    input_digest=_arguments_digest(arguments),
                    input_summary=_arguments_summary(call.name, arguments),
                ),
                outcome=Outcome(status="pending_approval"),
            )
        )
        return ToolResult(
            call_id=call.id,
            name=call.name,
            status="pending_approval",
            output=f"call parked pending human approval (approval id: {pending_id})",
        )

    # --------------------------------------------------------------- pending approvals

    def pending(self) -> list[dict[str, Any]]:
        """Queryable view of undecided parked calls."""
        return [
            {
                "call_id": entry.call_id,
                "tool": entry.call.name,
                "arguments": dict(entry.arguments),
                "requested_at": entry.requested_at.isoformat(),
                "state": entry.state,
            }
            for entry in self._pending.values()
            if entry.state == "pending"
        ]

    def resolve(
        self, call_id: str, decision: Literal["approve", "deny"], decided_by: str
    ) -> ToolResult:
        """Decide a parked call. Executes on approve; refuses on deny. Audited either way
        with the ORIGINAL trigger and the decider's identity."""
        if not decided_by or not decided_by.strip():
            raise ValueError("decided_by must identify the deciding user")
        entry = self._pending.get(call_id)
        if entry is None:
            raise PendingCallError(f"no pending call with id {call_id!r}")
        if entry.state != "pending":
            raise AlreadyDecidedError(f"call {call_id!r} was already {entry.state}")
        tool = self._tools[entry.call.name]
        if decision == "deny":
            entry.state = "denied"
            entry.decided_by = decided_by
            self._record_tool_call(
                name=entry.call.name,
                arguments=entry.arguments,
                trigger=entry.trigger,
                status="denied",
                approval=Approval(required=True, decided_by=decided_by),
            )
            result = ToolResult(
                call_id=entry.call.id,
                name=entry.call.name,
                status="denied",
                output=f"denied by {decided_by}",
            )
        else:
            entry.state = "approved"
            entry.decided_by = decided_by
            result = self._run(
                tool,
                entry.call,
                entry.arguments,
                entry.trigger,
                approval=Approval(required=True, decided_by=decided_by),
            )
        entry.result = result
        return result

    # ------------------------------------------------------------------------ internals

    def _run(
        self,
        tool: GrantedTool,
        call: ToolCallRequest,
        arguments: dict[str, Any],
        trigger: Trigger,
        *,
        approval: Approval,
    ) -> ToolResult:
        try:
            output = tool.run(arguments)
        except ToolDeniedError as exc:
            # The binding itself refused the call as a policy denial (e.g. the
            # MCP manager's read-only scope boundary): nothing ran — 'denied'.
            self._record_tool_call(
                name=tool.name,
                arguments=arguments,
                trigger=trigger,
                status="denied",
                approval=approval,
            )
            return ToolResult(call_id=call.id, name=tool.name, status="denied", output=str(exc))
        except Exception as exc:  # a failed tool call is still a tool call — recorded
            self._record_tool_call(
                name=tool.name,
                arguments=arguments,
                trigger=trigger,
                status="error",
                approval=approval,
            )
            return ToolResult(
                call_id=call.id, name=tool.name, status="error", output=f"tool error: {exc}"
            )
        self._record_tool_call(
            name=tool.name,
            arguments=arguments,
            trigger=trigger,
            status="ok",
            approval=approval,
            output=output,
        )
        return ToolResult(call_id=call.id, name=tool.name, status="ok", output=output)

    def _record_tool_call(
        self,
        *,
        name: str,
        arguments: Mapping[str, Any],
        trigger: Trigger,
        status: Literal["ok", "error", "denied", "pending_approval"],
        approval: Approval,
        output: str | None = None,
    ) -> None:
        digest = None
        if output is not None:
            digest = "sha256:" + hashlib.sha256(output.encode("utf-8")).hexdigest()
        self._audit_sink.append(
            AuditRecord(
                agent=self._identity,
                event="tool_call",
                trigger=trigger,
                action=Action(
                    name=name,
                    input_digest=_arguments_digest(arguments),
                    input_summary=_arguments_summary(name, arguments),
                ),
                outcome=Outcome(status=status, output_digest=digest),
                approval=approval,
            )
        )


def build_executor(
    spec: AgentSpec,
    *,
    identity: AgentIdentity,
    audit_sink: AuditSink,
    environ: Mapping[str, str] | None = None,
) -> ToolExecutor:
    """Wire a ToolExecutor from the spec's grants.

    Local-transport grants bind against the in-process local_tools registry;
    MCP grants (stdio/http) bind through the stage-7 mcp-manager component,
    which connects the declared servers AT BOOT and projects only the
    allowlisted tools into the registry. `environ` is where secretEnvs resolve
    from (defaults to os.environ; injectable for tests). The returned
    executor's close() tears the MCP transports down again.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    granted: list[GrantedTool] = []
    closers: list[Callable[[], None]] = []
    manager: Any = None
    try:
        for server in spec.spec.tools:
            if isinstance(server.transport, LocalTransport):
                registry = load_component("local-tools").REGISTRY
                for grant in server.allow:
                    impl = registry.get(grant.name)
                    if impl is None:
                        raise ComponentNotImplementedError(
                            f"component not implemented: local tool '{grant.name}' "
                            f"(spec.tools['{server.name}'].allow)"
                        )
                    qualified = f"{server.name}.{grant.name}"
                    # Enforce read-only SCOPE for local tools exactly as the MCP
                    # manager does (#109). A read-only grant to an op with no
                    # read-only evidence (`LocalTool.read_only`) binds a refuse()
                    # runner instead of the impl — the call is REFUSED, not
                    # silently executed. Scope only TIGHTENS: a read-write grant
                    # runs any op unchanged.
                    #
                    # Documented limitation: a read-write grant to a genuinely
                    # MUTATING local op still runs unchecked — acceptable because
                    # NO mutating local op exists today (the demo tools are
                    # read-only; the stage-33 analyzer is diff-only, never
                    # applies). This mirrors MCP: scope refuses read-only→mutating,
                    # it does not sandbox a read-write grant.
                    run = impl.run
                    if grant.scope == "read-only" and not impl.read_only:
                        message = (
                            f"scope violation: the grant for '{qualified}' is read-only but the "
                            "local tool provides no read-only evidence (LocalTool.read_only) "
                            "— refused at the local-tools boundary"
                        )

                        def refuse(arguments: Mapping[str, Any], _message: str = message) -> str:
                            raise ToolDeniedError(_message)

                        run = refuse
                    granted.append(
                        GrantedTool(
                            name=qualified,
                            scope=grant.scope,
                            description=impl.description,
                            parameters=dict(impl.parameters),
                            constraints=grant.constraints,
                            run=run,
                        )
                    )
                continue
            if manager is None:
                manager = load_component("mcp-manager").McpManager(environ=env)
                closers.append(manager.close)
            granted.extend(manager.bind_server(server))
        # Constructed INSIDE the try (#46): ToolExecutor's own construction
        # checks (duplicate registry names, the autoApprove re-check) can
        # raise AFTER children were spawned — that path must tear them down
        # too, not leak them until interpreter exit.
        return ToolExecutor(
            identity=identity,
            audit_sink=audit_sink,
            tools=granted,
            auto_approve=spec.spec.approval.autoApprove,
            closers=closers,
        )
    except Exception:
        # A failed boot must not leak already-spawned MCP children (orphan
        # prevention applies to the error path too).
        if manager is not None:
            manager.close()
        raise


# ----------------------------------------------------------- dev-http approval routes


class ApprovalHttpRoutes:
    """dev-http extension routes for the pending-approval flow (stage 6):

    - ``GET /pending`` — list undecided parked calls
    - ``POST /approve/{id}`` — body ``{"decision": "approve"|"deny", "decided_by": "..."}``

    Owner-only STUB auth: a shared secret from the APPROVAL_SECRET env var
    (name-only — the spec never carries values), presented in the
    ``X-Approval-Secret`` header. No secret configured => the endpoints refuse
    (503). Human approval UI/channels are a later stage.
    """

    def __init__(self, executor: ToolExecutor, *, secret: str | None) -> None:
        self._executor = executor
        self._secret = secret or None

    async def __call__(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, dict[str, Any]] | None:
        if path == "/pending" and method == "GET":
            denied = self._authorize(headers)
            if denied is not None:
                return denied
            return 200, {"pending": self._executor.pending()}
        if path.startswith("/approve/") and method == "POST":
            denied = self._authorize(headers)
            if denied is not None:
                return denied
            return self._approve(path.removeprefix("/approve/"), body)
        return None  # not ours — dev-http falls through to 404

    def _authorize(self, headers: Mapping[str, str]) -> tuple[int, dict[str, Any]] | None:
        if self._secret is None:
            return 503, {"error": "approval endpoints disabled: APPROVAL_SECRET is not set"}
        presented = headers.get("x-approval-secret", "")
        if not hmac.compare_digest(presented.encode(), self._secret.encode()):
            return 401, {"error": "missing or invalid X-Approval-Secret"}
        return None

    def _approve(self, call_id: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        try:
            payload = json.loads(body or b"")
        except ValueError:
            # ValueError is the shared parent of json.JSONDecodeError AND the
            # UnicodeDecodeError an invalid-UTF-8 body raises inside json.loads
            # — the dev_http/webex body-parse pattern (stage 19, #58). Every
            # undecodable body is the caller's 400, never an unhandled 500.
            return 400, {"error": "body must be a JSON object"}
        if not isinstance(payload, dict):
            return 400, {"error": "body must be a JSON object"}
        decision = payload.get("decision")
        decided_by = payload.get("decided_by")
        if decision not in ("approve", "deny"):
            return 400, {"error": "field 'decision' must be 'approve' or 'deny'"}
        if not isinstance(decided_by, str) or not decided_by.strip():
            return 400, {"error": "field 'decided_by' must be a non-empty string"}
        try:
            result = self._executor.resolve(call_id, decision, decided_by)
        except PendingCallError as exc:
            return 404, {"error": str(exc)}
        except AlreadyDecidedError as exc:
            return 409, {"error": str(exc)}
        return 200, {
            "call_id": call_id,
            "decision": decision,
            "decided_by": decided_by,
            "status": result.status,
            "output": result.output,
        }

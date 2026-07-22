"""Stage-6 unit tests: tool executor, approval gate, constraint pins, tool loop.

Every path must write its audit record with the originating trigger; a call
without a trigger is refused before anything runs (contract audit-record).
"""

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_runtime.audit import AgentIdentity, AuditRecord, Trigger, TriggerRefusedError
from agent_runtime.components.local_tools import REGISTRY
from agent_runtime.components.memory_queue import InProcessQueue
from agent_runtime.components.prompt_assembler import PromptAssembler
from agent_runtime.components.single_session import SingleSessionManager
from agent_runtime.components.static_provider import StaticProvider
from agent_runtime.core import MAX_TOOL_ROUNDS, AgentCore
from agent_runtime.executor import (
    POLICY_AUTO,
    AlreadyDecidedError,
    ApprovalHttpRoutes,
    GrantedTool,
    PendingCallError,
    ToolExecutor,
    build_executor,
)
from agent_runtime.messages import InternalMessage
from agent_runtime.provider import AssembledPrompt, ProviderReply, ToolCallRequest
from agent_runtime.queues import QueueItem
from keep_spec import validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
SKELETON_SPEC = REPO_ROOT / "examples" / "skeleton.yaml"

IDENTITY = AgentIdentity(slug="tooled", spec_version="0.1.0", image_digest="sha256:test")
TRIGGER = Trigger(message_id="m-1", purpose="unit test")


class _ListSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.records.append(record)


def _echo_grant(constraints: dict[str, Any] | None = None) -> GrantedTool:
    impl = REGISTRY["echo.repeat"]
    return GrantedTool(
        name="local-demo.echo.repeat",
        scope="read-only",
        description=impl.description,
        parameters=dict(impl.parameters),
        constraints=constraints,
        run=impl.run,
    )


def _executor(
    sink: _ListSink,
    *,
    constraints: dict[str, Any] | None = None,
    auto_approve: tuple[str, ...] = (),
) -> ToolExecutor:
    return ToolExecutor(
        identity=IDENTITY,
        audit_sink=sink,
        tools=[_echo_grant(constraints)],
        auto_approve=auto_approve,
    )


def _call(name: str = "local-demo.echo.repeat", **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(id="c-1", name=name, arguments=arguments)


# ------------------------------------------------------------------ execution paths


def test_no_trigger_is_refused_before_anything_runs() -> None:
    sink = _ListSink()
    executor = _executor(sink, auto_approve=("local-demo.echo.repeat",))
    with pytest.raises(TriggerRefusedError):
        asyncio.run(executor.execute(_call(text="hi"), None))
    assert sink.records == [], "an unrecordable call must not execute (or record)"


def test_unknown_tool_is_denied_and_audited() -> None:
    sink = _ListSink()
    executor = _executor(sink)
    result = asyncio.run(executor.execute(_call(name="local-demo.shell.exec"), TRIGGER))
    assert result.status == "denied"
    assert "unknown tool" in result.output
    [record] = sink.records
    assert record.event == "tool_call"
    assert record.outcome.status == "denied"
    assert record.action.name == "local-demo.shell.exec"
    assert record.trigger.message_id == "m-1"
    assert record.approval is not None
    assert record.approval.required is False
    assert record.approval.decided_by == POLICY_AUTO


def test_pinned_param_override_is_denied_and_audited() -> None:
    sink = _ListSink()
    executor = _executor(sink, constraints={"times": 2}, auto_approve=("local-demo.echo.repeat",))
    result = asyncio.run(executor.execute(_call(text="hi", times=9), TRIGGER))
    assert result.status == "denied"
    assert "pinned" in result.output
    [record] = sink.records
    assert record.event == "tool_call"
    assert record.outcome.status == "denied"
    assert record.approval is not None and record.approval.decided_by == POLICY_AUTO


def test_pinned_params_are_forced_server_side() -> None:
    """The pin rides into execution even when the model omits the parameter."""
    sink = _ListSink()
    executor = _executor(sink, constraints={"times": 3}, auto_approve=("local-demo.echo.repeat",))
    result = asyncio.run(executor.execute(_call(text="hi"), TRIGGER))
    assert result.status == "ok"
    assert result.output == "hi hi hi"


def test_pinned_params_are_stripped_from_the_model_visible_schema() -> None:
    executor = _executor(_ListSink(), constraints={"times": 3})
    [descriptor] = executor.visible_tools()
    assert "times" not in descriptor.parameters
    assert "text" in descriptor.parameters


def test_auto_approve_executes_with_policy_auto() -> None:
    sink = _ListSink()
    executor = _executor(sink, auto_approve=("local-demo.echo.repeat",))
    result = asyncio.run(executor.execute(_call(text="hi", times=2), TRIGGER))
    assert result.status == "ok"
    assert result.output == "hi hi"
    [record] = sink.records
    assert record.event == "tool_call"
    assert record.outcome.status == "ok"
    assert record.outcome.output_digest is not None
    assert record.approval is not None
    assert record.approval.required is False
    assert record.approval.decided_by == POLICY_AUTO


def test_tool_error_is_recorded_as_error() -> None:
    sink = _ListSink()
    executor = _executor(sink, auto_approve=("local-demo.echo.repeat",))
    result = asyncio.run(executor.execute(_call(text=""), TRIGGER))  # invalid input
    assert result.status == "error"
    [record] = sink.records
    assert record.event == "tool_call"
    assert record.outcome.status == "error"


def test_auto_approve_must_name_granted_tools() -> None:
    with pytest.raises(ValueError, match="ungranted"):
        _executor(_ListSink(), auto_approve=("local-demo.shell.exec",))


def test_duplicate_tool_names_fail_at_construction() -> None:
    """#34: two grants sharing one qualified name must not last-wins overwrite
    in the registry — that silently binds calls to the wrong tool. The
    executor refuses at construction instead."""
    with pytest.raises(ValueError, match=r"local-demo\.echo\.repeat"):
        ToolExecutor(
            identity=IDENTITY,
            audit_sink=_ListSink(),
            tools=[_echo_grant(), _echo_grant()],
            auto_approve=(),
        )


# ------------------------------------------------------------------- approval gate


def test_non_approved_call_parks_then_executes_on_approve() -> None:
    sink = _ListSink()
    executor = _executor(sink)
    result = asyncio.run(executor.execute(_call(text="hi", times=2), TRIGGER))
    assert result.status == "pending_approval"

    [pending_record] = sink.records
    assert pending_record.event == "approval"
    assert pending_record.outcome.status == "pending_approval"
    assert pending_record.trigger.message_id == "m-1"

    [entry] = executor.pending()
    assert entry["tool"] == "local-demo.echo.repeat"
    assert entry["state"] == "pending"

    decided = executor.resolve(entry["call_id"], "approve", "user-42")
    assert decided.status == "ok"
    assert decided.output == "hi hi"
    assert executor.pending() == [], "decided calls leave the pending view"

    tool_call = sink.records[-1]
    assert tool_call.event == "tool_call"
    assert tool_call.outcome.status == "ok"
    assert tool_call.approval is not None
    assert tool_call.approval.required is True
    assert tool_call.approval.decided_by == "user-42"
    # the ORIGINAL trigger is threaded into the decision-time record
    assert tool_call.trigger.message_id == "m-1"


def test_non_approved_call_refuses_on_deny() -> None:
    sink = _ListSink()
    executor = _executor(sink)
    asyncio.run(executor.execute(_call(text="hi"), TRIGGER))
    [entry] = executor.pending()
    decided = executor.resolve(entry["call_id"], "deny", "user-42")
    assert decided.status == "denied"
    tool_call = sink.records[-1]
    assert tool_call.event == "tool_call"
    assert tool_call.outcome.status == "denied"
    assert tool_call.approval is not None
    assert tool_call.approval.required is True
    assert tool_call.approval.decided_by == "user-42"
    assert tool_call.trigger.message_id == "m-1"


def test_approved_tool_error_at_decision_time_is_recorded_with_original_trigger() -> None:
    """resolve -> approve on a call whose tool RAISES: the error is recorded as a
    tool_call error record carrying the deciding user and the ORIGINAL trigger."""
    sink = _ListSink()
    executor = _executor(sink)  # nothing auto-approved => the call parks
    # text="" parks fine (nothing runs at park time) but fails inside the tool
    result = asyncio.run(executor.execute(_call(text=""), TRIGGER))
    assert result.status == "pending_approval"

    [entry] = executor.pending()
    decided = executor.resolve(entry["call_id"], "approve", "user-42")
    assert decided.status == "error"
    assert "tool error" in decided.output
    assert executor.pending() == [], "a decided call leaves the pending view, even on error"

    tool_call = sink.records[-1]
    assert tool_call.event == "tool_call"
    assert tool_call.outcome.status == "error"
    assert tool_call.outcome.output_digest is None, "no output — nothing to digest"
    assert tool_call.approval is not None
    assert tool_call.approval.required is True
    assert tool_call.approval.decided_by == "user-42"
    assert tool_call.trigger.message_id == "m-1", (
        "the decision-time error record threads the ORIGINAL trigger"
    )


def test_resolve_guards_unknown_decided_and_blank_decider() -> None:
    sink = _ListSink()
    executor = _executor(sink)
    with pytest.raises(PendingCallError):
        executor.resolve("nope", "approve", "user-42")
    asyncio.run(executor.execute(_call(text="hi"), TRIGGER))
    [entry] = executor.pending()
    with pytest.raises(ValueError, match="decided_by"):
        executor.resolve(entry["call_id"], "approve", "  ")
    executor.resolve(entry["call_id"], "deny", "user-42")
    with pytest.raises(AlreadyDecidedError):
        executor.resolve(entry["call_id"], "approve", "user-42")


# --------------------------------------------------------------- dev-http approvals


def _routes(executor: ToolExecutor, secret: str | None = "s3cret") -> ApprovalHttpRoutes:
    return ApprovalHttpRoutes(executor, secret=secret)


def test_approval_routes_require_the_shared_secret() -> None:
    executor = _executor(_ListSink())
    routes = _routes(executor)
    status, body = asyncio.run(routes("GET", "/pending", {}, None))  # type: ignore[misc]
    assert status == 401
    status, _ = asyncio.run(  # type: ignore[misc]
        routes("GET", "/pending", {"x-approval-secret": "wrong"}, None)
    )
    assert status == 401
    status, body = asyncio.run(  # type: ignore[misc]
        routes("GET", "/pending", {"x-approval-secret": "s3cret"}, None)
    )
    assert status == 200 and body == {"pending": []}


def test_approval_routes_refuse_without_configured_secret() -> None:
    routes = _routes(_executor(_ListSink()), secret=None)
    status, body = asyncio.run(  # type: ignore[misc]
        routes("GET", "/pending", {"x-approval-secret": ""}, None)
    )
    assert status == 503
    assert "APPROVAL_SECRET" in body["error"]


def test_approval_routes_full_flow() -> None:
    sink = _ListSink()
    executor = _executor(sink)
    asyncio.run(executor.execute(_call(text="hi", times=2), TRIGGER))
    routes = _routes(executor)
    auth = {"x-approval-secret": "s3cret"}

    status, body = asyncio.run(routes("GET", "/pending", auth, None))  # type: ignore[misc]
    assert status == 200
    [entry] = body["pending"]
    call_id = entry["call_id"]

    payload = json.dumps({"decision": "approve", "decided_by": "user-42"}).encode()
    status, body = asyncio.run(  # type: ignore[misc]
        routes("POST", f"/approve/{call_id}", auth, payload)
    )
    assert status == 200
    assert body["status"] == "ok"
    assert body["output"] == "hi hi"

    # deciding twice conflicts; unknown ids are 404; bad bodies are 400
    status, _ = asyncio.run(routes("POST", f"/approve/{call_id}", auth, payload))  # type: ignore[misc]
    assert status == 409
    status, _ = asyncio.run(routes("POST", "/approve/nope", auth, payload))  # type: ignore[misc]
    assert status == 404
    status, _ = asyncio.run(routes("POST", f"/approve/{call_id}", auth, b"{}"))  # type: ignore[misc]
    assert status == 400
    # unrelated paths fall through (None) so dev-http answers 404 itself
    assert asyncio.run(routes("GET", "/other", auth, None)) is None


def test_approval_route_invalid_utf8_body_is_400_not_500() -> None:
    """Stage 19 (#58): an invalid-UTF-8 approval body raises UnicodeDecodeError
    inside json.loads — a ValueError subclass the route must catch exactly like
    malformed JSON (the dev_http/webex body-parse pattern), answering 400,
    never an unhandled exception → 500."""
    executor = _executor(_ListSink())
    asyncio.run(executor.execute(_call(text="hi", times=2), TRIGGER))
    routes = _routes(executor)
    auth = {"x-approval-secret": "s3cret"}
    [entry] = executor.pending()
    status, body = asyncio.run(  # type: ignore[misc]
        routes("POST", f"/approve/{entry['call_id']}", auth, b"\x80\x81 not utf-8")
    )
    assert status == 400
    assert body["error"] == "body must be a JSON object"
    # the parked call is untouched — still pending, still decidable
    assert [e["call_id"] for e in executor.pending()] == [entry["call_id"]]


# ------------------------------------------------------------ core tool loop (unit)


def _local_tools_spec_data() -> dict[str, Any]:
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data = copy.deepcopy(data)
    data["spec"]["tools"] = [
        {
            "name": "local-demo",
            "transport": {"kind": "local"},
            "allow": [{"name": "clock.now"}, {"name": "echo.repeat"}],
        }
    ]
    data["spec"]["approval"] = {
        "policy": "allowlist-confirm-rest",
        "autoApprove": ["local-demo.echo.repeat"],
    }
    return data


def _message(text: str = "hello") -> InternalMessage:
    from agent_runtime.components.dev_http import DevHttpAdapter

    adapter = DevHttpAdapter(InProcessQueue())
    return adapter.normalize({"text": text, "conversation_id": "c-1", "sender_id": "tester"})


def test_core_tool_loop_executes_and_audit_chain_shares_the_trigger() -> None:
    sink = _ListSink()
    spec = validate_spec_data(_local_tools_spec_data())
    executor = build_executor(spec, identity=IDENTITY, audit_sink=sink)
    provider = StaticProvider(
        [
            'TOOL_CALL {"name": "local-demo.echo.repeat", '
            '"arguments": {"text": "tick", "times": 2}}',
            "the echo answered",
        ]
    )
    core = AgentCore(
        identity=IDENTITY,
        persona_identity="persona",
        queue=InProcessQueue(),
        sessions=SingleSessionManager(),
        assembler=PromptAssembler(),
        provider=provider,
        audit_sink=sink,
        executor=executor,
    )

    async def run() -> tuple[str, InternalMessage]:
        msg = _message("run the echo")
        item = QueueItem(message=msg, reply=asyncio.get_running_loop().create_future())
        await core.handle(item)
        return await item.reply, msg

    reply, msg = asyncio.run(run())
    assert reply == "the echo answered"
    events = [r.event for r in sink.records]
    assert events == ["model_call", "tool_call", "model_call"]
    assert {r.trigger.message_id for r in sink.records} == {msg.id}, (
        "model_call and tool_call records must share the originating trigger"
    )
    tool_call = sink.records[1]
    assert tool_call.action.name == "local-demo.echo.repeat"
    assert tool_call.outcome.status == "ok"
    assert tool_call.approval is not None and tool_call.approval.decided_by == POLICY_AUTO


class _AlwaysToolCallingProvider:
    """Requests the same auto-approved tool call on EVERY completion until
    switched to plain text — a runaway provider for exercising the loop cap."""

    name = "static"

    def __init__(self) -> None:
        self.plain = False
        self.completions = 0

    async def complete(self, prompt: AssembledPrompt) -> ProviderReply:
        await asyncio.sleep(0)  # yield point so a runaway loop cannot starve the test
        self.completions += 1
        if self.plain:
            return ProviderReply(text="recovered", tokens_in=1, tokens_out=1)
        return ProviderReply(
            text="",
            tokens_in=1,
            tokens_out=1,
            tool_calls=(
                ToolCallRequest(
                    id=f"c-{self.completions}",
                    name="local-demo.echo.repeat",
                    arguments={"text": "hi"},
                ),
            ),
        )


def test_tool_loop_cap_fails_the_message_gracefully_and_loop_survives() -> None:
    """MAX_TOOL_ROUNDS is a security control: a provider that never stops
    requesting tool calls is cut off at the cap; the failure surfaces as THIS
    message's error (the reply future gets the RuntimeError), every executed
    round is audited, and the core loop stays alive for the next message."""
    sink = _ListSink()
    executor = _executor(sink, auto_approve=("local-demo.echo.repeat",))
    provider = _AlwaysToolCallingProvider()
    queue = InProcessQueue()
    core = AgentCore(
        identity=IDENTITY,
        persona_identity="persona",
        queue=queue,
        sessions=SingleSessionManager(),
        assembler=PromptAssembler(),
        provider=provider,
        audit_sink=sink,
        executor=executor,
    )

    async def run() -> tuple[InternalMessage, str]:
        loop_task = asyncio.create_task(core.run())
        loop = asyncio.get_running_loop()
        runaway = QueueItem(message=_message("spin forever"), reply=loop.create_future())
        await queue.put(runaway)
        with pytest.raises(RuntimeError, match="tool loop exceeded"):
            await asyncio.wait_for(runaway.reply, timeout=5)
        # graceful: the SAME core loop must still serve the next message
        provider.plain = True
        followup = QueueItem(message=_message("hello again"), reply=loop.create_future())
        await queue.put(followup)
        reply = await asyncio.wait_for(followup.reply, timeout=5)
        loop_task.cancel()
        return runaway.message, reply

    runaway_msg, followup_reply = asyncio.run(run())
    assert followup_reply == "recovered", "the core loop survived the capped message"

    runaway_records = [r for r in sink.records if r.trigger.message_id == runaway_msg.id]
    tool_calls = [r for r in runaway_records if r.event == "tool_call"]
    model_calls = [r for r in runaway_records if r.event == "model_call"]
    assert len(tool_calls) == MAX_TOOL_ROUNDS, "the loop stopped exactly at the cap"
    assert len(model_calls) == MAX_TOOL_ROUNDS + 1, "one model call per round plus the first"
    assert all(r.outcome.status == "ok" for r in tool_calls)
    assert all(
        r.approval is not None and r.approval.decided_by == POLICY_AUTO for r in tool_calls
    ), "every executed round carries its audit record with the approval block"


def test_static_provider_tool_call_turns_are_deterministic() -> None:
    async def run() -> list[Any]:
        provider = StaticProvider(
            ['TOOL_CALL {"name": "a.b", "arguments": {"x": 1}}', "plain text"]
        )
        prompt = AssembledPrompt(system="p")
        return [await provider.complete(prompt) for _ in range(3)]

    first, second, third = asyncio.run(run())
    assert first.text == "" and len(first.tool_calls) == 1
    assert first.tool_calls[0].name == "a.b"
    assert first.tool_calls[0].arguments == {"x": 1}
    assert first.tool_calls[0].id == "call-0-0"
    assert second.text == "plain text" and second.tool_calls == ()
    assert third.tool_calls[0].id == "call-2-0", "call ids are per-turn deterministic"


# --------------------------------------------------------- runner wiring/kill-switch


def _runnable_spec_data(tmp_path: Path, with_tools: bool) -> dict[str, Any]:
    data = _local_tools_spec_data()
    if not with_tools:
        data["spec"]["tools"] = []
        data["spec"]["approval"] = {}
    data["spec"]["observability"]["audit"]["path"] = str(tmp_path / "audit.jsonl")
    return data


def test_runner_wires_executor_and_approval_routes_for_tool_specs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_runtime.runner import build_app

    monkeypatch.setenv("AGENT_IMAGE_DIGEST", "sha256:" + "ab" * 32)
    monkeypatch.delenv("TOOLS_ENABLED", raising=False)
    monkeypatch.setenv("APPROVAL_SECRET", "s3cret")
    spec = validate_spec_data(_runnable_spec_data(tmp_path, with_tools=True))
    core, adapter = build_app(spec)
    assert core._executor is not None
    assert adapter._extra_routes is not None


def test_tools_enabled_kill_switch_removes_the_tool_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_runtime.runner import build_app

    monkeypatch.setenv("AGENT_IMAGE_DIGEST", "sha256:" + "ab" * 32)
    monkeypatch.setenv("TOOLS_ENABLED", "0")
    spec = validate_spec_data(_runnable_spec_data(tmp_path, with_tools=True))
    core, adapter = build_app(spec)
    assert core._executor is None, "kill-switch OFF removes the executor entirely"
    assert adapter._extra_routes is None, "kill-switch OFF removes the approval routes"


def test_tool_less_spec_never_wires_the_tool_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_runtime.runner import build_app, tools_enabled

    monkeypatch.setenv("AGENT_IMAGE_DIGEST", "sha256:" + "ab" * 32)
    monkeypatch.setenv("TOOLS_ENABLED", "1")  # the flag cannot conjure ungranted tools
    spec = validate_spec_data(_runnable_spec_data(tmp_path, with_tools=False))
    assert tools_enabled(spec) is False
    core, adapter = build_app(spec)
    assert core._executor is None
    assert adapter._extra_routes is None


# ------------------------------------------ #109: local-tool read-only scope enforcement


def _local_scope_spec_data(name: str, scope: str) -> dict[str, Any]:
    """A spec granting ONE local op `name` with `scope`, auto-approved so its
    runner (real or the refuse() runner) fires without parking."""
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data = copy.deepcopy(data)
    data["spec"]["tools"] = [
        {
            "name": "local-demo",
            "transport": {"kind": "local"},
            "allow": [{"name": name, "scope": scope}],
        }
    ]
    data["spec"]["approval"] = {
        "policy": "allowlist-confirm-rest",
        "autoApprove": [f"local-demo.{name}"],
    }
    return data


def test_read_only_grant_to_mutating_local_op_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """#109: a read-only SCOPE grant binding a local op with NO read-only
    evidence (`LocalTool.read_only=False`) is REFUSED at bind time — the bound
    runner raises ToolDeniedError, the impl never runs. (Before this fix the
    executor stored `scope` but never read it: the mutating op executed
    identically under read-only and read-write — the security gap #109.)"""
    from agent_runtime.components.local_tools import REGISTRY, LocalTool

    ran = []

    def _mutate(args: Any) -> str:
        ran.append(args)
        return "MUTATED"

    monkeypatch.setitem(
        REGISTRY,
        "danger.write",
        LocalTool(name="danger.write", description="mutates", run=_mutate, read_only=False),
    )
    spec = validate_spec_data(_local_scope_spec_data("danger.write", "read-only"))
    sink = _ListSink()
    executor = build_executor(spec, identity=IDENTITY, audit_sink=sink)
    result = asyncio.run(executor.execute(_call(name="local-demo.danger.write"), TRIGGER))
    assert result.status == "denied", "read-only grant to a mutating local op must be refused"
    assert "scope violation" in result.output
    assert ran == [], "the mutating impl must never have executed"
    [record] = sink.records
    assert record.outcome.status == "denied"


def test_read_write_grant_to_mutating_local_op_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """#109: scope only TIGHTENS — a read-write grant still runs any op
    (mirrors MCP; the documented limitation is that a read-write grant to a
    genuinely mutating op executes unchecked, acceptable as none ship today)."""
    from agent_runtime.components.local_tools import REGISTRY, LocalTool

    monkeypatch.setitem(
        REGISTRY,
        "danger.write",
        LocalTool(
            name="danger.write", description="mutates", run=lambda a: "MUTATED", read_only=False
        ),
    )
    spec = validate_spec_data(_local_scope_spec_data("danger.write", "read-write"))
    executor = build_executor(spec, identity=IDENTITY, audit_sink=_ListSink())
    result = asyncio.run(executor.execute(_call(name="local-demo.danger.write"), TRIGGER))
    assert result.status == "ok"
    assert result.output == "MUTATED"


def test_read_only_grant_to_read_only_local_op_runs() -> None:
    """#109: a read-only op under a read-only grant is UNAFFECTED — the demo
    tools (and stage-33 analyzer ops) are read_only=True, so they run."""
    spec = validate_spec_data(_local_scope_spec_data("echo.repeat", "read-only"))
    executor = build_executor(spec, identity=IDENTITY, audit_sink=_ListSink())
    result = asyncio.run(
        executor.execute(_call(name="local-demo.echo.repeat", text="hi", times=2), TRIGGER)
    )
    assert result.status == "ok"
    assert result.output == "hi hi"


def test_shipping_local_ops_are_all_read_only() -> None:
    """#109: every op the registry ships today declares read_only=True (demo
    tools + the diff-only stage-33 analyzer), so read-only grants keep working."""
    from agent_runtime.components.local_tools import REGISTRY

    mutating = sorted(name for name, impl in REGISTRY.items() if not impl.read_only)
    assert mutating == [], f"unexpected mutating local op(s) shipped: {mutating}"

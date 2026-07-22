"""AgentCore — the message loop: queue -> session -> prompt -> model -> audit -> reply.

Enforces the audit-record contract's execution rule: a model call without a
valid trigger is REFUSED (TriggerRefusedError) before the provider is invoked,
and the completed call is recorded to the append-only sink.

Stage 6 adds the tool loop: when a tool executor is wired (spec grants tools
AND the TOOLS_ENABLED kill-switch is on), the model-visible tool list rides
into every prompt and provider tool-call requests are resolved by the executor
— each carrying the originating message's trigger — with results fed back to
the provider until it answers in text. With no executor wired, prompts and
digests are bit-identical to the tool-less runtime (absence semantics).

Stage 9 adds the router seam: when a model router is wired (spec declares
tiers and/or budgets), the core asks it which provider serves each trigger
and whether the session's budget allows the call — a refused call is audited
(`model_call`, outcome denied) BEFORE BudgetExceededError propagates, and a
warned one writes a `budget_warning` record and proceeds. With no router
wired, the single default provider path is bit-identical to pre-stage-9.

Stage 16 adds the memory read seam: when a memory component is wired (spec
declares `memory:`), each inbound message's top-K similar stored summaries
are recalled and handed to the prompt assembler as a fenced context section.
With no memory wired, prompts are bit-identical to before (absence semantics).
The memory WRITE boundary is the component's own surface — privileged, typed,
and audited there — not part of this loop.

Stage 17 adds the history seam: when a history strategy is wired
(`spec.sessions.history: {strategy: retrieval}`), the core (1) asks the index
for the stored turns most relevant to the inbound message BEFORE indexing it
— a message never retrieves itself — and hands them to the assembler as a
fenced, demarcated history section while the conversation turns collapse to
the current message only; (2) indexes each turn AFTER the session (the
stage-15 tier) durably holds it — the transcript stays the source of truth,
the index is derived. With no strategy wired, prompts are bit-identical to
before (absence semantics).
"""

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from agent_runtime.audit import (
    Action,
    AgentIdentity,
    AuditRecord,
    AuditSink,
    Cost,
    Outcome,
    Trigger,
    TriggerRefusedError,
)
from agent_runtime.messages import InternalMessage
from agent_runtime.provider import (
    AssembledPrompt,
    BudgetExceededError,
    BudgetVerdict,
    ModelProvider,
    PromptMessage,
    ProviderReply,
    ToolCallRequest,
    ToolDescriptor,
    ToolResult,
)
from agent_runtime.queues import MessageQueue, QueueItem
from agent_runtime.sessions import Session, SessionManager, Turn

logger = logging.getLogger(__name__)

#: Hard cap on provider->tool->provider rounds per message — a scripted or
#: misbehaving provider must not spin the loop forever.
MAX_TOOL_ROUNDS = 8


class PromptAssemblerProtocol(Protocol):
    def assemble(
        self,
        persona_identity: str,
        session: Session,
        recalled: Sequence[str] = (),
        retrieved_history: Sequence[str] | None = None,
        facts: Sequence[str] = (),
    ) -> AssembledPrompt: ...


class HistoryStrategyProtocol(Protocol):
    """The history-strategy seam (components/retrieval_history implements it —
    stage 17).

    Structural typing keeps core.py importable in images where the history
    module is ABSENT (specs without sessions.history — absence semantics).
    `relevant` is consulted BEFORE the inbound turn is recorded; `record`
    only ever runs after the session manager durably holds the turn."""

    def relevant(self, session_id: str, text: str) -> Sequence[str]: ...

    def record(self, session_id: str, turn: Turn) -> None: ...


class MemoryRecallProtocol(Protocol):
    """The memory read seam (components/pgvector_memory implements it — stage 16).

    Structural typing keeps core.py importable in images where the memory
    module is ABSENT (specs without a memory section — absence semantics).
    The core only READS through this seam; the component's write boundary is
    its own privileged, audited surface."""

    def recall(self, text: str) -> Sequence[str]: ...


class CommandSurfaceProtocol(Protocol):
    """The runtime command surface (components/facts_memory implements it —
    stage 24: `writePolicy: user-command`).

    Structural typing keeps core.py importable in images where the facts module
    is ABSENT (absence semantics). `handle_command` parses the RAW inbound text
    of an already-admitted (rostered) sender and, when it IS a command, executes
    it deterministically and returns the fixed-template confirmation — a
    non-None return SUPPRESSES the model call for that turn (the model is never
    reachable from the memory write path). None means the text is not a command
    and must flow to the model unchanged."""

    def handle_command(self, text: str, trigger: Trigger) -> str | None: ...


class FactsReadProtocol(Protocol):
    """The facts read seam (components/facts_memory implements it — stage 24).

    All stored facts render into the prompt as a fenced platform=memory section
    (the stage-16 assembler-argument pattern). Structural typing keeps core.py
    importable where the facts module is absent."""

    def facts_block(self) -> Sequence[str]: ...


class ToolExecutorProtocol(Protocol):
    """The executor seam (agent_runtime.executor implements it; the stage-7
    MCP manager plugs its servers into the same seam).

    Structural typing keeps core.py importable in images where the executor
    module is ABSENT (tool-less specs — absence semantics)."""

    def visible_tools(self) -> tuple[ToolDescriptor, ...]: ...

    async def execute(self, call: ToolCallRequest, trigger: Trigger | None) -> ToolResult: ...

    def close(self) -> None: ...


class ModelRouterProtocol(Protocol):
    """The router seam (components/model_router implements it — stage 9).

    Structural typing keeps core.py importable in images where the router
    module is ABSENT (specs without tiers/budgets — absence semantics)."""

    def provider_for(self, trigger: Trigger | None) -> ModelProvider: ...

    def budget_verdict(self, session_id: str) -> BudgetVerdict: ...

    def record_cost(
        self,
        session_id: str,
        *,
        tokens_in: int,
        tokens_out: int,
        provider: ModelProvider | None = None,
    ) -> None: ...


def _sha256(data: str) -> str:
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def prompt_digest(prompt: AssembledPrompt) -> str:
    """sha256 of canonicalized prompt inputs (digests-not-payloads, per contract).

    Tool-less prompts canonicalize exactly as before stage 6 (bit-identical);
    the tool list joins the digest only when present."""
    payload: dict[str, object] = {
        "system": prompt.system,
        "messages": [{"role": m.role, "text": m.text} for m in prompt.messages],
    }
    if prompt.tools:
        payload["tools"] = [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in prompt.tools
        ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _sha256(canonical)


def _trigger_for(message: InternalMessage) -> Trigger:
    """The WHY of this turn (contract audit-record: exactly one of message_id /
    trigger_id).

    A trigger-originated message (stage 18: `sender.kind: system` with an
    `event` content block carrying the intake's activation id) yields a
    trigger with `trigger_id` set to that id, so EVERY record of the turn —
    model_call and tool_call alike — carries the triggering event's identity
    ("every tool call logged with the triggering alarm attached"). The block's
    fields are only ever produced by the trigger components themselves (the
    endpoint constructs the message; payloads cannot reach these fields). Any
    other message keeps the message-id trigger, byte-identical to before.
    """
    if message.sender.kind == "system":
        for block in message.content:
            if block.type != "event":
                continue
            trigger_id = getattr(block, "trigger_id", None)
            if isinstance(trigger_id, str) and trigger_id:
                source = getattr(block, "source", None)
                label = source if isinstance(source, str) and source else message.channel.platform
                return Trigger(
                    trigger_id=trigger_id,
                    purpose=f"handle '{label}' event activation in "
                    f"conversation {message.channel.conversation_id}",
                )
    return Trigger(
        message_id=message.id,
        purpose=f"reply to {message.channel.platform} message in "
        f"conversation {message.channel.conversation_id}",
    )


def _reply_digest(reply: ProviderReply) -> str:
    """Digest of what the model produced; tool calls join it only when present."""
    if not reply.tool_calls:
        return _sha256(reply.text)
    payload = {
        "text": reply.text,
        "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in reply.tool_calls
        ],
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


class AgentCore:
    def __init__(
        self,
        *,
        identity: AgentIdentity,
        persona_identity: str,
        queue: MessageQueue,
        sessions: SessionManager,
        assembler: PromptAssemblerProtocol,
        provider: ModelProvider,
        audit_sink: AuditSink,
        executor: ToolExecutorProtocol | None = None,
        router: ModelRouterProtocol | None = None,
        memory: MemoryRecallProtocol | None = None,
        history: HistoryStrategyProtocol | None = None,
        commands: CommandSurfaceProtocol | None = None,
        facts: FactsReadProtocol | None = None,
    ) -> None:
        self._identity = identity
        self._persona_identity = persona_identity
        self._queue = queue
        self._sessions = sessions
        self._assembler = assembler
        self._provider = provider
        self._audit_sink = audit_sink
        self._executor = executor
        self._router = router
        self._memory = memory
        self._history = history
        self._commands = commands
        self._facts = facts

    @property
    def executor(self) -> ToolExecutorProtocol | None:
        """The wired tool executor, if any — the runner closes it at shutdown
        (stage 7: MCP transport teardown / stdio orphan prevention)."""
        return self._executor

    async def run(self) -> None:
        """Consume the queue forever."""
        while True:
            item = await self._queue.get()
            try:
                await self.handle(item)
            except Exception as exc:  # keep the loop alive; surface the error to the caller
                logger.exception("message handling failed")
                if not item.reply.done():
                    item.reply.set_exception(exc)

    async def handle(self, item: QueueItem) -> None:
        message = item.message
        session = self._sessions.session_for(message)
        trigger = _trigger_for(message)
        # Command surface (stage 24, `writePolicy: user-command`): a
        # deterministic, model-free command from a rostered HUMAN is intercepted
        # AFTER gateway admission (the gate already dropped non-rostered senders
        # upstream) and BEFORE the model. It executes against the facts store;
        # its command turn AND template confirmation enter the transcript
        # (consuming sliding-window slots, Mechanic-citable); and NO model call
        # happens (the write surface is unreachable from model output). A
        # system/trigger-originated message is never a command — it flows to the
        # model as usual.
        if self._commands is not None and message.sender.kind == "human":
            confirmation = self._commands.handle_command(message.text(), trigger)
            if confirmation is not None:
                session.add_message(message)
                if self._history is not None:
                    self._history.record(session.session_id, session.turns[-1])
                session.add_reply(confirmation)
                if self._history is not None:
                    self._history.record(session.session_id, session.turns[-1])
                if not item.reply.done():
                    item.reply.set_result(confirmation)
                return
        # History strategy (stage 17): retrieve the relevant PAST turns before
        # the inbound turn is recorded anywhere — a message never retrieves
        # itself. `None` (no strategy wired) tells the assembler to replay the
        # full transcript, bit-identical to before.
        retrieved: Sequence[str] | None = None
        if self._history is not None:
            retrieved = self._history.relevant(session.session_id, message.text())
        session.add_message(message)
        if self._history is not None:
            # Index only AFTER the session manager holds the turn durably —
            # the transcript is the source of truth, the index derived.
            self._history.record(session.session_id, session.turns[-1])
        # Read path (stage 16): with a memory component wired, the inbound
        # message's top-K similar stored summaries join the prompt as a
        # fenced context section. With none wired, `recalled` stays empty and
        # the assembled prompt is bit-identical to the memory-less runtime.
        recalled: Sequence[str] = ()
        if self._memory is not None:
            recalled = self._memory.recall(message.text())
        # Facts read path (stage 24): with a facts store wired, ALL stored facts
        # join the prompt as a fenced platform=memory section. With none wired
        # `facts` stays empty and the prompt is bit-identical to before.
        facts: Sequence[str] = ()
        if self._facts is not None:
            facts = self._facts.facts_block()
        prompt = self._assembler.assemble(
            self._persona_identity,
            session,
            recalled=recalled,
            retrieved_history=retrieved,
            facts=facts,
        )
        if self._executor is not None:
            # The model-visible tool list comes FROM THE SPEC's grants only;
            # with no executor there is no tool list at all (absence).
            prompt = replace(prompt, tools=self._executor.visible_tools())
        reply = await self.call_model(prompt, trigger, session_id=session.session_id)
        rounds = 0
        while reply.tool_calls and self._executor is not None:
            rounds += 1
            if rounds > MAX_TOOL_ROUNDS:
                raise RuntimeError(
                    f"tool loop exceeded {MAX_TOOL_ROUNDS} rounds for message {message.id}"
                )
            prompt = replace(prompt, messages=await self._tool_round(prompt, reply, trigger))
            reply = await self.call_model(prompt, trigger, session_id=session.session_id)
        session.add_reply(reply.text)
        if self._history is not None:
            # The agent's replies are part of the conversation's past too —
            # indexed with the same durably-held-first ordering as above.
            self._history.record(session.session_id, session.turns[-1])
        if not item.reply.done():
            item.reply.set_result(reply.text)

    async def _tool_round(
        self, prompt: AssembledPrompt, reply: ProviderReply, trigger: Trigger
    ) -> list[PromptMessage]:
        """Execute one round of requested tool calls; return the extended turn list."""
        assert self._executor is not None  # guarded by the caller's loop condition
        turns = list(prompt.messages)
        requested = "; ".join(
            f"{c.name}({json.dumps(c.arguments, sort_keys=True, default=str)})"
            for c in reply.tool_calls
        )
        turns.append(PromptMessage(role="assistant", text=f"[tool calls requested: {requested}]"))
        for call in reply.tool_calls:
            # The executor threads THIS message's trigger into its audit records.
            result = await self._executor.execute(call, trigger)
            text = f"[{result.name} -> {result.status}] {result.output}"
            turns.append(PromptMessage(role="tool", text=text))
        return turns

    async def call_model(
        self,
        prompt: AssembledPrompt,
        trigger: Trigger | None,
        *,
        session_id: str | None = None,
    ) -> ProviderReply:
        """Execute a model call — refused without a trigger, recorded when it runs.

        With a router wired (stage 9), the provider is tier-selected per
        trigger and the session budget is enforced BEFORE the call: refuse
        writes a denied model_call record then raises BudgetExceededError;
        warn writes a budget_warning record and lets the call proceed.
        """
        if trigger is None:
            raise TriggerRefusedError(
                "model call refused: no trigger — an unrecordable call must not execute"
            )
        provider = (
            self._router.provider_for(trigger) if self._router is not None else self._provider
        )
        action = Action(
            name=provider.name,
            input_digest=prompt_digest(prompt),
            input_summary=f"assembled prompt: {len(prompt.messages)} message(s), "
            f"system context present",
        )
        if self._router is not None and session_id is not None:
            verdict = self._router.budget_verdict(session_id)
            if verdict.action == "refuse":
                # The refused call is still a model_call event — recorded with
                # outcome denied (contract audit-record), then fails the
                # message gracefully (the loop stays alive; run() surfaces
                # the error to the caller).
                self._audit_sink.append(
                    AuditRecord(
                        agent=self._identity,
                        event="model_call",
                        trigger=trigger,
                        action=Action(
                            name=provider.name,
                            input_digest=action.input_digest,
                            input_summary=f"{action.input_summary}; {verdict.note}",
                        ),
                        outcome=Outcome(status="denied"),
                        cost=Cost(tokens_in=0, tokens_out=0),
                    )
                )
                raise BudgetExceededError(verdict.note)
            if verdict.action == "warn":
                logger.warning("model budget exceeded (onExceed=warn): %s", verdict.note)
                self._audit_sink.append(
                    AuditRecord(
                        agent=self._identity,
                        event="budget_warning",
                        trigger=trigger,
                        action=Action(
                            name=provider.name,
                            input_digest=_sha256(verdict.note),
                            input_summary=verdict.note,
                        ),
                        outcome=Outcome(status="ok"),
                    )
                )
        try:
            reply = await provider.complete(prompt)
        except Exception:
            # A failed provider call is still a model call — "every call
            # recorded" (contract audit-record). Digests of what was sent,
            # no payload; then the failure propagates unchanged.
            self._audit_sink.append(
                AuditRecord(
                    agent=self._identity,
                    event="model_call",
                    trigger=trigger,
                    action=action,
                    outcome=Outcome(status="error"),
                    cost=Cost(tokens_in=0, tokens_out=0),
                )
            )
            raise
        record = AuditRecord(
            agent=self._identity,
            event="model_call",
            trigger=trigger,
            action=action,
            outcome=Outcome(status="ok", output_digest=_reply_digest(reply)),
            cost=Cost(tokens_in=reply.tokens_in, tokens_out=reply.tokens_out),
        )
        self._audit_sink.append(record)
        if self._router is not None and session_id is not None:
            # Budgets charge the AUDITED cost — the same numbers the record
            # carries. The selected provider rides along so the USD budget
            # (stage 25) charges this call at exactly its tier's declared rate.
            self._router.record_cost(
                session_id,
                tokens_in=reply.tokens_in,
                tokens_out=reply.tokens_out,
                provider=provider,
            )
        return reply

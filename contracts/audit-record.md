# Contract: audit-record

- **Status:** frozen v1 (re-frozen under Agent Keep, 2026-07-22)

> **Carried from Agent Foundry** (`~/projects/Agent-Factorio/contracts/audit-record.md`,
> frozen there at v1) per the transplant manifest — proven shape, carried not
> rewritten. The body below is verbatim from the source. Read it under this
> identity mapping: `foundry_spec` → `keep_spec`, `foundry/v1` → `keep/v1`,
> `agent-foundry` → `agent-keep`, `/etc/agent-foundry/` → `/etc/agent-keep/`.
>
> **Successor deltas (normative):** none (includes the 2026-07-14 run-correlation amendment). Egress-observation records (see `egress-observation` contract) are additive record kinds within this shape.

---


- **Status:** frozen v1
- **Owner:** `agent_runtime.audit` (Audit Log component)

The append-only record of everything an agent does. The vision's claim — "each
tool call carries its purpose into the audit log" — is enforced here: a tool call
without a triggering context reference cannot be recorded, and an unrecordable
tool call must not execute. Uniform across every agent the Foundry builds
(factory-level, ADR 0003 item 7), so fleet-wide review tooling works on one shape.

## Exposes

- `AuditRecord` model in `agent_runtime`; an append-only sink interface
  (`append(record) -> None` — no update, no delete in the interface at all).

## Consumes

- Tool executor (every call, before-and-after), approval gate (decisions), memory
  system (privileged memory writes), model router (per-call cost accounting).

**Clarification (2026-07-11):** the "before-and-after" phrasing above is
descriptive prose, not a normative field spec, and this note supersedes it for
current code (the wire shape below is unchanged). What ships today: an
**approval-gated** tool call emits a pre-action record — an `approval` event with
`outcome.status: pending_approval` — when it is parked (`executor.py`), followed
by a terminal record (`ok` / `denied` / `error`) once it is decided and run. An
**auto-approved** tool call (`executor.py`) and a **model call** (`core.py`) emit
a **terminal record only**, appended after the underlying `tool.run()` /
`provider.complete()` returns. So a *pre-action* record exists only on the
approval-gated path today, not for every call. A durable pre-action **intent**
event emitted for every call regardless of approval — so the log records what an
agent was *about to* do, not only what it *did* — is named future work: the
externally-verifiable audit pipeline (ENH-04 direction), tied to the `log-egress`
mechanic arc. It arrives additively (a new event type / optional fields) under the
versioning rule below; no consumer of the current shape is affected.

**Amendment (2026-07-14):** two additive declarations (ADR 0014); the wire
shape below gains exactly one optional line, nothing else changes. First,
`trigger.message_id`/`trigger_id` is THE run-correlation key:
`run-lifecycle@1`'s `run_id` equals the non-null one of them — one run = one
activation — so the heartbeat↔audit join is a key equality, never a timestamp
heuristic, and NO second run key exists in the system. Second, the record
gains an OPTIONAL, nullable top-level `trace_id`, reserved for a future
analytics plane (ADR 0014's two-plane design: live/unsampled joined to
traces/sampled by shared keys). No tracer exists today, so `trace_id` is
null/absent on every record and no producer changes.

## Schema / wire

```yaml
id: <uuid>
ts: <RFC 3339, UTC>
agent:
  slug: <agent slug>
  spec_version: <metadata.specVersion of the spec the running image was built from>
  image_digest: <sha256 of the running image>
event: <enum: tool_call | approval | memory_write | model_call ...additive>
trigger:                       # the WHY — required, never null
  message_id: <internal-message id that led here>      # exactly ONE of message_id
  trigger_id: <schedule/event activation id>           # or trigger_id is non-null
  purpose: <short human-readable statement assembled by the core>
action:
  name: <tool/model/operation name as declared in the spec>
  input_digest: <sha256 of canonicalized inputs>
  input_summary: <redacted human-readable summary>
outcome:
  status: <enum: ok | error | denied | pending_approval>
  output_digest: <sha256, when applicable>
approval:                      # block always present on tool_call events
  required: <bool>
  decided_by: <internal_user_id or "policy:auto"; "policy:auto" when required=false>
cost:                          # present on model_call events
  tokens_in: <int>
  tokens_out: <int>
trace_id: <optional, nullable — reserved analytics-plane key (ADR 0014); null today>
```

Rules: records are append-only at the interface level; digests-not-payloads keeps
secrets and bulk content out of the log while preserving tamper-evidence;
`trigger` is mandatory — the executor refuses tool calls that arrive without one.

## Versioning

Frozen at **v1**. Changes are **additive only** (new event types, new optional
fields) — a breaking change is a NEW contract, not an edit (framework-spec §4.3).
Every consumer depends on this shape.

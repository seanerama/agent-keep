# Contract: run-lifecycle

- **Status:** frozen v1 (re-frozen under Agent Keep, 2026-07-22)

> **Carried from Agent Foundry** (`~/projects/Agent-Factorio/contracts/run-lifecycle.md`,
> frozen there at v1) per the transplant manifest — proven shape, carried not
> rewritten. The body below is verbatim from the source. Read it under this
> identity mapping: `foundry_spec` → `keep_spec`, `foundry/v1` → `keep/v1`,
> `agent-foundry` → `agent-keep`, `/etc/agent-foundry/` → `/etc/agent-keep/`.
>
> **Successor deltas (normative):** none. Still shape-only until an emitter stage lands.

---


- **Status:** frozen v1
- **Owner:** `agent_runtime` (core loop; a future heartbeat emitter component
  will be the producer — no emitter exists in v1, this contract is shape-only)

The live-plane vocabulary: what a run *is*, which states it can be in, and the
minimal heartbeat record an agent emits while a run is in flight. Standard
telemetry (OTel traces) reports work only after it finishes — a span exports
when it ends — so live state ("grinding for twenty minutes", "blocked on a
human approval") needs its own explicit, unsampled vocabulary emitted DURING
execution. This contract defines that vocabulary (ADR 0014). It deliberately
does NOT define where heartbeats go, how often they fire, or any consumer —
emission is a per-agent spec choice (the deferred `observability` spec
section); the shape is fleet-uniform so future fleet tooling reads one record.

## Exposes

- `RunState` enum + `RunHeartbeat` model in `agent_runtime` — the wire shape
  below, drift-guarded by tests like every other contract model.

## Consumes

- `internal-message` (the run key — see Rules), `audit-record` (the shared
  `agent` identity block and the `trigger` join), `agent-spec` (the future
  `observability` section governs whether/where an agent emits — deferred).

## Schema / wire

Run states — a CLOSED enum, derived from transitions the runtime can actually
produce today (not a generic textbook set):

```
queued → running → awaiting_approval → running → completed
running → failed
```

- `queued` — the activation sits in the queue (`core.py` consumer loop).
- `running` — the core is handling the activation.
- `awaiting_approval` — a tool call is parked pending a human decision
  (`executor.py` approval gate). THE operationally-critical state: a human is
  the bottleneck. Note: an approval **denial does not terminate the run** — the
  tool outcome is `denied`, the error result returns to the model, and the run
  resumes (`running`). There is deliberately NO `rejected` run state.
- `completed` — the reply future resolved.
- `failed` — message handling raised (`core.py` sets the exception).

There is NO `cancelled` in v1 — nothing can cancel a run today. Future states
(e.g. `cancelled`, when a canceller exists) arrive ADDITIVELY under the
versioning rule; consumers MUST tolerate unknown states by treating them as
non-terminal.

```yaml
# run-lifecycle@1 heartbeat
run_id: <THE activation id — see Rules; NOT a freshly minted uuid>
agent:                        # identical block to audit-record `agent`
  slug: <agent slug>
  spec_version: <metadata.specVersion the running image was built from>
  image_digest: <sha256 of the running image>
status: <RunState enum above>
current_step: <token from the runtime-owned step vocabulary — see Rules>
started_at: <RFC 3339, UTC — when this run left `queued`>
last_seen: <RFC 3339, UTC — emission time; freshness signal>
worker_id: <container/process instance identity>
trace_id: <optional, nullable — reserved analytics-plane correlation key;
           no tracer exists in v1, producers MAY emit null>
```

Rules (invariants a consumer may rely on):

- **`run_id` IS the activation id** — the non-null one of the triggering
  `internal-message.id` / trigger activation id, i.e. exactly the value every
  audit record already carries as `trigger.message_id`/`trigger_id`. One run =
  one activation. This makes the heartbeat↔audit join a key equality, never a
  timestamp heuristic, and it means NO second run key exists in the system.
- **CLOSED VOCABULARIES ONLY (anti-exfiltration).** `status` and
  `current_step` come from closed, runtime-owned sets: `current_step` values
  are stamped by runtime code (component/operation names, e.g.
  `executor.local_tool.<op>`, `provider.complete`), NEVER text the model
  composed or influenced. A heartbeat fires mid-loop while the model is
  processing untrusted input; free-form step text would be a covert channel an
  injected agent could exfiltrate through (same discipline as `log-egress`
  demarcation, applied to the live plane).
- **STATE, NOT DATA.** A heartbeat carries no message content, no tool
  inputs/outputs, no prompt text, and (as everywhere) no secret VALUES.
- **`last_seen` freshness, not ordering.** Staleness past a consumer-chosen
  threshold means "presumed dead — stopped reporting at `last_seen`". The wall
  clock may step backward (the `interview-transcript` `answered_at` lesson):
  consumers use max-seen-per-run, and MUST NOT assume monotonicity.
- **Emission is spec-governed and OPT-IN, default none** (ADR 0014). An agent
  whose spec declares no observability emits nothing and ships no
  telemetry/emitter code (absence-as-security); this contract only fixes the
  shape such an agent would emit, so the fleet is uniform from the first
  opted-in deployment onward.

## Versioning

Frozen at **v1**. Changes are **additive only** — a breaking change is a NEW
contract, not an edit (framework-spec §4.3). Every consumer depends on this shape.

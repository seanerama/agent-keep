# Contract: internal-message

- **Status:** frozen v1 (re-frozen under Agent Keep, 2026-07-22)

> **Carried from Agent Foundry** (`~/projects/Agent-Factorio/contracts/internal-message.md`,
> frozen there at v1) per the transplant manifest — proven shape, carried not
> rewritten. The body below is verbatim from the source. Read it under this
> identity mapping: `foundry_spec` → `keep_spec`, `foundry/v1` → `keep/v1`,
> `agent-foundry` → `agent-keep`, `/etc/agent-foundry/` → `/etc/agent-keep/`.
>
> **Successor deltas (normative):** none.

---


- **Status:** frozen v1
- **Owner:** `agent_runtime.normalizer` (Message Normalizer component)

The canonical message format — the contract for the whole runtime. Every channel
adapter translates INTO this shape at the boundary; everything downstream (gateway,
queue, sessions, prompt assembler, audit) speaks only this. The blueprint flags
this as the schema that "touches every adapter" if changed later — hence frozen
first. This is a factory-level decision (ADR 0003, item 1): uniform across every
agent the Foundry builds.

## Exposes

- `InternalMessage` Pydantic model in `agent_runtime` — validation at the boundary
  is a security control (untrusted platform payloads never flow raw past the
  adapter).

## Consumes

- Raw platform payloads (Discord, Slack, WebEx, SMS, …) — adapter-private; never
  visible downstream.
- Trigger events (schedule/event-subscription activations) are wrapped in the same
  envelope with `sender.kind: system`, so the core has ONE inbound shape.

## Schema / wire

```yaml
id: <uuid, assigned by normalizer>
ts: <RFC 3339, UTC>
channel:
  platform: <enum: dev-http | discord | slack | webex | sms | system | ...additive>
                                        # dev-http = localhost-only development/test adapter (walking skeleton)
  conversation_id: <platform-scoped opaque string>
sender:
  kind: <enum: human | system>       # system = trigger-originated
  platform_id: <opaque platform identity; null for system>
  internal_user_id: <resolved internal identity, or null if unmapped>
  verified: <bool — signature/token verification passed at the adapter>
content:                              # ordered content blocks
  - type: <enum: text | image | file | event ...additive>
    ...block fields per type
provenance:
  adapter: <component id + version that produced this>
  trust: <enum: untrusted | operator | ...additive>
    # ALL inbound human content is `untrusted` — allowlisted/verified senders
    # included (verification authenticates the sender, not the content).
    # `operator` is reserved for content originating from the agent's own
    # spec/config, never from a channel.
```

Rules: adapters MUST set `verified` honestly (false when the platform offers no
verification); `content` is always a block list, never a bare string; downstream
components MUST ignore unknown block types rather than error (forward
compatibility of additive types).

## Versioning

Frozen at **v1**. Changes are **additive only** (new block types, new optional
fields, new platform enum values) — a breaking change is a NEW contract, not an
edit (framework-spec §4.3). Every consumer depends on this shape.

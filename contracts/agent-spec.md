# Contract: agent-spec

- **Status:** frozen v1 (re-frozen under Agent Keep, 2026-07-22)

> **Carried from Agent Foundry** (`~/projects/Agent-Factorio/contracts/agent-spec.md`,
> frozen there at v1) per the transplant manifest — proven shape, carried not
> rewritten. The body below is verbatim from the source. Read it under this
> identity mapping: `foundry_spec` → `keep_spec`, `foundry/v1` → `keep/v1`,
> `agent-foundry` → `agent-keep`, `/etc/agent-foundry/` → `/etc/agent-keep/`.
>
> **Successor deltas (normative):** the spec is authored from a config/brief file by a human — there is no interview and never will be (project identity). The schema narrows to the chassis config; factory-breadth fields are pruned before first freeze of `keep/v1`.

---


- **Status:** frozen v1
- **Owner:** `foundry_spec` package

The central artifact of the system — the declarative manifest ("a Dockerfile for an
agent") produced by the interview and consumed by everything else. This contract
freezes the **envelope and its rules**; the fields inside each section are defined
by the Phase 1 schema work and grow **additively** under these rules.

## Exposes

- A YAML document validated by `foundry_spec` (Pydantic models; JSON Schema
  exported to `docs/spec-schema.json` on every release — that file first exists
  once Stage 0 builds `foundry_spec`; it is not in this PR).
- The complete, reviewable answer to every **agent-level** decision (ADR 0003):
  what exists in this agent's image, how it is scoped, and what requires approval.

## Consumes

Nothing at runtime. Tooling consumers: composer/builder (`foundry`), runbook
generator, spec differ, the Phase 3 interview, and human security reviewers.

## Schema / wire

```yaml
apiVersion: foundry/v1          # literal; version bump = NEW contract
kind: AgentSpec                 # literal
metadata:
  name: <human name>
  slug: <kebab-case; image becomes ghcr.io/<owner>/agent-foundry-<slug>>
  description: <one line>
  specVersion: <semver of THIS document, bumped on any edit>
spec:
  persona: {...}                # identity, tone, standing instructions; source =
                                #   static config / learned memory / both + precedence
  triggers: {...}               # what activates the agent: messages / schedule / events
  channels: [...]               # platform adapters + transport + verification
  gateway: {...}                # allowlist policy, identity unification, queue, concurrency
  sessions: {...}               # session definition, history strategy
  memory: {...}                 # structure, stores, write policy
  skills: [...]                 # instruction packs (knowledge, never code) +
                                #   selection strategy (always-loaded / on-demand)
  tools: [...]                  # MCP servers/tools allowlist, per-tool scopes
  approval: {...}               # what requires human confirmation
  sandbox: {...}                # execution isolation profile + EGRESS allowlist
  models: {...}                 # routing tiers, providers ('static' is first-class), budgets
  observability: {...}          # logging config, audit sink, health
  persistence: {...}            # storage tier for sessions/transcripts/vectors
```

Binding rules (all frozen):

1. **Strict validation** — unknown fields are an error, never ignored. A spec that
   validates says everything it means.
2. **Absence semantics** — a component/tool not selected in the spec is ABSENT from
   the built image, not disabled. The spec is an exhaustive positive declaration.
3. **No secrets** — the spec names required secrets (env var names in the export's
   env template); values never appear in spec, image, or runbook.
4. **Sections are additive** — new optional fields and new enum values may be
   added; no field is ever removed, renamed, or re-typed under `foundry/v1`.
5. **Reproducibility** — spec + component-library version pin fully determine the
   image contents.

## Versioning

Frozen at **v1**. Changes are **additive only** — a breaking change is a NEW
contract (`apiVersion: foundry/v2`), not an edit (framework-spec §4.3). Every
consumer depends on this shape.

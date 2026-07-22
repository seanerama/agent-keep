# 0001. Inherit the Foundry stack: Python 3.12 uv modular monolith, transplanted runtime

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

Agent Keep is the chassis-first successor to Agent Foundry (predecessor ADR 0018).
The Foundry's agent-architecture parts — runtime, audit, budget, contracts,
per-agent deploy — never failed; only its creation-experience breadth did. The
transplant manifest in `chassis-successor-brief.md` names the packages to carry.
The stack decision here is therefore "inherit or diverge", not greenfield choice.

## Decision

- **Language/tooling:** Python 3.12, `uv` workspace with committed lockfile,
  hatchling per-package builds, pydantic v2, httpx (hand-rolled Anthropic adapter,
  no vendor SDK), pytest + pytest-asyncio, ruff, mypy `strict`. Identical to the
  Foundry; carried, not rewritten.
- **Topology: modular monolith.** One repo, one runtime image per stamped agent.
  Workspace packages:
  - `agent_runtime` — transplanted from the Foundry (name is generic; keep it).
  - `keep_spec` — the Foundry's `foundry_spec` narrowed to the chassis config and
    renamed under the new identity. Spec version identifier becomes `keep/v1`.
    Factory-breadth options are dropped as they prove unneeded, not up front.
  - Left behind entirely: `foundry_interview`, the composer fleet/template breadth.
- The mechanic is a second container from the same codebase, not a second service
  in the CI/deploy sense (see ADR 0005); images extend the slug per the locked
  identity: `ghcr.io/seanerama/agent-keep[-<service>]`.
- The hermetic `static` model provider remains first-class and is the CI substrate
  (predecessor ADR 0004): CI proves the full compose→boot→message→respond path
  with zero secrets or network.

## Alternatives considered

- The stack-and-topology guide recommends boring, well-supported stacks and a
  modular monolith — the inherited stack **is** the guide's recommendation, so no
  deviation ADR is needed.
- **Greenfield rewrite (any language/framework):** rejected; the transplant
  sources are proven by live inhabitants and the brief's rule is "carry, don't
  rewrite".
- **Multi-service split (proxy/mechanic/agent as separately-built services):**
  rejected for now; every service multiplies the CI matrix and deploy surface.
  The egress proxy and mechanic ride the same workspace and image family.

## Consequences

- Fast path to a working skeleton; the risk shifts from "does the code work" to
  "was the transplant faithful" — the Reviewer verifies transplanted code against
  the read-only source at `~/projects/Agent-Factorio`.
- `keep_spec` narrowing is gradual; until pruned, some factory-era spec surface
  rides along inert. Pruning is additive-safe (removals happen before first
  freeze of `keep/v1`).
- Renaming the spec package means transplanted imports must be updated
  mechanically; contracts are re-frozen under the new identity (see contracts/).

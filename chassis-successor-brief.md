# Handoff brief: the chassis-first successor project

> Consumed by the successor's `/verity:vision` session. Copy THIS FILE ONLY
> into the new, empty project directory — do not clone the Foundry repo
> (clean-tree rule, ADR 0018). The Foundry repo remains at
> `~/projects/Agent-Factorio` as the read-only transplant source.

## The one-liner (operator's words, 2026-07-16)

A repeatable foundation for one agent at a time — the container that houses
it, the mechanic that operates that container, and every byte and token in
or out, observed.

## What this project is (and is not)

- **Is:** an agent CHASSIS, proven by real inhabitants. Depth over breadth:
  one or two living agents at a time, and the framework built around them —
  container, paired mechanic, monitored inbound/outbound communication,
  token/cost accounting, append-only audit, health.
- **Is not:** a factory, a spec generator, or a creation experience. The
  human-facing input is a config/brief file. No interview surfaces, ever.
- **Lineage:** Blueprint (the map) → Agent Foundry (proved the components
  and governance; now in maintenance, ADR 0018) → this project (the
  reference implementation).

## Decisions already made (2026-07-16, operator-confirmed)

1. **Fresh repo, new identity.** Name is NOT chosen — first order of
   business for the vision session. Candidates floated, none binding:
   Chassis, Keel, Vivarium, Agent Keep.
2. **Mechanic = ops plane only.** It operates the container (restart,
   pause, budget throttle, health remediation) and explains from evidence;
   it NEVER edits what the agent is — spec changes stay human-approved
   diffs. Two planes: the mechanic runs the body; the mind is
   human-governed. (Widening this later is its own ADR with guardrails.)
3. **Walking skeleton = thin agent, thick chassis.** A simple default
   chatbot inside the FULL envelope from day one: monitored ingress AND
   egress (outbound through an observed choke point — new architecture,
   the most valuable piece), token accounting, audit, paired mechanic.

## Principles carried forward (non-negotiable inheritance)

- **Absence is the strongest security control** — capabilities not in the
  config do not exist in the container.
- **The manifest is the product artifact** — one declarative file a
  security reviewer reads in a sitting; runbook generated from it.
- **Provenance/citability** — the mechanic answers "why" from recorded
  evidence, never from guesses.
- **Test honesty** — real tests, hermetic CI (static model provider
  pattern), green CI as the floor; live verification by the operator is
  the gate that closes issues.
- **No secrets in git/images/logs** — env at deploy; token-wise secret
  screens on anything persisted.

## Transplant manifest (copy code OUT of `~/projects/Agent-Factorio` when
stages need it — carry, don't rewrite; re-freeze contracts under the new
identity)

- `packages/agent_runtime/` — the component library: gateway, sessions,
  queues, append-only audit, budget/token machinery (`BudgetVerdict`),
  hand-rolled Anthropic adapter (httpx, no SDK) + hermetic `static`
  provider (the CI substrate).
- `packages/foundry_spec/` — the strict manifest schema; NARROW it to the
  chassis's config (drop factory-breadth options as they prove unneeded).
- `contracts/`: `audit-record.md`, `log-egress.md`, `run-lifecycle.md`,
  `internal-message.md`, `agent-spec.md` — proven shapes; re-freeze in the
  successor's tree.
- Deploy machinery: `deploy.sh`, systemd unit + per-agent env-file pattern
  (`/etc/<project>/<slug>.env`, root 0600), host-agnostic via env (ADR 0013
  posture).
- `docs/blueprint-data.json` — the decision-space map, as reference.
- `templates/agent-brief.md` — the plain-language front door, refit as the
  chassis config's human-facing companion.
- Left behind: `packages/foundry_interview/` entirely; composer
  breadth/fleet/templates; `interview-tools@1` (frozen, unimplemented —
  revivable pattern if config authoring ever wants an agent seam).

## Open questions FOR the vision session

- The name / identity lock (slug, image prefix, owner `seanerama`).
- Which channel the default chatbot speaks first (dev-http was the
  Foundry's hermetic default; WebEx/Slack adapters exist to transplant).
- Deploy target for the first live chassis (operator-provided host per the
  standing catalog; Coolify was earmarked for future web surfaces).
- What "monitored egress" means concretely in v1 (observed proxy choke
  point vs host firewall + audit — the architect session's first big call).
- Public-repo timing (the standing plan: private first, public later from
  a clean tree — this repo IS the clean tree).

## Predecessor pointers (read-only)

- ADR 0018 (this pivot, full rationale) and ADRs 0001-0017 for why each
  inherited shape is the way it is — especially 0004 (hermetic provider),
  0009-0011 (mechanic discipline), 0013 (deploy posture), 0014
  (observability is spec-declared).
- The v0.9.x live-testing friction arc (issues #162/#163/#168, PRs
  #166/#172) — the evidence that chose depth over breadth.

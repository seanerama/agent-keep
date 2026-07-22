# Assessment: initial decomposition (Mode A) — the walking skeleton

- **Date:** 2026-07-22
- **Input:** `docs/walking-skeleton.md` (Architect handoff), ADRs 0001-0005,
  `chassis-successor-brief.md`
- **Decision:** ACCEPT, SPLIT into Stages 1-5 (dependency-ordered)

## Claim / reality verification (against `~/projects/Agent-Factorio`, 2026-07-22)

| Claim (from brief / skeleton doc) | Reality in source | Verdict |
| --- | --- | --- |
| `agent_runtime` components to transplant exist (dev_http, static/anthropic providers, jsonl_audit, memory_queue, single_session, worker_analyzer, …) | All present in `packages/agent_runtime/src/agent_runtime/components/` | ✅ holds |
| Budget/token machinery incl. `BudgetVerdict` exists | `agent_runtime/provider.py:98 class BudgetVerdict` | ✅ holds |
| Mechanic crashes on transcript-less bundle (predecessor ADR 0011) | `worker_analyzer.py:104` unconditionally reads `<slug>.interview.json` | ✅ holds — Stage 4 must fix |
| Deploy machinery transplantable | `deploy.sh` + `deploy/` (helper, sudoers, systemd, env templates) present | ✅ holds |
| Egress proxy is NEW architecture | No proxy/HTTP(S)_PROXY handling anywhere in runtime source | ✅ holds — Stage 3 is a build, not a transplant |
| CI shape to mirror | Jobs: structure, secret-scan, lint, typecheck, test, container, publish-image | ✅ holds |

No false premises found; planning proceeds on verified ground.

## Why this split

- **Stage 1 (transplant, chore)** isolates "was the carry faithful" risk from
  all build risk; hermetic CI green is its whole exit-state.
- **Stage 2 (chatbot container, feature)** is the thinnest runnable envelope
  and rebuilds the predecessor's honest container CI job.
- **Stage 3 (egress proxy, feature)** is the only greenfield build — kept
  alone so review can be strictly against `egress-observation` v1.
- **Stage 4 (mechanic, feature)** depends only on Stage 2 (parallel-safe with
  3); its heart is the contract-recorded transcript-less delta.
- **Stage 5 (deploy, chore)** closes the skeleton with live verification —
  the ALLOW path and the operator smoke that CI structurally cannot prove.
- Stages 3 and 4 are independent; 5 gates on both. Together 1-5 satisfy every
  acceptance line in `docs/walking-skeleton.md`.

## Contract safety

- No frozen contract is threatened. Stage 3 builds TO the new
  `egress-observation` v1 (frozen by the Architect); its `egress` audit record
  kind is additive within `audit-record` v1. Stage 4 implements the
  transcript-less delta already recorded normatively in the re-frozen
  `log-egress`. No new seams needed at intake time.

## Deferred (future intake, deliberately not stages yet — thin backlog)

- Real platform channel (WebEx or Slack) — one stage, post-skeleton.
- Host-firewall defense-in-depth from the allowlist (ADR 0002 deferral).
- Mechanic ops actuators (restart/pause/throttle + scoped host seam, ADR 0005).
- `keep_spec` narrowing before `keep/v1` schema-export freeze.
- Redis/Postgres tiers, event intake, schedule trigger, MCP, embeddings.

## Rejected

- `helper-bot` catalog feature — conflicts with the no-UI identity
  (operator-confirmed 2026-07-22; see Architect session).

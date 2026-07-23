# Assessment: deployment abstraction (Mode A) — the "two inputs → deployed" backlog

- **Date:** 2026-07-22
- **Input:** ADR 0007 (conformant host; two inputs deploy any agent), ADR 0008
  (two-plane: chassis on host, model as provider), `docs/deployment-abstraction-skeleton.md`, issue #21
- **Decision:** ACCEPT, SPLIT into Stages 13–15 (thin, dependency-ordered)

## Claim / reality verification (against live code, 2026-07-22)

| Claim (from the design) | Reality in source | Verdict |
| --- | --- | --- |
| `deploy.sh` is target-agnostic (DEPLOY_HOST, KEEP_SPEC_FILE, slug+version) | `deploy.sh` usage + `WORKER_IMG=ghcr.io/<owner>/agent-keep-<slug>`, DEPLOY_HOST/KEEP_SPEC_FILE/KEEP_DEPLOY_SECRETS all present | ✅ holds — the engine is already portable |
| Host bootstrap is currently manual | Helper/sudoers install steps appear only in `docs/deploy/first-live-chassis.md`; no `scripts/bootstrap*` | ✅ holds — Stage 13 automates it |
| Worker image is pulled by tag (needs a reachable registry image) | `deploy.sh` step 4 "Pull each image BY TAG… pin"; `WORKER_IMG:VERSION` | ✅ holds — Stage 14 adds a no-registry-write build+load path |
| No `(blueprint, target)` orchestrator exists | `deploy.sh` is the only entry; no wrapper | ✅ holds — Stage 15 adds it |
| The model plane is already the provider abstraction | anthropic/ollama/openai adapters merged + live-proven | ✅ holds — ADR 0008 needs no new deploy machinery for the model |

No false premises. The design's core claim — "most of it already exists" — is
confirmed; the backlog closes exactly the three named gaps.

## Why this split

- **Stage 13 (bootstrap)** — no deps; the biggest single simplicity win (turns the
  manual runbook preflight into one command). Foundational.
- **Stage 14 (arbitrary-blueprint image path)** — no hard deps (parallel-safe with
  13); the "any blueprint" half of the north star, and it removes the
  registry-write requirement that bit the operator during provider live-testing.
- **Stage 15 (single entry point)** — depends on 13 + 14; the thin orchestrator
  that realizes `(blueprint, target) → deployed` and IS the walking-skeleton proof.
- 13 and 14 are independent; 15 gates on both. Together they deliver the north
  star on the baseline (local + bring-your-own-host, all clouds via a VM).

## Contract safety

No frozen contract is threatened. The two inputs reuse frozen `agent-spec` (the
blueprint) and the `.verity/deploy-access.md` pattern (the target). The worker
image ref (`agent-keep-<slug>@<digest>`) is unchanged — Stage 14 only changes its
SOURCE (a load vs a pull). No new contract issued; Stage 15 flags the Planner if a
formal deploy-target descriptor emerges.

## Deferred (per ADR 0007/0008 + the skeleton — deliberately not stages yet)

- Per-cloud provisioners (`gcloud`/`az`/`aws` scripts or a Terraform module) —
  optional conveniences, one cloud at a time, on demand.
- In-tenant serverless GPU model endpoints (ADR 0008) — a provider/model-plane
  concern (issue #15 pattern), added when a data-sovereignty client needs it.
- Kubernetes / cloud-native chassis targets — only if a client mandates it (own ADR).

## Rejected

- Baking provisioning/access (Terraform-everywhere, Tailscale-everywhere) into the
  abstraction — rejected in ADR 0007 (a client's tenant/network is theirs;
  provisioning + access are pluggable, not required).

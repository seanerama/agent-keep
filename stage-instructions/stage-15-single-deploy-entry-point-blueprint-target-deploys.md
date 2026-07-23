# Stage 15: Single deploy entry point: (blueprint, target) deploys

- **Type:** feature
- **Depends on:** 13,14

## Objectives

Realize the north star (ADR 0007): **two inputs — a blueprint and a target —
deploy any agent**, with everything else automated. This is the walking skeleton
of the deployment-abstraction phase: one command orchestrating bootstrap →
image → the audited paired topology → verify, on the baseline (local host or any
bring-your-own VM).

## What to build

- A single entry point `scripts/deploy-agent.sh <blueprint-spec> <target>` (or a
  `keep deploy <spec> <target>` verb) that orchestrates the proven pieces:
  1. **Ensure conformant**: if the target isn't deploy-ready, run the stage-13
     bootstrap (or detect + instruct). Idempotent — a ready host is a no-op.
  2. **Resolve the image**: if the blueprint maps to a CI-published tag, use it
     (registry pull); otherwise build + load it via the stage-14 path (no
     registry write). Blueprint identity → image ref.
  3. **Deploy**: invoke `deploy.sh` with the resolved slug/version + `KEEP_SPEC_FILE`
     = the blueprint, threading any provider secret through `KEEP_DEPLOY_SECRETS`
     on stdin (never argv/log — stage 7 discipline).
  4. **Verify**: the deploy's own liveness gate (proxy + worker + mechanic) plus a
     one-line smoke; report a clear `DEPLOYED` with the endpoints.
- Secrets stay stdin-only and provider-agnostic (the operator pipes whatever the
  blueprint's provider needs, or nothing for a keyless/local model).
- Keep `deploy.sh` as the low-level engine; this is thin orchestration over it +
  stages 13/14, not a rewrite.

## Interface contracts

- **Consumes:** stage-13 bootstrap, stage-14 image path, `deploy.sh`, frozen
  `agent-spec` (blueprint) + `.verity/deploy-access.md` (target). No new contract
  (ADR 0007) unless a deploy-target descriptor proves necessary — flag to the
  Planner if so.

## Testing requirements

- Integration (stub-ssh, mirror the deploy tests): `(blueprint, target)` drives
  bootstrap-if-needed → image-resolve → deploy.sh → verify in order; a ready host
  skips bootstrap; a CI-variant blueprint pulls, an arbitrary blueprint builds+loads.
- Container (`-m container`): end-to-end against a local docker "host" — one
  `deploy-agent.sh <spec> <local-target>` stands up the audited paired topology
  and a smoke passes. This IS the walking-skeleton proof.
- shellcheck clean; existing suites green.

## Acceptance conditions

- [ ] Kill-switch: N/A — operator entry point (recorded)
- [ ] Observably-works: a single `(blueprint, target)` invocation deploys the
      audited chassis and passes a smoke, on a fresh host, with no manual steps
      between the two inputs
- [ ] Secrets stdin-only, provider-agnostic; no value in git/argv/log
- [ ] Additive only; `contracts/` untouched
- [ ] Existing suite stays green; CI all-green

## Pipeline test: YES — the container job runs the full (blueprint, target) path

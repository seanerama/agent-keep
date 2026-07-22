# Stage 4: Paired mechanic: transcript-less bundle, read-and-explain only

- **Type:** feature
- **Depends on:** 2

## Objectives

The other half of the chassis identity: the paired mechanic that reads the
worker's read-only artifact bundle and explains behavior from cited evidence.
Ops-plane only (ADR 0005); in this stage it is read-and-explain ONLY — no
actuators (restart/pause/throttle are a later stage with their own scoped host
seam).

## What to build

- Fix `worker_analyzer` for the transcript-less bundle: in Agent Keep the
  bundle is `<slug>.yaml` + `<slug>.audit.jsonl` ONLY — there is no
  `<slug>.interview.json` (interview left behind). The transplanted analyzer
  hard-reads it (Foundry `worker_analyzer.py:104`, verified 2026-07-22) and
  crashes; make the transcript optional-and-absent per the re-frozen
  `log-egress` contract delta, removing interview-specific explain paths.
- Mechanic spec (`specs/mechanic.yaml`, `keep/v1`) per the predecessor's
  role-template pattern: dev-http owner-only channel, the single read-only
  worker-analyzer local-tool grant, no memory writes, jsonl audit, static
  provider in CI / anthropic live, egress allowlist = model provider only.
- Mechanic image `ghcr.io/seanerama/agent-keep-mechanic` from the SAME runtime
  (ADR 0001 — a second stamped spec, not a second service).
- Pairing topology: worker writes its audit into the bundle dir; mechanic
  mounts the SAME dir read-only via `MECHANIC_WORKER_DIR`, own audit on a
  separate path (the predecessor's ADR 0011 collision fix, now actually built).
- Read-only enforced in the three layers the contract names: diff-only outputs,
  read-only mount, read-mode opens.

## Interface contracts

- **Exposes:** the citable "why did the agent do X" surface; proposed spec
  diffs (human-approved, never applied by the mechanic).
- **Consumes:** `log-egress.md` (re-frozen v1 + transcript-less delta — THE
  spec for this stage), `agent-spec.md`, `audit-record.md` (it must cite
  egress records from Stage 3 like any other evidence).

## Testing requirements

- Unit: analyzer over a fixture transcript-less bundle — explain + cite works;
  a bundle WITH a stray transcript file is ignored, not crashed on.
- Container/integration: worker + mechanic paired compose; ask the mechanic
  "what did the worker just do?" after a scripted worker message; assert the
  answer cites real audit line refs; assert a mechanic write attempt into the
  bundle dir fails (read-only mount).

## Acceptance conditions

- [ ] Kill-switch: the mechanic is opt-in by deployment — a worker without a
      paired mechanic is a valid topology (default OFF satisfied by absence)
- [ ] Observably-works asset authored: `scripts/smoke-mechanic.sh` — one
      question to the live mechanic, assert a cited answer (Operator runs it
      in Stage 5's live smoke)
- [ ] Additive migration only
- [ ] Existing suite stays green; CI all-green

## Pipeline test: YES — paired-compose test joins the container CI job

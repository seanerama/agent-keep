# Stage 6: Fix egress proxy startup: empty KEEP_EGRESS_PORT crash + deploy proxy-liveness gate

- **Type:** bug
- **Depends on:** none

## Objectives

Fix the two defects found in Stage-5 live verification (issue #13): the egress
proxy crashed at boot on an empty `KEEP_EGRESS_PORT`, and `deploy.sh` declared
the deploy "verified live" while that security boundary was down. After this
stage a deploy with empty proxy env vars starts the proxy on its defaults, and a
proxy that fails to come up FAILS the deploy loudly.

## What to build

- **Defect 1 — `packages/keep_egress/src/keep_egress/runner.py`:** treat
  present-but-empty env vars as absent (fall back to the default), for
  `KEEP_EGRESS_PORT` (the crasher — `int("")` → ValueError) and
  `KEEP_EGRESS_HOST` (same latent hazard). Pattern:
  `int(os.environ.get("KEEP_EGRESS_PORT") or DEFAULT_PORT)`,
  `os.environ.get("KEEP_EGRESS_HOST") or DEFAULT_HOST`. Do the same for any
  other `KEEP_EGRESS_*` read the same way. Minimal, surgical.
- **Defect 2 — `deploy.sh` (and the unit if needed):** after startup, verify the
  egress-proxy container is actually running (e.g. `docker inspect -f
  '{{.State.Running}}'`, ideally after a brief settle, and/or a proxy liveness
  probe). If the proxy is not up, FAIL the deploy with a clear message — a dead
  proxy must never read as a successful deploy. Consider whether the proxy
  should be a fatal unit dependency rather than fire-and-forget `ExecStartPre`;
  at minimum deploy.sh's verify step must catch it.

## Interface contracts

- **Consumes:** `egress-observation.md` (unchanged — this is a startup/robustness
  fix, not a wire change). No contract edits.

## Testing requirements

- **Regression (Defect 1):** a unit test that sets `KEEP_EGRESS_PORT=""` (and
  `KEEP_EGRESS_HOST=""`) in the environment and asserts the runner resolves the
  DEFAULT_PORT/DEFAULT_HOST rather than raising — fails before the fix, passes
  after. Cover the "unset" and "explicit value" cases too.
- **Regression (Defect 2):** prove the deploy verify step rejects a non-running
  proxy. Cheapest honest form: a shell/unit-level test of the verify function
  with a stubbed `docker inspect` returning not-running → nonzero exit; or extend
  the render/verify coverage. If the local paired-topology container test can be
  made to assert proxy liveness via the deploy verify path, better.
- shellcheck stays clean; existing 620 unit + 23 container stay green.

## Acceptance conditions

- [ ] Reproduction captured + a regression test (fails before, passes after) for
      BOTH defects
- [ ] A deploy whose proxy fails to start now EXITS non-zero with a clear message
- [ ] `contracts/` untouched; no runtime semantics changed beyond the empty-env
      fallback
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (live re-smoke is the Operator's step after redeploy)

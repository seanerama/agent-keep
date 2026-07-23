# Intake assessment — reject empty/malformed secret lines (stage 24, bug)

- **Source:** Project Tester finding **F1**, GCP live test 2026-07-23
  (`LIVE-TEST-GCP-CHARTER.md` results sheet). Filed via /verity:plan.
- **Decision:** ACCEPT as one bug stage. Not SPLIT — the guard is the same
  few lines in two scripts plus tests in one existing file.

## Claim / reality verification (against source, 2026-07-23)

| Claim (from the live failure) | Reality in source | Verdict |
|---|---|---|
| Pipeline accepts an empty secret value end-to-end | Guards at `deploy-agent.sh:78–85` and `deploy.sh:97–104` reject only fully-empty stdin; `VAR=` (empty value) passes both | CONFIRMED (reproduced live) |
| No per-line format validation anywhere | `deploy.sh:246–249` pipes raw lines to the helper; `append-env` is a bare `cat >>` (`deploy/agent-keep-deploy:78–90`) | CONFIRMED — malformed lines can corrupt the root env file |
| Failure surfaces opaquely, late | Worker boots → `MissingApiKeyError` → restart loop → deploy fails at liveness gate with `curl (52)`; no message names the secret | CONFIRMED (journal captured in live test) |

## Contract safety

No frozen contract touched. The `VAR=value` stdin protocol is documented only
in script headers; this stage enforces what those docs already promise.
Additive failure mode (exit 64 pre-action). No ADR needed — no architectural
decision, no seam.

## Scope call

Client-side only. Helper-side (`append-env`) validation was considered and
deliberately deferred: the helper ships via bootstrap and versions
independently per host; bundling it would turn a one-line guard into a host
re-bootstrap concern. If wanted, it is its own chore.

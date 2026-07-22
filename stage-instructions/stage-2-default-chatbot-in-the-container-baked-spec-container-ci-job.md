# Stage 2: Default chatbot in the container: baked spec, container CI job

- **Type:** feature
- **Depends on:** 1

## Objectives

The thin agent inside the thick chassis: a default chatbot spec baked into a
hardened container image that boots, answers `/healthz`, takes a `POST /message`
on dev-http, runs the loop against the `static` provider, and writes audit
records — proven end-to-end by a real container CI job, hermetically.

## What to build

- `specs/default-chatbot.yaml` (`keep/v1`): dev-http channel, `static` provider
  for CI (model_router allows `anthropic` selection live), single-session or
  sqlite persistence, jsonl audit, token/budget accounting on, egress allowlist
  containing ONLY the model provider host (`api.anthropic.com`).
- Image build path (transplant/adapt the Foundry's build machinery only as far
  as needed to bake ONE spec — no fleet/composer breadth): non-root uid,
  absence-composed (unselected components not in the image).
- Image name per locked identity: `ghcr.io/seanerama/agent-keep-default-chatbot`
  (slug extends per ADR 0001); CI pushes `:edge` + `:<sha>` on main, never
  `:latest` (release flow owns that later).
- CI `container` job (marker `container`): build → `docker run` → healthz →
  scripted message round-trip via static provider → assert an `audit-record`
  line with the run-correlation key → assert non-root → absence-grep (left-out
  components truly absent from the image).

## Interface contracts

- **Exposes:** the runnable chassis image + baked-spec pattern Stages 3-5 wrap.
- **Consumes:** `agent-spec.md` (baked spec rules: no secrets, env names only),
  `internal-message.md` (dev-http normalizes at the boundary),
  `audit-record.md` (every model/tool call audited, digests-not-payloads),
  `run-lifecycle.md` (shape-only; no emitter composed).

## Testing requirements

- The container CI job above is the real test — no mocked docker.
- Unit: spec validation of `specs/default-chatbot.yaml`; wiring test that the
  composed component set matches the spec (absence semantics).

## Acceptance conditions

- [ ] Kill-switch: N/A as a runtime flag — the "off" state is the spec not being
      deployed; dark-launch = image published but no host runs it (recorded here
      deliberately: a chassis has no feature flags to hide behind)
- [ ] Observably-works asset authored: `scripts/smoke-chat.sh <host:port>` —
      curl healthz + one message round-trip, asserting a non-empty reply and a
      new audit line; the Operator runs it post-deploy (Stage 5, live)
- [ ] Additive migration only (no destructive schema change)
- [ ] Existing suite stays green; CI all-green

## Pipeline test: YES — the container job IS the pipeline proof for this stage

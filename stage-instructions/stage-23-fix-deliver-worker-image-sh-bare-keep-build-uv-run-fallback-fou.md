# Stage 23: Fix deliver-worker-image.sh bare keep-build (uv run fallback, found live)

- **Type:** bug
- **Depends on:** none

## Objectives
Fix issue #47, found in the AWS live test: `scripts/deliver-worker-image.sh` ran
`keep-build` as a bare command, but it is a uv workspace entry point (only on PATH
inside the venv). So `deploy.sh` local-image mode failed with `keep-build: command
not found` (exit 127) when invoked from a plain shell (how `deploy-agent.sh` runs it).

## What to build
- `scripts/deliver-worker-image.sh`: resolve keep-build robustly — bare command if
  on PATH (activated venv/CI), else `uv run keep-build` (uv project), overridable
  via `$KEEP_BUILD`; clear error if neither. Use an array so it's shellcheck-clean.

## Testing requirements
- shellcheck clean. Prove it: with `keep-build` NOT on PATH (plain shell), the
  script now builds via `uv run keep-build` — run `deliver-worker-image.sh <spec>
  <tag> LOCAL` and confirm it loads an image + prints the id (was exit 127).
- `bash scripts/lint.sh` PASS; existing suites green.

## Acceptance conditions
- [ ] Reproduction captured (#47 exit 127) + the deliver script builds outside the venv
- [ ] shellcheck clean; existing suite green; CI all-green

## Pipeline test: NO (the local-image deploy is exercised in the live cloud test)

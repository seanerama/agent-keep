# Stage 16: Pre-push lint hook: run the exact CI lint (ruff + shellcheck) locally

- **Type:** chore
- **Depends on:** none

## Objectives

Make local == CI for lint so a red lint is caught before push (issue #32). Two
CI round-trips were lost (stage 14 a missed `ruff format`, stage 15 an SC2015 the
local shellcheck version didn't emit). First in the backlog so it protects every
later stage.

## What to build

- `scripts/lint.sh` — runs the EXACT CI lint commands in one place:
  `uv run ruff check .`, `uv run ruff format --check .`, and
  `shellcheck deploy.sh deploy/agent-keep-deploy scripts/*.sh` (mirror ci.yml's
  lint job verbatim — read `.github/workflows/ci.yml` and match). Non-zero on any
  failure; clear per-tool output.
- A git pre-push hook that runs `scripts/lint.sh`, wired via a committed
  `.githooks/` dir + a one-line opt-in (`git config core.hooksPath .githooks`)
  documented in README/CONTRIBUTING — do NOT force-install into `.git/hooks`
  (respect the developer's setup; make enabling it one command).
- **Pin shellcheck version parity:** CI's `shellcheck` step uses the runner's
  preinstalled version, which differs from local (local 0.11.0 missed SC2015).
  Pin it: install a fixed shellcheck version in the ci.yml lint job (e.g. via a
  pinned action or a version-pinned download) AND document the same version for
  local use, so local and CI emit identical findings. Record the chosen version.
- Document in README/CONTRIBUTING: `scripts/lint.sh` + enabling the hook + the
  shellcheck version.

## Interface contracts

- **Consumes:** nothing new; wraps the existing ci.yml lint commands. No contract
  or runtime change.

## Testing requirements

- `scripts/lint.sh` is itself shellcheck-clean and, run on the current clean tree,
  exits 0. A trivially-broken file (temp, not committed) makes it exit non-zero —
  demonstrate/note the behavior.
- CI lint job still green with the pinned shellcheck version.

## Acceptance conditions

- [ ] Exit-state: `scripts/lint.sh` runs the exact CI lint set and is the single
      source both CI and the pre-push hook use; shellcheck version pinned so
      local == CI
- [ ] Hook is opt-in (committed `.githooks/`, one-command enable), not force-installed
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO

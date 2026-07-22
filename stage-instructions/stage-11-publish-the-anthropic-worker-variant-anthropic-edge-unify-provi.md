# Stage 11: Publish the anthropic worker variant (:anthropic-edge); unify provider spec naming

- **Type:** chore
- **Depends on:** none

## Objectives

Make all three providers uniformly one-command deployable so each can be
live-tested the same way. The ollama (`:ollama-edge`) and openai (`:openai-edge`)
worker variants are CI-published, but the anthropic variant
(`specs/default-chatbot.live.yaml`) is not — and the operator can't push it (no
ghcr write). Publish it via CI, and rename the spec to the consistent
`default-chatbot.anthropic.yaml` (alongside `.ollama.yaml` / `.openai.yaml`).

## What to build

- Rename `specs/default-chatbot.live.yaml` → `specs/default-chatbot.anthropic.yaml`
  (git mv; content unchanged — it is already `provider: anthropic`, egress
  `[api.anthropic.com:443]`). Update live references: `STATUS.md`,
  `docs/deploy/first-live-chassis.md` (Step B example → the new filename +
  `:anthropic-edge`). Leave historical `stage-instructions/` records as-is.
- `.github/workflows/ci.yml` publish job: add an anthropic build+push step
  mirroring the ollama/openai ones — build `specs/default-chatbot.anthropic.yaml`
  with an explicit `--tag`, push `:anthropic-edge` + `:anthropic-${GITHUB_SHA}`
  (same repo, never `:latest`).

## Interface contracts

- **Consumes:** nothing new; reuses keep-build + the existing ghcr publish path.
  No contract or runtime change.

## Testing requirements

- The renamed spec still validates via keep_spec (existing suite covers spec
  loading; run it). `keep-build build specs/default-chatbot.anthropic.yaml --tag …`
  builds locally. ci.yml stays valid YAML. Publish is main-only — the proof is the
  post-merge run landing `:anthropic-edge` on ghcr.

## Acceptance conditions

- [ ] Exit-state: post-merge, `ghcr.io/seanerama/agent-keep-default-chatbot:anthropic-edge`
      exists on ghcr; the three provider variants (anthropic/ollama/openai) are all
      published and deployable with one command
- [ ] Never `:latest`; static/mechanic/proxy/ollama/openai publishing unchanged
- [ ] No stale `default-chatbot.live.yaml` reference left in live docs/specs/tests
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (post-merge publish is the proof)

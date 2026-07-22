# Stage 9: CI publishes the Ollama worker image variant (:ollama-edge + :ollama-sha)

- **Type:** chore
- **Depends on:** none

## Objectives

Make the Ollama worker image available on ghcr so it can be deployed, without
requiring an operator workstation to have `write:packages` (the live-deploy
session hit that gap). CI already has the `packages:write` credential; extend the
main-only publish job to also build and push the ollama-spec worker variant.

## What to build

- `.github/workflows/ci.yml` publish-image job: add a build step for
  `specs/default-chatbot.ollama.yaml` with an explicit `--tag` (the ollama spec
  shares the `default-chatbot` slug, so it must not clobber the static `:latest`),
  then tag+push it as `:ollama-edge` (moving) and `:ollama-${GITHUB_SHA}`
  (immutable) on the same `agent-keep-default-chatbot` repo. Never `:latest`.
  Deploy with `./deploy.sh default-chatbot ollama-edge`.

## Interface contracts

- **Consumes:** nothing new; reuses the existing keep-build + ghcr publish path.
  No contract or runtime change.

## Testing requirements

- The publish job runs main-only and can't be exercised in a PR; coverage is the
  existing CI-side gates (yaml is well-formed; `keep-build build
  specs/default-chatbot.ollama.yaml --tag …` verified to build locally) plus
  watching the post-merge publish run go green and confirming the tags land on
  ghcr.

## Acceptance conditions

- [ ] Exit-state: post-merge, `ghcr.io/seanerama/agent-keep-default-chatbot:ollama-edge`
      exists on ghcr and `./deploy.sh default-chatbot ollama-edge` can pull it
- [ ] Never pushes `:latest`; static/mechanic/proxy publishing unchanged
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (post-merge publish + live deploy is the proof)

# Stage 14: Arbitrary-blueprint image path: deploy any spec (build + reach the host)

- **Type:** feature
- **Depends on:** none

## Objectives

The second leg of "two inputs → deployed" (ADR 0007): deploy ANY agent blueprint,
not just the CI-published `default-chatbot.<provider>` variants. Today `deploy.sh`
pulls the worker image by tag from ghcr (`ghcr.io/<owner>/agent-keep-<slug>:<tag>`),
which requires the image to already be published on a registry the host can pull —
and the operator may lack registry write. Close that: given a blueprint spec,
make its worker image exist and be reachable by the target host.

## What to build

- A build-and-deliver path for an arbitrary spec. Recommended primary mechanism
  (builder confirms against constraints): **build the image locally (or on the
  host) from the spec and load it onto the target**, so no registry write is
  required:
  - `keep-build build <spec> --tag <ref>` already produces a local image
    (verified). Add a deliver step: `docker save <ref> | ssh <host> docker load`
    (streamed; no intermediate file), or build directly on the host if the build
    context is shipped.
  - Teach `deploy.sh` a **local-image mode**: when the worker image is already
    present on the host (loaded, not pulled), skip the `docker pull` for the
    worker and digest-pin the loaded image. Keep the pull path as-is for
    registry-published tags (proxy/mechanic still pull `:edge`). A clear flag or
    auto-detect (image present locally → skip pull) — builder's call, documented.
  - The image slug still derives from the blueprint's locked identity; the worker
    image name follows `agent-keep-<slug>` (per-spec). For the default-chatbot
    family this is the same repo with a tag; a genuinely different blueprint slug
    is its own image name.
- Keep the CI-published-variant path (registry pull) working unchanged — this
  ADDS the no-registry-write path, does not replace it.

## Interface contracts

- **Consumes:** frozen `agent-spec` (the blueprint), the existing keep-build +
  deploy.sh. No contract edit; the worker image ref is the seam and is unchanged
  (still `agent-keep-<slug>@<digest>`), just sourced from a load instead of a pull.

## Testing requirements

- Unit/integration: `keep-build build <spec> --tag` produces an image (exists);
  the `docker save | docker load` round-trip preserves it (local docker); a
  stub-ssh test that deploy.sh's local-image mode skips the worker pull and
  digest-pins the loaded image (mirror the stub-ssh deploy tests).
- Container (`-m container`): build an arbitrary non-default spec image, load it,
  and assert deploy.sh (local-image mode, against a local docker "host") stands up
  the worker from the loaded image (no registry).
- shellcheck clean; existing suites green; the CI-published pull path still works.

## Acceptance conditions

- [ ] Kill-switch: N/A — additive deploy capability (recorded)
- [ ] Observably-works: an arbitrary blueprint's image is built, loaded to a host,
      and deployed WITHOUT any registry push (the no-ghcr-write path)
- [ ] The registry-pull path (CI variants) still works unchanged
- [ ] Additive only; `contracts/` untouched
- [ ] Existing suite stays green; CI all-green

## Pipeline test: YES — the container job builds+loads+deploys an arbitrary spec

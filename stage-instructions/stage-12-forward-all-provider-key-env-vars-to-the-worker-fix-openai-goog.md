# Stage 12: Forward all provider key env vars to the worker (fix openai/google boot)

- **Type:** bug
- **Depends on:** none

## Objectives

Fix issue #23, found in the OpenAI live test: the systemd unit's worker
`docker run` forwarded only `-e ANTHROPIC_API_KEY`, so an openai (or future
google) worker never received its key and crashed at boot with
`MissingApiKeyError`. Make the worker provider-agnostic for key passthrough.

## What to build

- `deploy/systemd/agent-keep@.service`: add `-e OPENAI_API_KEY` and
  `-e GOOGLE_API_KEY` to the worker `docker run`, alongside the existing
  `-e ANTHROPIC_API_KEY` (bare `-e VAR` no-value form — docker omits an unset one,
  so no empty-string crash for non-selected providers; cf issue #13). The key is
  still injected only at deploy time via KEEP_DEPLOY_SECRETS (stage 7); this just
  forwards it into the container.
- `tests/deploy/test_systemd_render.py`: assert all three provider key vars are
  present in the worker ExecStart as passthrough (`-e VAR`, no `VAR=` literal).

Note: a fully-agnostic forwarding (deploy.sh computing `-e` args from the injected
secret var names) needs a shell wrapper in ExecStart because systemd does not
word-split expanded variables — deferred; the known-cloud-keys list covers the
implemented + planned providers (anthropic/openai/google).

## Interface contracts

- **Consumes:** nothing new. No contract or runtime change; deploy topology only.

## Testing requirements

- Regression: the render test asserts the three key passthroughs (fails before —
  only ANTHROPIC was present). shellcheck clean; existing suites green.
- The real proof is the post-merge redeploy of the openai variant booting healthy
  (operator/live step).

## Acceptance conditions

- [ ] Reproduction captured (issue #23 journald crash) + render-test regression
- [ ] Worker forwards anthropic/openai/google keys as passthrough; no value in git
- [ ] No empty-string crash for an unset key (bare `-e VAR` omitted by docker)
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (live redeploy of the openai variant is the proof)

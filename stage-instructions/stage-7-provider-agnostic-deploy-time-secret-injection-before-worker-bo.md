# Stage 7: Provider-agnostic deploy-time secret injection (before worker boot)

- **Type:** chore
- **Depends on:** none

## Objectives

Make going live a single honest command for ANY provider. Today a live worker
(e.g. `provider: anthropic`) builds its provider eagerly at boot and refuses to
start without its key (`anthropic_provider.py:123`, `runner.py:233`), but
`deploy.sh` writes the env file and starts the worker in one flow — so the key
can only be appended AFTER the worker has already crashed. This stage lets
`deploy.sh` inject operator-supplied secret env vars into the env file
(root:0600) BEFORE the worker starts. Provider-agnostic: the secrets are
arbitrary `VAR=value` lines — an Anthropic key, an OpenAI key, a Google key,
several, or NONE (a local Ollama needs no secret). See issue #15 for the broader
provider-agnostic direction (Architect-owned); this stage is only the deploy seam.

## What to build

- `deploy.sh`: when `KEEP_DEPLOY_SECRETS=1`, read `VAR=value` lines from stdin
  ONCE up front (before any ssh), into a shell variable. After `write-env` and
  BEFORE `service … restart`, pipe them to the existing scoped-helper
  `append-env <slug>` verb (root:0600, stdin-only — never argv/log). Never echo
  the secret values. Empty stdin with the flag set = clear error, exit non-zero.
- Make the env-write comment block provider-neutral (drop the Anthropic-specific
  wording; mention ANTHROPIC/OPENAI/GOOGLE keys or none as examples).
- `docs/deploy/first-live-chassis.md`: rewrite Step B so go-live is one command,
  e.g. `printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" | KEEP_DEPLOY_SECRETS=1 ./deploy.sh
  default-chatbot live` — and show the provider-neutral shape (OpenAI/Google/none
  for Ollama). Note the redeploy semantics: `write-env` overwrites, so secrets are
  re-supplied each deploy (explicit provenance, nothing secret persisted in the
  tracked flow beyond the host env file).

## Interface contracts

- **Consumes:** the scoped helper's existing `append-env`/`write-env`/`service`
  verbs (unchanged). No contract edits. No runtime code change.

## Testing requirements

- A test that with `KEEP_DEPLOY_SECRETS=1` and secrets piped on stdin, `deploy.sh`
  invokes the helper `append-env` BEFORE `service … restart` (ordering is the
  whole point). Cheapest honest form: a stubbed `ssh` on PATH that appends its
  argv (and, for append-env, a marker) to a log, run deploy.sh against it, assert
  the log shows `append-env` before `service … restart`. Also assert: no flag →
  no append-env call; flag + empty stdin → non-zero with a clear message; the
  secret VALUE never appears in deploy.sh's own stdout/stderr.
- shellcheck stays clean; existing suites stay green.

## Acceptance conditions

- [ ] Exit-state: a single `KEEP_DEPLOY_SECRETS=1 … | ./deploy.sh <slug> <tag>`
      brings a key-requiring worker up healthy in one command (no crash-then-fix)
- [ ] Provider-agnostic: works for any `VAR=value` (or none); nothing hardcodes
      a provider name in the injection path
- [ ] Secret VALUE never in git, argv, logs, or deploy.sh output; lands only in
      the root:0600 host env file via the existing helper
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (live re-verify is the Operator's, when they run Step B)

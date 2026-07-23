# Stage 24: Reject empty or malformed secret lines at the deploy entry points (found live)

- **Type:** bug
- **Depends on:** none

## Objectives

Make `KEEP_DEPLOY_SECRETS=1` fail **fast and loud at the workstation** when any
piped secret line is empty-valued or malformed, instead of shipping it and
failing minutes later as an opaque `curl (52)` at the liveness gate.

**Live reproduction (2026-07-23, GCP live test, finding F1):** the operator ran
the documented pipe with an unresolved placeholder path; `cat` failed, so stdin
carried `ANTHROPIC_API_KEY=` (empty value). Both existing guards passed — they
only reject **fully empty stdin** (`deploy-agent.sh:78–85`, `deploy.sh:97–104`)
— so the empty value was appended to the host env file, the anthropic worker
crashed at boot with `MissingApiKeyError`, systemd restart-looped, and the
deploy failed downstream with `curl: (52) Empty reply from server`. Full record:
`LIVE-TEST-GCP-CHARTER.md` (results sheet, F1).

Verified wider during intake: NO per-line validation exists anywhere on the
path — `deploy.sh:246–249` pipes raw lines to the host helper, whose
`append-env` is a bare `cat >>` (`deploy/agent-keep-deploy:78–90`). A line with
no `=` at all, or an invalid var name, lands verbatim in the root:0600 env file
and can corrupt it for the systemd `EnvironmentFile` parse.

## What to build

Client-side per-line validation, immediately after the existing empty-stdin
guard, in **both** entry points (deploy-agent.sh may thread to deploy.sh, but
deploy.sh is also directly operator-usable):

- `scripts/deploy-agent.sh` (after line 85) and `deploy.sh` (after line 104):
  every non-empty line of `DEPLOY_SECRETS` must match
  `^[A-Za-z_][A-Za-z0-9_]*=.+$` — a valid env var name AND a non-empty value.
- On violation: print which line NUMBER and which VAR NAME failed and why
  (empty value / malformed) — **NEVER echo the value or the raw line** (the
  no-secrets-in-logs discipline of Stage 7 applies to error paths too); point
  at the documented pipe example; `exit 64` BEFORE any build, ssh, or remote
  action.
- Keep the check pure bash (no new dependencies); shellcheck-clean per the
  Stage 16 pre-push lint.

Out of scope (deliberately): helper-side (`append-env`) validation on the host
— defense-in-depth candidate, but the helper is installed by bootstrap and
versions independently; widening this stage to reship the helper turns a
one-liner guard into a host-migration concern. File separately if wanted.

## Interface contracts

- **Exposes:** unchanged CLI surface; new documented failure mode (exit 64 +
  named-var stderr message) for invalid secret lines.
- **Consumes:** the `KEEP_DEPLOY_SECRETS` stdin protocol as documented in the
  headers of `deploy-agent.sh` (lines 37–42) and `deploy.sh` (lines 89–95) —
  `VAR=value` lines. No frozen contract (`contracts/`) is touched; this
  formalizes what the docs already promise.

## Testing requirements

Extend `tests/deploy/test_deploy_secret_injection.py` (regression home — it
already exercises the stdin path):

- `ANTHROPIC_API_KEY=` (empty value) → exit 64, stderr names
  `ANTHROPIC_API_KEY`, **value-free stderr**, and NO ssh/build action was
  attempted (the F1 regression, fails before / passes after).
- `not-a-var-line` (no `=`) and `9BAD=x` (invalid name) → exit 64.
- Multi-line: one good line + one bad line → exit 64 (all-or-nothing; nothing
  is shipped).
- Happy path `GOOD_KEY=value` still reaches injection unchanged (existing
  tests stay green).
- Same matrix against BOTH `deploy.sh` and `scripts/deploy-agent.sh`.

## Acceptance conditions

- [ ] Reproduction captured + a regression test (fails before, passes after)
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO

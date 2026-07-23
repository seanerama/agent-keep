# Stage 13: Host bootstrap: any fresh Ubuntu+Docker host to deploy-ready

- **Type:** feature
- **Depends on:** none

## Objectives

The first leg of the "two inputs → deployed" north star (ADR 0007): a single
command that takes ANY fresh Ubuntu host — local, a VM you provisioned, or one a
client handed you in their tenant — to **deploy-ready** for `deploy.sh`. Replaces
the manual preflight steps that today live only in
`docs/deploy/first-live-chassis.md`. Input: a target (SSH). Effect: conformant
host.

## What to build

- `scripts/bootstrap-host.sh <ssh-target>` (or a `keep bootstrap` verb): runs
  from the operator's workstation, drives the host over SSH:
  - **Docker**: verify present; if absent, install the official Docker Engine
    (idempotent; skip if already there). Confirm the invoking user can run docker
    (in the `docker` group or via the install).
  - **Scoped root helper + sudoers**: install `deploy/agent-keep-deploy` →
    `/usr/local/sbin/agent-keep-deploy` (0755 root) and a sudoers drop-in from
    `deploy/sudoers-agent-keep` with `<deploy-user>` substituted to the SSH user
    (0440), validated with `visudo -c`. This is the exact manual sequence in the
    runbook §1 — automate it, including the user substitution (no hand-edit).
  - **Verify**: end with a conformance check — docker works, the helper is
    installed and callable via `sudo -n`, systemd present — printing a clear
    `HOST READY` (or a precise failure).
  - Idempotent: safe to re-run (re-install the helper when it changes in-repo).
- Update `docs/deploy/first-live-chassis.md` §1–2 to call the bootstrap script
  instead of the manual steps.
- Fix the stale `default-chatbot.live.yaml` doc-comment in `deploy.sh` (renamed to
  `.anthropic.yaml` in stage 11) while here — tiny, in-scope cleanup.

## Interface contracts

- **Consumes:** the existing `deploy/agent-keep-deploy` + `deploy/sudoers-agent-keep`
  (unchanged). No contract or runtime change; deploy machinery only.

## Testing requirements

- shellcheck clean on the new script.
- A test of the user-substitution + sudoers-render logic (the risky part) with a
  stubbed ssh (mirror `tests/deploy/test_deploy_secret_injection.py`'s stub-ssh
  pattern): assert the helper install + sudoers-with-substituted-user + `visudo -c`
  are driven in order; a re-run is idempotent; a bad SSH target fails clearly.
- The real proof is bootstrapping a fresh host (a throwaway VM or a local docker
  host) then a successful `deploy.sh` against it — the operator/live step
  (also exercised as Stage 15's skeleton).

## Acceptance conditions

- [ ] Kill-switch: N/A (an operator tool, not a runtime feature) — recorded here
- [ ] Observably-works asset: the script's own `HOST READY` conformance check IS
      the smoke; a fresh host bootstraps then accepts a `deploy.sh`
- [ ] Additive only; `contracts/` untouched; deploy machinery only
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (live bootstrap of a fresh host is the proof)

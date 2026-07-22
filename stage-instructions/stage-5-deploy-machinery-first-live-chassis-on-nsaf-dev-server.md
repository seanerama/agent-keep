# Stage 5: Deploy machinery + first live chassis on NSAF dev server

- **Type:** chore
- **Depends on:** 3,4

## Objectives

The walking skeleton's last leg: transplant the deploy machinery re-namespaced
to `agent-keep`, and put the first live chassis (default chatbot + egress proxy
+ paired mechanic) on the NSAF dev server under systemd (ADR 0004). Live smoke
proves what CI cannot: the egress ALLOW path through the proxy against the real
Anthropic API, with every direction audited.

## What to build

- Transplant + re-namespace from the Foundry `deploy/` + `deploy.sh`:
  systemd template (`agent-keep@.service`, `EnvironmentFile=
  /etc/agent-keep/%i.env`, full OPS-01 hardening carried: no-new-privileges,
  cap-drop ALL, read-only rootfs + named data volume + tmpfs, pids/memory
  caps, non-root uid), scoped root helper `agent-keep-deploy` +
  `sudoers-agent-keep`, env templates (NAMES only).
- Extend the unit/deploy flow for the paired topology (proxy + worker +
  mechanic, shared bundle dir per Stage 4, private network per Stage 3) —
  the predecessor's single-container unit is the base, the pairing is new.
- `deploy.sh`: `DEPLOY_HOST` from env (ADR 0004 / gitignored
  `.verity/deploy-access.md`), digest-pinned `IMAGE_REF`, idempotent, rollback
  = re-run previous tag; verify step runs the three smoke scripts authored in
  Stages 2-4.
- Port assignment for the two dev-http surfaces (worker, mechanic) — static
  map entries under the predecessor's scheme.

## Interface contracts

- **Exposes:** the operator's deploy path; the live environment `/verity:ship`
  and `/verity:verify` operate against.
- **Consumes:** ADR 0004 (target), `.verity/deploy-access.md` (access,
  out-of-band), `agent-spec.md` rule 3 (secret VALUES only on host, root:0600),
  the smoke scripts from Stages 2-4.

## Testing requirements

- CI cannot reach the tailnet: CI-side coverage = shellcheck on deploy scripts
  + a unit-render test of the systemd template env expansion.
- The real test is the live smoke (below) — run by the Operator over SSH,
  results pasted into the stage's PR/issue.

## Acceptance conditions

- [ ] Exit-state: chassis live on the NSAF dev server under systemd —
      `smoke-chat.sh` (real Anthropic reply through the proxy),
      `smoke-egress.sh` (live denial audited), `smoke-mechanic.sh` (cited
      answer) all pass over the tailnet
- [ ] Audit log on host shows: inbound message, model call, `egress` ALLOW
      record for `api.anthropic.com`, token accounting for the run
- [ ] No secret value in git or image; `/etc/agent-keep/<slug>.env` root:0600
- [ ] Rollback path exercised once (redeploy previous tag succeeds)
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (live verification by Operator is the gate)

# Deployment abstraction — walking skeleton (Architect → Planner handoff)

> Design: ADR 0007 (conformant host; two inputs → deployed) + ADR 0008 (two-plane:
> chassis on host, model as provider). Issue #21. Hand to `/verity:plan`.

## North star

**Two inputs deploy any agent: `(blueprint, target)`.** The operator provides an
agent spec and a host; bootstrap, image, the audited paired topology, secret
injection, and verification are all automated behind those two inputs. Works on
local + any cloud VM (bring-your-own-host); clouds and data-locality are
parameters, not separate deployment models.

## What already exists (do not rebuild)

- `deploy.sh` — target-agnostic (`DEPLOY_HOST` = any SSH-reachable host, incl.
  bastion/VPN/tailscale/cloudflared), digest-pins images, injects secrets before
  start (KEEP_DEPLOY_SECRETS), verifies proxy + worker + mechanic liveness.
- The audited paired topology (systemd unit: worker + proxy + mechanic + ingress
  on the two-network no-route layout) — proven live across 3 providers.
- The provider abstraction (the model plane, ADR 0008): anthropic / ollama /
  openai, each an allowlisted, audited egress target.

## The gaps to close (for `/verity:plan` to stage)

1. **Host bootstrap** — one script/command that takes ANY fresh Ubuntu+Docker
   host (local, self-provisioned VM, or client-provided) to deploy-ready:
   installs Docker if absent, installs the scoped root helper + sudoers
   (currently the runbook's MANUAL preflight), verifies. Input: a target (SSH).
   This is the biggest simplicity win and the natural **first stage**.
2. **Arbitrary-blueprint image path** — deploy ANY spec, not just the shipped
   `default-chatbot.<provider>` variants. Today CI publishes fixed variants and
   the operator lacks ghcr write. Options for the planner to weigh: build-on-host,
   build-and-push to a registry the target can pull, or build-locally-and-transfer.
   The "any blueprint" in the north star depends on this.
3. **Single deploy entry point** — a wrapper realizing `(blueprint, target) →
   deployed`: ensure the target is conformant (bootstrap if needed) → resolve/
   build the blueprint's image → deploy the paired topology → verify. Thin
   orchestration over the pieces above + `deploy.sh`.

## Walking skeleton (Stage 0 for this phase)

The thinnest slice proving the north star on the simplest target: **bootstrap a
fresh conformant host and deploy an arbitrary blueprint to it with a single
`(blueprint, target)` invocation**, ending green (audited paired topology up,
one real smoke). Prove it on a local Docker host or a throwaway cloud VM — the
same path both.

## Explicitly deferred (per-cloud conveniences, on demand — not the skeleton)

- Per-cloud provisioners (`gcloud`/`az`/`aws` scripts or a Terraform module) that
  create a conformant VM + firewall and hand it to the bootstrap. Add one cloud
  at a time, driven by real need.
- In-tenant serverless GPU model endpoints (ADR 0008) — a provider/model-plane
  concern, added when a data-sovereignty client needs self-hosted inference.
- Kubernetes / cloud-native chassis targets — only if a client mandates it (own ADR).

## Contract note

No new frozen contract required. The two inputs reuse frozen `agent-spec` (the
blueprint) and the `.verity/deploy-access.md` pattern (the target). If a formal
deploy-target descriptor emerges, the Planner issues it as a new contract.

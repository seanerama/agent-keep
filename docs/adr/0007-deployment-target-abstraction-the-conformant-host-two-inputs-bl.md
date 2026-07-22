# 0007. Deployment-target abstraction: the conformant host; two inputs (blueprint + target) deploy any agent

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

Agent Keep instances must deploy across local/on-prem and AWS/GCP/Azure — as
simply as possible, and often **into a client's own cloud tenant** (data
sovereignty; co-locating the agent with the systems/tools it calls to cut
latency and keep traffic in-tenant). The defining constraint is the **audited
egress boundary** (ADR 0002): the worker sits on a no-route network and reaches
the outside ONLY through the observation proxy, allowlist-enforced and audited.
Any deployment model that weakens that isn't Agent Keep.

Standard multi-cloud advice (Cloud Run, Azure Container Apps, ECS/Fargate)
optimizes cost/scale but degrades this boundary: shared-network-namespace
platforms let the worker reach egress *around* the proxy. Kubernetes gives
portability at the cost of a heavy dependency and a boundary that leans on CNI
NetworkPolicy enforcement.

Operator direction (2026-07-22): the whole deployment effort exists to make
deploying an agent **simple** — "any agent blueprint should be two inputs and
it's deployed" — with clouds and data-locality as *parameters*, not separate
deployment models.

## Decision

**The deployment target is a "conformant host": a Linux box with Docker +
systemd, reachable over SSH.** Local machines, self-provisioned cloud VMs, and
client-provided VMs in their tenant are all the same target. This keeps the
audited topology (worker + proxy + mechanic on the two-network no-route layout)
**single-sourced** — one security implementation, everywhere.

- **North star — two inputs → deployed:** the operator provides (1) the
  **blueprint** (the agent spec) and (2) the **target** (a host). Everything else
  — host bootstrap, image build/resolve, the paired topology, secret injection,
  liveness verification — is automated behind those two inputs.
- **Bring-your-own-host is the baseline.** A client may hand you a VM in their
  tenant, reachable over their network. So provisioning is NOT part of the
  abstraction; a uniform **bootstrap** makes any fresh Ubuntu+Docker host
  deploy-ready (Docker, the scoped helper + sudoers, verification).
- **Per-cloud provisioners are optional conveniences**, added on demand (a
  `gcloud`/`az`/`aws` script or a Terraform module that creates a conformant VM +
  firewall and hands it to the bootstrap). Never required.
- **Access is flexible SSH per deployment** — direct, ProxyJump/bastion, the
  client's VPN, Tailscale, or cloudflared — whatever the target environment
  allows, recorded in the gitignored per-app `.verity/deploy-access.md`.
  `deploy.sh` already reaches any of these via `DEPLOY_HOST`.
- **Kubernetes is out of scope** for a single agent; the shared Docker image
  already gives portability. Revisit only if a client environment is K8s-native
  and mandates it — its own ADR.

## Alternatives considered

- **Cloud-native serverless as the chassis substrate** (Cloud Run / ACA / Fargate):
  best cost/scale for a generic app, but the no-route-worker boundary degrades on
  shared-namespace platforms — and the chassis is CPU-light, so the scale-to-zero
  cost argument barely applies to it (ADR 0008). Rejected as the substrate;
  remains a possible future target behind this abstraction if a client requires it.
- **Kubernetes everywhere** (GKE/AKS/EKS + local): portable/managed, but heavy for
  one agent and the boundary depends on CNI NetworkPolicy. Rejected now.
- **Commit to Terraform-everywhere / Tailscale-everywhere:** rejected — a client's
  tenant/network is theirs; provisioning and access must be pluggable, not required.

## Consequences

- One security topology, proven live, runs unchanged on local + any cloud VM.
- The chassis is CPU-light (ADR 0008), so always-on hosting is cheap; no
  scale-to-zero pressure on the chassis.
- New work: (a) a host **bootstrap** (any host → deploy-ready), (b) an
  **arbitrary-blueprint image path** (deploy any spec, not just the shipped
  variants), (c) a **single deploy entry point** realizing (blueprint, target) →
  deployed. Per-cloud provisioners are incremental, one cloud at a time.
- No new frozen contract: the two inputs reuse the frozen `agent-spec` (blueprint)
  and the existing `.verity/deploy-access.md` pattern (target). A deploy-target
  descriptor may be formalized by `/verity:plan` if a real seam emerges.

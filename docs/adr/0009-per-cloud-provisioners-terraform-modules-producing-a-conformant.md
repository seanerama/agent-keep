# 0009. Per-cloud provisioners: Terraform modules producing a conformant host, AWS first

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

ADR 0007 locked the deployment abstraction: the target is a **conformant host**
(Ubuntu + Docker + systemd, SSH-reachable), and per-cloud provisioners are
optional conveniences that "create a conformant VM + firewall and hand it to the
bootstrap." Issue #21 asks to make deployment first-class across local + GCP /
Azure / AWS. This ADR designs the provisioner mechanism (the deferred half of
ADR 0007). The hard parts — the paired proxy/worker/mechanic topology, the
`--internal` no-route network, the egress proxy, `bootstrap-host.sh`,
`deploy-agent.sh` — already exist at the HOST level and run on any Ubuntu+Docker
VM, so a provisioner is thin: make the VM exist and be reachable.

Workstation reality (2026-07-23): the `aws` CLI is installed and authed
(account 979096371461); `gcloud`, `az`, and `terraform` are not installed.

## Decision

**Per-cloud provisioners are Terraform modules that produce a conformant host,
then hand off to the existing bootstrap + entry point. Nothing about the chassis,
topology, or security model changes.** (Operator-selected mechanism, 2026-07-23.)

- **Terraform, uniform interface.** One module per cloud under `provision/<cloud>/`
  (`provision/aws/` first), each exposing the SAME variables (ssh public key,
  instance size, allowed-SSH CIDR, region/zone, name) and the SAME outputs (the
  ssh target `user@public-ip`, the instance id). The uniform `terraform apply` /
  `terraform destroy` workflow across clouds is the payoff for the multi-cloud
  goal; clean teardown matters for ephemeral/client VMs.
- **The module's whole job:** a conformant Ubuntu 24.04 VM + a security group /
  firewall (SSH ingress locked to the allowed CIDR, egress open for image pulls +
  provider APIs), registering the operator's existing SSH public key. It does NOT
  install the chassis or reproduce the docker topology — that is `deploy.sh`'s job
  on any VM. Docker itself is installed by the existing `bootstrap-host.sh` (or an
  optional minimal cloud-init).
- **x86_64 instances (constraint).** The chassis images are built amd64 (CI
  `ubuntu-latest` + the x86 workstation), so provisioned instances MUST be x86_64
  (e.g. `t3.small`/`t3.medium`), NOT Graviton/arm64. Multi-arch image builds are a
  separate future option; until then, arm64 is out.
- **Local, gitignored Terraform state.** State lives in the provisioner dir,
  gitignored (never committed — it can contain resource metadata). A remote
  backend (S3 + lock) is a later option for teams; not worth standing up just to
  make a VM. `.tfvars` with real values are gitignored; a committed
  `terraform.tfvars.example` documents the shape.
- **Handoff, no coupling.** Provisioning is a SEPARATE, credentialed step from
  deploy: `terraform apply` → capture the ssh-target output → run
  `bootstrap-host.sh <target>` → `deploy-agent.sh <blueprint> <target>`. Provision
  once, deploy many. `.verity/deploy-access.md` records the target + how it was
  provisioned (locations only, no secrets). No auto-provisioning inside
  `deploy-agent.sh` for now.
- **No new frozen contract** (ADR 0007): the provisioner's "output" is an ssh
  target; it reuses `DEPLOY_HOST` + the access-file pattern.
- **AWS first** (only authed here); GCP/Azure follow the identical module
  interface once `gcloud`/`az` + accounts are set up (operator step).

## Alternatives considered

- **Per-cloud CLI + cloud-init scripts** (aws/gcloud/az bash): the `aws` CLI is
  already authed, so AWS could run today with no new tooling — lighter. Rejected
  as the primary: three divergent scripts, manual teardown/idempotency, and the
  multi-cloud uniformity (one workflow across all three) is exactly what #21 wants
  — which Terraform gives and bash does not.
- **Terraform-everywhere as REQUIRED** (baked into the abstraction): rejected by
  ADR 0007 — provisioners stay optional conveniences; bring-your-own-host and the
  bootstrap remain the baseline for client-provided VMs.
- **Cloud-native container services** (Cloud Run / ACI / Fargate): rejected in
  ADR 0007 (the no-route-worker boundary degrades on shared-namespace platforms).

## Consequences

- One new dependency for the operator (Terraform install); one-time.
- Teardown is a first-class `terraform destroy` — good for testing + ephemeral
  client VMs.
- The provisioner is genuinely thin; the security-sensitive topology stays
  single-sourced in the already-proven deploy machinery.
- Deferred, on-demand: static egress IP / NAT for partner allowlisting; the
  in-tenant serverless-GPU model endpoint (ADR 0008); a remote Terraform backend;
  tailscale-in-cloud-init; GCP + Azure modules (each a later stage on this
  interface).

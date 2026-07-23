# Per-cloud provisioner — walking skeleton (Architect → Planner handoff)

> Design: ADR 0009 (Terraform modules → conformant host; AWS first) on top of
> ADR 0007 (conformant-host abstraction). Issue #21. Hand to `/verity:plan`.

## North star

`terraform apply` in a cloud module produces a conformant host; then the EXISTING
`bootstrap-host.sh` + `deploy-agent.sh <blueprint> <target>` deploy the audited
chassis unchanged. Uniform `apply`/`destroy` workflow across clouds; provision
once, deploy many. AWS first; GCP/Azure follow the identical module interface.

## What already exists (do not rebuild)

- `bootstrap-host.sh` (any fresh Ubuntu+Docker host → deploy-ready) and
  `deploy-agent.sh <blueprint> <target>` (the two-inputs entry point) — both
  cloud-agnostic, both proven.
- `deploy.sh` + the systemd unit create the `--internal` no-route network + egress
  proxy topology on ANY Linux+Docker VM — the provisioner does not touch this.

## The gap to close (for `/verity:plan` to stage)

1. **`provision/aws/` Terraform module** — the walking skeleton and first stage:
   a conformant Ubuntu 24.04 **x86_64** (amd64 image constraint, ADR 0009) EC2
   instance + a security group (SSH ingress locked to an allowed CIDR var; egress
   open), registering the operator's existing SSH public key. Uniform variables
   (ssh key, instance size, allowed-SSH CIDR, region, name) + outputs (ssh target
   `user@public-ip`, instance id). Local gitignored state; `terraform.tfvars.example`
   committed, real `.tfvars` gitignored. `terraform destroy` tears it down.
2. **A thin provision→deploy runbook / wrapper** — documents (or scripts) the
   handoff: `terraform apply` → capture ssh-target output → `bootstrap-host.sh` →
   `deploy-agent.sh <blueprint> <target>` → smoke → optional `terraform destroy`.
   Update `.verity/deploy-access.md` (target + provisioning method, locations only).
3. **CI-side coverage** — Terraform is not reachable-to-real-AWS in CI, so:
   `terraform fmt -check` + `terraform validate` on the module (no apply), plus a
   plan-shape / variable-contract check. The real proof is an operator `apply` on
   AWS (the live step).

## Walking skeleton (Stage 0 for this phase)

`terraform apply` a real EC2 VM in AWS (authed here) → `bootstrap-host.sh` it →
`deploy-agent.sh <a blueprint> <that VM>` → one live smoke green → `terraform
destroy`. This proves the provisioner is a true drop-in for the conformant-host
target — same deploy interface as a local host — end to end on a real cloud VM.

## Explicitly deferred (per ADR 0009 — later stages / on demand)

- **GCP and Azure modules** (each a later stage on the same variable/output
  interface; need `gcloud`/`az` + accounts first — operator setup).
- Static egress IP / NAT for partner allowlisting; in-tenant serverless-GPU model
  endpoint (ADR 0008); remote Terraform backend (S3+lock); tailscale-in-cloud-init;
  auto-provisioning from inside `deploy-agent.sh`.

## Contract note

No new frozen contract (ADR 0007/0009). The provisioner's output is an ssh target;
it reuses `DEPLOY_HOST` + the `.verity/deploy-access.md` pattern.

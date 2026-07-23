# Stage 21: GCP provisioner: Terraform module (conformant GCE) on the uniform interface

- **Type:** feature
- **Depends on:** none

## Objectives

The GCP provisioner (ADR 0009, issue #21): a `provision/gcp/` Terraform module
that stands up a conformant host (Ubuntu + Docker + systemd, SSH-reachable) as a
GCE instance, on the SAME uniform variable/output interface as `provision/aws/`
(stage 20). The existing `bootstrap-host.sh` + `deploy-agent.sh` then deploy
unchanged. Second cloud on the abstraction; Azure deferred (no account yet).

## What to build

Mirror `provision/aws/` (read it) for GCP, using the `google` provider:

- **`provision/gcp/`** module: `versions.tf` (required_providers google pinned to
  a major; required_version floor), `variables.tf`, `main.tf`, `outputs.tf`,
  `terraform.tfvars.example`.
- **Instance:** a `google_compute_instance`, **x86_64** (ADR 0009 amd64 image
  constraint) — boot disk from the public Ubuntu 24.04 LTS image family
  `ubuntu-os-cloud/ubuntu-2404-lts-amd64` (a `data "google_compute_image"` with
  `family = "ubuntu-2404-lts-amd64"`, `project = "ubuntu-os-cloud"` — no stale
  pinned image). Default `instance_size` (→ `machine_type`) an x86 type, e.g.
  `e2-small` (NOT a `t2a-*`/arm type). ~20 GB balanced/pd-ssd boot disk. An
  external IP (access_config) so it's SSH-reachable.
- **Firewall:** a `google_compute_firewall` allowing **tcp:22 ingress from
  `var.allowed_ssh_cidr` ONLY** (no default; validation REJECTS `0.0.0.0/0`).
  Egress is open by default on GCP (fine — image pulls + provider APIs). Do NOT
  open dev-http (8377/8477) — loopback + SSH tunnel. Scope the firewall by a
  target tag on the instance.
- **SSH key:** GCE metadata `ssh-keys = "ubuntu:${var.ssh_public_key}"` (login
  `ubuntu`; public key only — validation rejects a private key, like AWS).
- **Uniform interface** (MUST match aws/ so the deploy handoff + future modules
  are identical): variables `ssh_public_key`, `instance_size` (default `e2-small`),
  `allowed_ssh_cidr` (no default, reject 0.0.0.0/0), `region` (default
  `us-central1`), `name` (default `agent-keep`). GCP-specific additions:
  `project` (**default `agent-keep-kn6r6i`** — the dedicated project already
  created + billing-linked + Compute API enabled), `zone` (default derived, e.g.
  `${region}-a`). Outputs EXACTLY: `ssh_target = "ubuntu@${external_ip}"`,
  `instance_id`, `public_ip` (the external IP).
- **`terraform.tfvars.example`** documents every variable with placeholders
  (allowed_ssh_cidr = your IP /32; ssh_public_key = your PUBLIC key; project
  defaults to agent-keep-kn6r6i). Real `terraform.tfvars` gitignored (the stage-20
  `.gitignore` rules already cover `provision/**`).
- **CI:** the stage-20 lint-job Terraform step must also cover `provision/gcp/`
  (extend the fmt/validate to run on BOTH modules — `provision/aws` and
  `provision/gcp`; or loop over `provision/*/`). `scripts/lint.sh` likewise.
- **Runbook** `docs/deploy/provision-gcp.md`: the operator flow mirroring
  provision-aws.md — `terraform init/apply` → `terraform output -raw ssh_target`
  → `bootstrap-host.sh` → `deploy-agent.sh <blueprint>` → smoke → `terraform
  destroy`. Note it creates a real billable e2-small in project agent-keep-kn6r6i.

## Interface contracts

- **Consumes:** the existing bootstrap + deploy-agent (unchanged) and the uniform
  provisioner interface established by stage 20. **No new frozen contract** (ADR
  0007/0009) — output is an ssh target.

## Testing requirements

- `terraform -chdir=provision/gcp fmt -check -recursive`, `init -backend=false`,
  `validate` → clean/Success (credential-free; terraform 1.9.8 installed).
- `bash scripts/lint.sh` PASS (now covering both modules). No secrets / real
  tfvars / tfstate committed (grep + gitignore dry-run). deploy-agent/bootstrap
  untouched. Existing suites green.
- The LIVE proof (apply→bootstrap→deploy→smoke→destroy on real GCE in
  agent-keep-kn6r6i) is the Operator step — billable, not in CI.

## Acceptance conditions

- [ ] Kill-switch: N/A — operator provisioning tool; `terraform destroy` is the off
- [ ] Observably-works: `terraform validate` passes; runbook flow exact + complete
- [ ] No secrets/private keys/state/real-tfvars in git; x86_64 machine type; SSH
      ingress not 0.0.0.0/0; uniform interface matches provision/aws
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (live apply→deploy→destroy on real GCP is the Operator's proof)

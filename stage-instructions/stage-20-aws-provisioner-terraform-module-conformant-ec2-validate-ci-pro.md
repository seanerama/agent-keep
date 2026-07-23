# Stage 20: AWS provisioner: Terraform module (conformant EC2) + validate CI + provision-deploy runbook

- **Type:** feature
- **Depends on:** none

## Objectives

The walking skeleton of the per-cloud-provisioner phase (ADR 0009, issue #21): a
`provision/aws/` Terraform module that stands up a **conformant host** (Ubuntu +
Docker + systemd, SSH-reachable), which the EXISTING `bootstrap-host.sh` +
`deploy-agent.sh` then deploy unchanged. Proves "`terraform apply` → a conformant
target → deploy any blueprint" on a real cloud. AWS first (the only authed cloud);
GCP/Azure follow the identical module interface later.

## What to build

- **`provision/aws/` Terraform module** producing a conformant EC2 host:
  - A conformant **Ubuntu 24.04, x86_64** instance (ADR 0009: images are amd64 —
    NOT Graviton/arm64). Default a small size (e.g. `t3.small`); size is a variable.
  - A security group: **SSH (22) ingress locked to an `allowed_ssh_cidr` variable**
    (no `0.0.0.0/0` default — require the operator to set their IP/CIDR); egress
    open (image pulls + provider APIs). Dev-http stays loopback (reached by SSH
    tunnel per ADR 0007's flexible access) — do NOT open the dev-http ports publicly.
  - Registers the operator's EXISTING SSH **public** key (a variable — a path or
    the key material; NEVER a private key, never a secret in git).
  - **Uniform interface** (so GCP/Azure modules match): variables
    `ssh_public_key`, `instance_size`, `allowed_ssh_cidr`, `region`, `name` (+ an
    Ubuntu-24.04-x86_64 AMI lookup by owner/filter, not a hardcoded stale AMI id);
    outputs `ssh_target` (`ubuntu@<public-ip>`), `instance_id`, `public_ip`.
  - **Local, gitignored state.** Add `provision/**/.terraform/`,
    `*.tfstate*`, `*.tfvars` (real) to `.gitignore`; commit a
    `provision/aws/terraform.tfvars.example` documenting the variable shape. No
    remote backend (deferred).
- **Provision→deploy runbook** `docs/deploy/provision-aws.md`: the exact operator
  flow — `terraform init/apply` → capture `ssh_target` output → `bootstrap-host.sh
  <ssh_target>` → `deploy-agent.sh <blueprint> <ssh_target>` → smoke →
  `terraform destroy`. Note it creates a REAL billable EC2 instance (small,
  short-lived) and that `destroy` tears it down. Record the target + provisioning
  method in `.verity/deploy-access.md` (locations only).
- **CI-side validation** (Terraform can't reach real AWS in CI): add a lightweight
  CI step (or extend the lint job) running `terraform fmt -check` and `terraform
  validate` on `provision/aws/` (install a pinned Terraform version, mirroring the
  pinned-shellcheck approach from stage 16). No `plan`/`apply` in CI. If wiring
  Terraform into CI is heavy, at minimum a repo test asserting the module's
  variable/output contract (fmt + validate is preferred).

## Interface contracts

- **Consumes:** the existing `bootstrap-host.sh` + `deploy-agent.sh` (unchanged)
  and `DEPLOY_HOST` / `.verity/deploy-access.md` pattern. **No new frozen
  contract** (ADR 0007/0009) — the module's output is an ssh target. The
  variable/output interface here IS the template GCP/Azure modules must match.

## Testing requirements

- `terraform fmt -check` + `terraform validate` clean on `provision/aws/` (the
  CI-able proof). `scripts/lint.sh` / shellcheck clean on any new shell.
- No secrets committed (grep); `.gitignore` covers state + real tfvars; only the
  `.example` tfvars is committed.
- The LIVE proof (the walking skeleton) is the Operator step: `apply` a real EC2
  VM → bootstrap → `deploy-agent.sh <spec> <target>` → smoke green → `destroy`.
  Not runnable in CI (needs real AWS + billing).

## Acceptance conditions

- [ ] Kill-switch: N/A — an operator provisioning tool, not a runtime feature
      (recorded). It creates real cloud resources; `terraform destroy` is the off.
- [ ] Observably-works: `terraform validate` passes; the runbook's
      apply→bootstrap→deploy→smoke→destroy flow is exact and complete (Operator
      runs the live part)
- [ ] No secrets/private keys in git; state + real tfvars gitignored; x86_64
      instance (amd64 image constraint); SSH ingress not `0.0.0.0/0` by default
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (live apply→deploy→destroy on real AWS is the Operator's proof)

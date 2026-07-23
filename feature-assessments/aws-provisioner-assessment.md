# Assessment: per-cloud provisioner (Mode A) — the AWS-first thin backlog

- **Date:** 2026-07-23
- **Input:** ADR 0009 (Terraform modules → conformant host, AWS first), ADR 0007
  (conformant-host abstraction), `docs/provisioner-skeleton.md`, issue #21
- **Decision:** ACCEPT as Stage 20 (the AWS module = walking skeleton); DEFER
  GCP + Azure modules and the on-demand extras

## Claim / reality verification (against live code + tooling, 2026-07-23)

| Claim | Reality | Verdict |
| --- | --- | --- |
| The provisioner hands off to existing `deploy-agent.sh <blueprint> <target>` | usage confirms `<blueprint-spec> <target>` (ssh target / LOCAL) | ✅ holds |
| ...and `bootstrap-host.sh <ssh-target>` | usage confirms `<ssh-target>` | ✅ holds |
| Chassis images are amd64 (x86_64 instance constraint) | `keep_build/composer.py` sets no `--platform`; images build on amd64 CI + workstation → amd64 | ✅ holds — instance MUST be x86_64 |
| No `provision/` dir / terraform yet | absent; `terraform` not installed (operator setup) | ✅ holds |
| AWS authed for the live skeleton | `aws sts` → account 979096371461 | ✅ holds |

No false premises. The provisioner is genuinely thin — it only makes a conformant
VM exist; the proven deploy machinery does the rest.

## Why one stage (thin backlog)

- **Stage 20** is one cohesive deliverable: the `provision/aws/` module + its
  gitignore/state hygiene + the `terraform fmt/validate` CI gate + the
  provision→deploy runbook. Splitting the module from its runbook would fragment a
  single "working AWS provisioner" unit. The live apply→deploy→destroy is the
  Operator's step (like every deploy stage), not CI.
- The module's variable/output interface (ssh_public_key, instance_size,
  allowed_ssh_cidr, region, name → ssh_target/instance_id/public_ip) IS the
  template GCP/Azure reuse — established here, matched there.

## Contract safety

No new contract, no frozen contract touched (ADR 0007/0009). The provisioner's
output is an ssh target; it reuses `DEPLOY_HOST` + `.verity/deploy-access.md`.

## Deferred (per ADR 0009 — future stages / on demand, NOT in this thin backlog)

- **GCP module** and **Azure module** — each a later stage on Stage 20's
  variable/output interface; blocked on external prerequisites (`gcloud`/`az`
  install + cloud accounts — operator setup), so genuinely not startable now.
- Static egress IP / NAT (partner allowlisting); in-tenant serverless-GPU model
  endpoint (ADR 0008); remote Terraform backend (S3+lock); tailscale-in-cloud-init;
  auto-provision from inside `deploy-agent.sh`.

## Rejected

- Baking Terraform into the abstraction as required (ADR 0007/0009 keep
  provisioners optional; bring-your-own-host stays the baseline).
- Cloud-native container services as the substrate (ADR 0007 — boundary degrades).

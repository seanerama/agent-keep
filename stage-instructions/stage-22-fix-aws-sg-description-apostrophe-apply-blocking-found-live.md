# Stage 22: Fix AWS SG description apostrophe (apply-blocking, found live)

- **Type:** bug
- **Depends on:** none

## Objectives

Fix issue #45, found in the AWS live test: `provision/aws/main.tf` set the SG
ingress `description` to `"SSH from the operator's allowed CIDR only"`. AWS
restricts SG descriptions to `^[0-9A-Za-z_ .:/()#,@\[\]+=&;{}!$*-]*$` — the
apostrophe is disallowed, so `terraform apply` fails at the provider's plan-time
schema check (`ingress.0.description doesn't comply`). Nothing is created.

## What to build

- `provision/aws/main.tf`: remove the apostrophe from the ingress description
  (e.g. "SSH from the allowed operator CIDR only"). Add a short comment noting the
  AWS description charset so it isn't reintroduced.

## Interface contracts

- No contract/runtime change; a one-line Terraform description fix.

## Testing requirements

- `terraform -chdir=provision/aws fmt -check` + `validate` clean (they always
  were — this class isn't caught by validate).
- **The real proof: `terraform plan`** (needs AWS creds; the operator/live gate)
  no longer errors on the description — the plan produces "1 to add" cleanly.
- `bash scripts/lint.sh` PASS.

## Acceptance conditions

- [ ] Reproduction captured (issue #45 apply error) + the description is now
      within the AWS-allowed charset (no apostrophe)
- [ ] `terraform plan` on provision/aws succeeds past the SG description check
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO (terraform plan/apply needs AWS creds — operator/live)

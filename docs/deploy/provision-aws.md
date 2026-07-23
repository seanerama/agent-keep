# Operator runbook — provision an AWS host, then deploy a chassis onto it

> **Audience:** the Operator, from their workstation. This is the per-cloud
> provisioner walking skeleton (ADR 0009, issue #21): `terraform apply` stands up
> a **conformant host** (Ubuntu 24.04 + reachable over SSH), then the EXISTING,
> unchanged `bootstrap-host.sh` + `deploy-agent.sh` deploy the audited chassis
> onto it — exactly as they do for the NSAF dev server or any bring-your-own host.
>
> **This creates a REAL, BILLABLE EC2 instance** (a `t3.small` by default).
> Keep it short-lived: provision → bootstrap → deploy → smoke → **`terraform
> destroy`**. `destroy` is the off switch; run it as soon as you're done.

## What this is / is NOT

- The Terraform module (`provision/aws/`) makes a VM exist, SSH-reachable, with a
  security group that allows SSH from **your IP only** and open egress. That is
  its whole job (ADR 0009).
- It does NOT install Docker, the chassis, or the paired topology. Docker is
  installed by `bootstrap-host.sh`; the topology by `deploy.sh` (driven by
  `deploy-agent.sh`) — unchanged, on any Ubuntu+Docker VM.

## 0. Prerequisites (one-time)

- **AWS credentials** on your workstation with EC2 permissions (the `aws` CLI
  authed, or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_PROFILE` in the
  environment — Terraform's AWS provider reads the same chain). Confirm:
  `aws sts get-caller-identity`.
- **Terraform** installed (the module + CI are pinned to **1.9.8**;
  `terraform version`).
- **An SSH keypair** you already own. You register the **PUBLIC** key with the
  module; you keep the private key. Have `~/.ssh/id_ed25519.pub` (or your `.pub`)
  ready.
- Your workstation's **public IP** (for `allowed_ssh_cidr`): `curl -s ifconfig.me`.

## 1. Configure the module

```sh
cd provision/aws
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars     # real terraform.tfvars is gitignored — never committed
```

Set, at minimum:

- `ssh_public_key` — your PUBLIC key material (`ssh-ed25519 AAAA...`) OR a path to
  a `.pub` file (`~/.ssh/id_ed25519.pub`). **Public key only, never a private key.**
- `allowed_ssh_cidr` — **your** public IP as a `/32` (e.g. `203.0.113.4/32`). The
  module REJECTS `0.0.0.0/0`.
- Optionally `instance_size` (default `t3.small`, x86_64 only — ADR 0009),
  `region` (default `us-east-1`), `name` (default `agent-keep`).

## 2. Provision (apply) — creates the billable instance

```sh
cd provision/aws
terraform init
terraform apply            # review the plan, type `yes`
```

State is written locally (`terraform.tfstate`, gitignored — never commit it).
Capture the ssh target the deploy steps consume:

```sh
TARGET="$(terraform output -raw ssh_target)"   # e.g. ubuntu@54.x.x.x
echo "$TARGET"
```

> The dev-http ports (worker `8377`, mechanic `8477`) are **not** open publicly —
> they stay on host loopback. Reach them later via an SSH tunnel, e.g.
> `ssh -L 8377:127.0.0.1:8377 "$TARGET"`.

## 3. Bootstrap the host (fresh Ubuntu → deploy-ready)

The same command used for any host (docs/deploy/first-live-chassis.md §1). It
installs Docker + the scoped root helper/sudoers and runs the conformance check.
On a brand-new AWS instance the SSH user is `ubuntu`.

```sh
# From the repo root:
scripts/bootstrap-host.sh "$TARGET"
```

Notes for a fresh AWS VM:
- First connection: accept the host key. If your key isn't the default identity,
  add `-i <your-private-key>` via `~/.ssh/config` for the host.
- The default `ubuntu` user has passwordless sudo on Ubuntu AMIs, so the
  privileged install steps run without a password prompt (unlike a password-only
  host). On success it prints `HOST READY`. If Docker was just installed, the
  script adds `ubuntu` to the `docker` group — reconnect once so `docker info`
  works without sudo.

## 4. Deploy a chassis onto it

`deploy-agent.sh <blueprint-spec> <target>` builds the worker from the blueprint,
loads it, and stands up the worker+proxy+mechanic topology (ADR 0007 two-inputs
entry point). Pick a blueprint:

```sh
# A — keyless, no cloud egress: the static default chatbot (fast, no secret).
scripts/deploy-agent.sh specs/default-chatbot.yaml "$TARGET"

# B — a real provider, e.g. OpenAI (secret piped on stdin ONLY, never argv/log):
printf 'OPENAI_API_KEY=%s\n' "$OPENAI_API_KEY" | \
  KEEP_DEPLOY_SECRETS=1 \
  scripts/deploy-agent.sh specs/default-chatbot.openai.yaml "$TARGET"
```

## 5. Smoke (on the host, over the loopback surfaces)

The dev-http surfaces bind host loopback, so run the smokes on the host (or over
an SSH tunnel). Mirror docs/deploy/first-live-chassis.md §3:

```sh
ssh "$TARGET" 'cd /path/to/checkout && \
  scripts/smoke-chat.sh 127.0.0.1:8377 docker:agent-keep-default-chatbot'
ssh "$TARGET" 'cd /path/to/checkout && \
  scripts/smoke-egress.sh agent-keep-default-chatbot docker:agent-keep-default-chatbot-proxy'
ssh "$TARGET" 'cd /path/to/checkout && \
  scripts/smoke-mechanic.sh 127.0.0.1:8477 docker:agent-keep-default-chatbot-mechanic'
```

All three print `SMOKE PASS`. (If the repo isn't checked out on the host,
`scp scripts/*.sh` there first.)

## 6. Destroy — the off switch (do this when done)

```sh
cd provision/aws
terraform destroy          # review, type `yes` — tears down the instance, SG, key pair
```

Confirm nothing lingers billing you (the EC2 instance is the cost). `destroy`
leaves your local state file empty; the `.tfvars`/state stay gitignored.

## 7. Record the target (locations only, no secrets)

Add the ephemeral target + provisioning method to `.verity/deploy-access.md`
(gitignored, shared out-of-band — never commit host/IP detail). Record: that this
target was **provisioned via `provision/aws/` (`terraform apply`)**, the region,
and that the ssh target comes from `terraform output -raw ssh_target` — **not** the
raw IP or any secret. Because AWS targets are ephemeral, note the target is only
valid between `apply` and `destroy`.

## Cost & safety recap

- Real, billable `t3.small` + a gp3 root volume — pennies per hour, but **destroy
  it** when done.
- SSH is open to your `/32` only; dev-http is loopback-only (SSH-tunnel to reach).
- No secrets in git: `terraform.tfvars` and `*.tfstate*` are gitignored; only the
  `.example` is committed. The provider key (if any) is injected on-host at deploy
  by `deploy-agent.sh`/`deploy.sh`, never here.

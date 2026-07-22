# Operator runbook — first live chassis on the NSAF dev server

> **Audience:** the Operator, from their workstation. This is the ONLY place the
> live deploy happens — CI cannot (the NSAF dev server is on the tailnet, ADR
> 0004). Everything below runs on the operator's machine + over SSH.
>
> **What CI already proved (so you don't re-prove it):** shellcheck on the
> deploy scripts, the systemd unit renders into the hardened paired topology
> (`tests/deploy/test_systemd_render.py`), and the FULL four-container topology
> stands up locally and passes all three smokes against the STATIC image
> (`tests/integration/test_paired_topology.py`). What CI **cannot** prove, and
> what you prove here, is the egress ALLOW path against the **real Anthropic
> API**, every direction audited, over the tailnet.

## 0. The go-live truth (read this first)

The published image `ghcr.io/seanerama/agent-keep-default-chatbot` is baked
**static-only** (CI builds it from `specs/default-chatbot.yaml`, `provider:
static`). A static worker makes **no** outbound call, so it can prove the pipe
(message -> model_call -> audited reply) and the egress **DENY** path, but it can
**never** produce a real Anthropic reply or an egress **ALLOW** record for
`api.anthropic.com`. Those are the heart of the Stage-5 acceptance.

Therefore the first live chassis is deployed in **two steps**:

- **Step A — prove the pipe + the DENY boundary** with the published static
  image. Fast, keyless, low-risk. Satisfies healthz, `smoke-chat` (scripted
  reply + token-accounted `model_call` audit line), `smoke-egress` (a
  non-allowlisted host denied + audited **live**), and `smoke-mechanic` (cited
  answer). It does NOT satisfy the real-Anthropic-reply / egress-ALLOW
  conditions.
- **Step B — go live for real** by building the **anthropic** worker variant
  (`specs/default-chatbot.live.yaml`, `provider: anthropic`) and redeploying it.
  This is the reviewed spec edit `specs/default-chatbot.yaml` always described.
  It is what makes `smoke-chat` a **real Anthropic reply through the proxy** and
  puts an `egress` **ALLOW** record for `api.anthropic.com:443` in the proxy's
  audit log. **Requires your Anthropic API key** (the one operator secret).

Both steps use the same slug (`default-chatbot`), the same unit, the same ports.
Step B just swaps the worker image for one that actually calls the model.

## 1. Preflight (once per host, and after any change to the helper/unit)

Values come from `.verity/deploy-access.md` (gitignored, shared out-of-band —
ADR 0004 keeps access locations out of git). For the NSAF dev server the ssh
target and its tailnet address live in that file; the host is reachable only on
the tailnet.

```sh
export DEPLOY_HOST=<ssh-target>      # from .verity/deploy-access.md

# You must be on the tailnet:
ssh "$DEPLOY_HOST" 'docker info >/dev/null && echo docker-ok'

# One-time bootstrap of the scoped root helper + sudoers (re-run whenever
# deploy/agent-keep-deploy or deploy/sudoers-agent-keep changes in-repo):
scp deploy/agent-keep-deploy deploy/sudoers-agent-keep "$DEPLOY_HOST":/tmp/
ssh -t "$DEPLOY_HOST" 'sudo install -o root -g root -m 0755 /tmp/agent-keep-deploy \
    /usr/local/sbin/agent-keep-deploy && sudo install -o root -g root -m 0440 \
    /tmp/sudoers-agent-keep /etc/sudoers.d/agent-keep && sudo visudo -c'
# Then edit /etc/sudoers.d/agent-keep: replace <deploy-user> with `smahoney`
# (the login that runs deploy.sh), and re-run `sudo visudo -c`.
```

## 2. GHCR login on the host (so `docker pull` can fetch the images)

The three images are on ghcr under `seanerama`. On the host:

```sh
# Uses a GitHub PAT with read:packages. Locations per .verity/deploy-access.md;
# NEVER paste the token into git or this file.
ssh "$DEPLOY_HOST" 'echo "$GHCR_PAT" | docker login ghcr.io -u seanerama --password-stdin'
```

## 3. Step A — deploy the static image (prove the pipe + DENY)

```sh
DEPLOY_HOST=<ssh-target> ./deploy.sh default-chatbot edge
```

`deploy.sh` ships the unit, the read-only spec and the ingress relay, (re)creates
the Stage-4 bundle dir, pulls + **digest-pins** all three images, writes
`/etc/agent-keep/default-chatbot.env` (root:0600), starts
`agent-keep@default-chatbot`, and verifies `/healthz` for the worker **and** the
mechanic from the host. Ports (Foundry scheme): worker `127.0.0.1:8377`, mechanic
`127.0.0.1:8477`, proxy internal-only.

### Live smokes for Step A (run on the host, over SSH)

The dev-http surfaces bind host loopback, so run the smokes **on the host**:

```sh
# 1) chat: healthz + non-empty reply + a NEW run-correlated model_call audit line
ssh "$DEPLOY_HOST" 'cd /path/to/checkout && \
  scripts/smoke-chat.sh 127.0.0.1:8377 docker:agent-keep-default-chatbot'

# 2) egress: a non-allowlisted host is REFUSED and a denied `egress` record
#    lands in the PROXY's own audit log — the boundary, LIVE
ssh "$DEPLOY_HOST" 'cd /path/to/checkout && \
  scripts/smoke-egress.sh agent-keep-default-chatbot docker:agent-keep-default-chatbot-proxy'

# 3) mechanic: a cited answer + the mechanic's OWN run-correlated audit line
ssh "$DEPLOY_HOST" 'cd /path/to/checkout && \
  scripts/smoke-mechanic.sh 127.0.0.1:8477 docker:agent-keep-default-chatbot-mechanic'
```

(If the repo isn't checked out on the host, `scp scripts/*.sh` there first, or
run each script's body via `ssh`.) All three must print `SMOKE PASS`.

## 4. Step B — go live for real (a non-static provider)

This is the step that satisfies the full Stage-5 acceptance (a real model reply
through the proxy + a live egress ALLOW record). The provider is **your choice** —
the chassis is agnostic (issue #15). Today the built provider adapters are
`static` (CI) and `anthropic`; more (OpenAI, Google, Ollama) land as they are
implemented against the `ModelProvider` seam. The example below uses the
`anthropic` variant, so **you need an Anthropic API key**; swap the spec + the
secret VAR name for another provider, or use a local Ollama with **no secret at
all**.

### 4a. Build + push the live worker image (on your workstation)

```sh
# Build the live variant (specs/default-chatbot.live.yaml → provider: anthropic;
# same egress allowlist). For another provider, point at that provider's spec.
uv run keep-build build specs/default-chatbot.live.yaml \
  --tag ghcr.io/seanerama/agent-keep-default-chatbot:live
docker push ghcr.io/seanerama/agent-keep-default-chatbot:live   # ghcr write as seanerama
```

### 4b. Deploy the live tag WITH the secret, in one command

`deploy.sh` injects the secret into the env file (root:0600) **before** the
worker starts — a key-requiring provider crashes at boot otherwise — when you set
`KEEP_DEPLOY_SECRETS=1` and pipe `VAR=value` line(s) on stdin. The value travels
on stdin only (never argv/log), through the scoped helper. Provider-agnostic:
pipe whatever your provider needs, several vars, or nothing.

```sh
# Anthropic example — one command brings the live worker up healthy:
printf 'ANTHROPIC_API_KEY=%s\n' "$THE_KEY" | \
  KEEP_DEPLOY_SECRETS=1 \
  KEEP_SPEC_FILE=specs/default-chatbot.live.yaml \
  DEPLOY_HOST=<ssh-target> \
  ./deploy.sh default-chatbot live

# OpenAI:  printf 'OPENAI_API_KEY=%s\n' "$KEY" | KEEP_DEPLOY_SECRETS=1 ... ./deploy.sh default-chatbot live
# Google:  printf 'GOOGLE_API_KEY=%s\n' "$KEY" | KEEP_DEPLOY_SECRETS=1 ... ./deploy.sh default-chatbot live
# Ollama (local, no cloud egress, no secret): omit KEEP_DEPLOY_SECRETS entirely.
```

`KEEP_SPEC_FILE` makes the proxy mount + the mechanic's bundle copy the **live**
spec (its allowlist is byte-identical to the static one, so the boundary is
unchanged). `deploy.sh` re-pins to the `:live` digest, injects the secret before
start, and its liveness gate verifies the worker + proxy came up.

> The secret VALUE lives ONLY in `/etc/agent-keep/default-chatbot.env`
> (root:0600) on the host — never in git, an image, or this file (agent-spec rule
> 3). `write-env` overwrites on each deploy, so **re-supply the secret on every
> redeploy** (explicit provenance; nothing secret persists in the tracked flow).
> Confirm perms: `ssh "$DEPLOY_HOST" 'sudo stat -c "%a %U:%G"
> /etc/agent-keep/default-chatbot.env'` must print `600 root:root`.

### 4c. Live smokes for Step B (the real acceptance)

Re-run the three smokes from step 3. Now `smoke-chat` returns a **real model
reply** (not the scripted static line), routed **through the proxy**, with an
egress ALLOW record for the provider's host.

## 5. Acceptance checklist (the Stage-5 gate)

- [ ] **Real Anthropic reply THROUGH the proxy** — `smoke-chat.sh` (Step B)
      returns a genuine model reply; the worker had `HTTP(S)_PROXY -> egress-proxy`
      and no other route.
- [ ] **A non-allowlisted host DENIED and audited, LIVE** — `smoke-egress.sh`
      prints `SMOKE PASS`; a denied `egress` record for the smoke host is in the
      proxy's `egress-audit.jsonl`.
- [ ] **Mechanic cites** — `smoke-mechanic.sh` prints `SMOKE PASS` (reply carries
      an `audit_record` citation marker).
- [ ] **Audit log shows the full run** — in the worker bundle audit
      (`/var/lib/agent-keep/default-chatbot/bundle/default-chatbot.audit.jsonl` on
      the host): an inbound message, a `model_call`, token accounting for the run;
      in the proxy audit (`docker exec agent-keep-default-chatbot-proxy cat
      /var/lib/agent-keep/egress-audit.jsonl`): an `egress` **ALLOW** record for
      `api.anthropic.com:443`.
- [ ] **Env file root:0600** — `stat -c "%a %U:%G"` on the env file is
      `600 root:root`.
- [ ] **Rollback exercised once** — see below.

### Inspecting the audit planes on the host

```sh
# worker audit (persisted in the bundle on the host):
ssh "$DEPLOY_HOST" 'sudo tail -n 20 \
  /var/lib/agent-keep/default-chatbot/bundle/default-chatbot.audit.jsonl'
# proxy egress audit (ALLOW for api.anthropic.com + the DENY from smoke-egress):
ssh "$DEPLOY_HOST" 'docker exec agent-keep-default-chatbot-proxy \
  cat /var/lib/agent-keep/egress-audit.jsonl'
```

## 6. Rollback (exercise once, then keep as the recovery path)

Rollback is **re-run the previous worker tag**. `deploy.sh` re-pins to that tag's
digest and the unit recreates the topology. The helper backed up the previous env
file (`<slug>.env.bak.<timestamp>`).

```sh
# e.g. roll Step B (:live) back to the static :edge you ran in Step A:
DEPLOY_HOST=<ssh-target> ./deploy.sh default-chatbot edge
# re-run smoke-chat to confirm the previous image serves again.
```

## 7. Off / dark-launch

There is no runtime kill-switch by design (the chassis has no feature flags). To
take a chassis **off**:

```sh
ssh "$DEPLOY_HOST" 'sudo -n /usr/local/sbin/agent-keep-deploy service default-chatbot stop'
# fully remove: also `systemctl disable agent-keep@default-chatbot`
```

"Off" IS the unit not running; a published image nobody runs is the dark-launch
state.

## Live-smoke result (paste into the Stage-5 PR/issue)

The Operator pastes the three `SMOKE PASS` blocks + the two audit-tail excerpts
(worker `model_call` + token accounting, proxy `egress` ALLOW for
`api.anthropic.com`) and the `stat` line here / in the PR — that transcript is the
acceptance evidence CI structurally cannot produce.

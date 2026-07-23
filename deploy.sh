#!/usr/bin/env bash
# Agent Keep — deploy a paired chassis (worker + egress-proxy + mechanic) to ANY
# host you can reach over SSH that runs Docker + systemd (ADR 0004). Re-namespaced
# + extended from the Foundry's deploy.sh (ADR 0001 transplant): that script
# shipped ONE hardened container; a Keep chassis is the Stage-3/Stage-4 paired
# topology, stood up by ONE systemd unit (deploy/systemd/agent-keep@.service).
#
# Nothing about the target is hardcoded: the host comes from the DEPLOY_HOST
# environment variable (from your gitignored .verity/deploy-access.md), so this
# tracked script runs against the NSAF dev server, a VM, or a laptop without edits.
#
# Usage:
#   DEPLOY_HOST=user@host ./deploy.sh <slug> <worker-version-tag>
#     e.g. DEPLOY_HOST=<ssh-target> ./deploy.sh default-chatbot edge
#
# DEPLOY_HOST may be a `user@host` pair or a ~/.ssh/config alias.
#
# Image tags:
#   <worker-version-tag>   the tag for the WORKER image, pulled then digest-pinned.
#                          For the published STATIC image use `edge` or a git sha.
#                          For a LIVE anthropic worker, build+push that image to
#                          ghcr under a tag (e.g. `live`) and pass it here — see
#                          docs/deploy/first-live-chassis.md.
#   KEEP_PROXY_VERSION     tag for the generic egress-proxy image (default `edge`).
#   KEEP_MECHANIC_VERSION  tag for the generic mechanic image  (default `edge`).
#   KEEP_SPEC_FILE         the spec.yaml the worker was baked from (default
#                          specs/<slug>.yaml). It is shipped read-only to the host
#                          for BOTH the proxy's allowlist mount AND the mechanic's
#                          bundle copy. For the live worker set
#                          KEEP_SPEC_FILE=specs/default-chatbot.anthropic.yaml.
#   KEEP_WORKER_LOCAL_IMAGE=1  LOCAL-IMAGE MODE (ADR 0007 arbitrary-blueprint
#                          path): deploy ANY blueprint WITHOUT a registry write.
#                          Instead of pulling the worker tag from ghcr, BUILD the
#                          worker image locally from KEEP_SPEC_FILE and stream it
#                          to the host (docker save | ssh docker load — no file, no
#                          registry), then pin the LOADED image by its immutable ID
#                          (a loaded image has NO RepoDigests to pin). The proxy +
#                          mechanic STILL pull their generic published :edge images
#                          — only the WORKER changes SOURCE (load vs pull). Off by
#                          default: the registry-pull path below is unchanged.
#
# Optional:
#   BIND_HOST   interface both dev-http surfaces publish on (default 127.0.0.1 =
#               reachable only from the host itself; correct for the tailnet dev
#               chassis — a public route is a later ADR).
#
# Privilege model: all root steps go through the scoped helper
# /usr/local/sbin/agent-keep-deploy (deploy/agent-keep-deploy), whitelisted via
# /etc/sudoers.d/agent-keep. One-time bootstrap (re-run whenever the helper or
# sudoers file changes in-repo):
#   scp deploy/agent-keep-deploy deploy/sudoers-agent-keep <host>:/tmp/
#   ssh -t <host> 'sudo install -o root -g root -m 0755 /tmp/agent-keep-deploy \
#       /usr/local/sbin/agent-keep-deploy && sudo install -o root -g root -m 0440 \
#       /tmp/sudoers-agent-keep /etc/sudoers.d/agent-keep && sudo visudo -c'
#   (edit /etc/sudoers.d/agent-keep's <deploy-user> to the login that runs this.)
#
# What it does (idempotent):
#   1. Pre-flight: host reachable, docker present, helper installed.
#   2. Install/refresh the systemd template (agent-keep@.service).
#   3. Ship the read-only spec.yaml; (re)create the Stage-4 bundle dir.
#   4. Pull each image BY TAG, resolve immutable digests on the host, pin them.
#   5. Write /etc/agent-keep/<slug>.env (digest-pinned deploy vars). The operator
#      appends ANTHROPIC_API_KEY (the ONE secret VALUE) for the live variant.
#   6. enable + restart agent-keep@<slug>.
#   7. Verify /healthz for worker AND mechanic from the host over ssh.
# Rollback: re-run with the previous <worker-version-tag> (env backups kept).
# The full LIVE smoke (real Anthropic reply through the proxy, live audited
# denial, mechanic cite) is the Operator's, over the tailnet — see the runbook.
# Every `ssh "$HOST" "... $HELPER ... ${SLUG} ..."` below INTENTIONALLY expands
# client-side: the helper path is a fixed constant, and SLUG/ports/BIND_HOST are
# client-known and already validated against SLUG_RE. That is exactly what we
# want sent to the host — so SC2029 (client-side expansion) is disabled file-wide.
# (Directive must precede the first command to be file-scoped.)
# shellcheck disable=SC2029
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: DEPLOY_HOST=user@host ./deploy.sh <slug> <worker-version-tag>" >&2
  echo "  e.g. DEPLOY_HOST=<ssh-target> ./deploy.sh default-chatbot edge" >&2
  exit 64
fi
SLUG="$1"
VERSION="$2"

# Provider-agnostic deploy-time secrets (stage 7). A live worker builds its
# provider EAGERLY at boot and refuses to start without its secret
# (anthropic_provider.py; runner.py). So any secret the chosen provider needs
# must be in the env file BEFORE the worker starts — not appended after it has
# already crashed. When KEEP_DEPLOY_SECRETS=1, read `VAR=value` lines from stdin
# ONCE, up front (before any ssh consumes it), and inject them below (§ secrets)
# right after write-env and before the worker is started. The values are
# arbitrary: ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, several, or NONE
# (a local Ollama needs no secret — then just omit the flag). They travel on
# stdin only, never argv/log, and land in the root:0600 host env file via the
# scoped helper's append-env. NEVER echo DEPLOY_SECRETS.
DEPLOY_SECRETS=""
if [ "${KEEP_DEPLOY_SECRETS:-}" = "1" ]; then
  DEPLOY_SECRETS="$(cat)"
  if [ -z "$DEPLOY_SECRETS" ]; then
    printf '%s\n' "KEEP_DEPLOY_SECRETS=1 but stdin was empty — pipe VAR=value line(s), e.g." >&2
    printf '  %s\n' "printf 'ANTHROPIC_API_KEY=%s\\n' \"\$KEY\" | KEEP_DEPLOY_SECRETS=1 ./deploy.sh ${SLUG} ${VERSION}" >&2
    exit 64
  fi
  # Per-line validation (stage 24, live finding F1): every non-empty line must
  # be VAR=value — a valid env var name AND a non-empty value. An empty-valued
  # or malformed line otherwise lands verbatim in the root:0600 host env file
  # (append-env is a bare cat >>) and the worker crash-loops at boot. Fail fast
  # HERE, before any build/ssh/remote action. NEVER echo the value or the raw
  # line — the no-secrets-in-logs discipline (Stage 7) applies to error paths.
  secret_line_no=0
  while IFS= read -r secret_line; do
    secret_line_no=$((secret_line_no + 1))
    [ -n "$secret_line" ] || continue
    if [[ "$secret_line" =~ ^[A-Za-z_][A-Za-z0-9_]*=.+$ ]]; then
      continue
    fi
    if [[ "$secret_line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=$ ]]; then
      printf '%s\n' "invalid secret line ${secret_line_no}: ${BASH_REMATCH[1]} has an EMPTY value" >&2
    else
      printf '%s\n' "invalid secret line ${secret_line_no}: malformed — expected VAR=value (valid env var name, non-empty value)" >&2
    fi
    printf '%s\n' "pipe VAR=value line(s), e.g." >&2
    printf '  %s\n' "printf 'ANTHROPIC_API_KEY=%s\\n' \"\$KEY\" | KEEP_DEPLOY_SECRETS=1 ./deploy.sh ${SLUG} ${VERSION}" >&2
    exit 64
  done <<< "$DEPLOY_SECRETS"
  unset secret_line secret_line_no
fi

# Slug validation MIRRORS the root helper's (deploy/agent-keep-deploy): strict
# [a-z0-9] with single interior hyphens. Reject early client-side (defence in
# depth) so a bad slug never reaches the host.
SLUG_RE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
if ! [[ "$SLUG" =~ $SLUG_RE ]]; then
  echo "invalid slug: '${SLUG}' — must match ${SLUG_RE}" >&2
  exit 64
fi

# Deploy target — supplied via environment, NEVER hardcoded in this tracked
# script. Real per-host values live in .verity/deploy-access.md (gitignored).
# FAIL CLOSED with a clear message when unset (it only exists on the operator's
# machine — CI never sets it, and must not).
HOST="${DEPLOY_HOST:?set DEPLOY_HOST (ssh target, e.g. an ssh alias or user@host) — see .verity/deploy-access.md}"
OWNER="${AGENT_KEEP_OWNER:-seanerama}"   # ghcr namespace; override for a fork

WORKER_IMG="ghcr.io/${OWNER}/agent-keep-${SLUG}"
PROXY_IMG="ghcr.io/${OWNER}/agent-keep-egress-proxy"
MECHANIC_IMG="ghcr.io/${OWNER}/agent-keep-mechanic"
PROXY_VERSION="${KEEP_PROXY_VERSION:-edge}"
MECHANIC_VERSION="${KEEP_MECHANIC_VERSION:-edge}"

# Local-image mode (ADR 0007 arbitrary-blueprint path): build the worker locally
# and load it to the host instead of pulling it from a registry. Opt-in, off by
# default so the CI-published pull path stays UNCHANGED. See the header.
WORKER_LOCAL_IMAGE="${KEEP_WORKER_LOCAL_IMAGE:-}"

# The spec.yaml the worker image was baked from — shipped read-only to the host
# for the proxy's allowlist mount AND the mechanic's bundle copy.
SPEC_FILE="${KEEP_SPEC_FILE:-specs/${SLUG}.yaml}"
if [ ! -f "$SPEC_FILE" ]; then
  echo "spec file not found: ${SPEC_FILE} (set KEEP_SPEC_FILE)" >&2
  exit 66
fi

BIND_HOST="${BIND_HOST:-127.0.0.1}"
UNIT_TEMPLATE="agent-keep@.service"
HELPER="/usr/local/sbin/agent-keep-deploy"

# ── Per-slug ports — static, hand-curated map (collision-safe by construction) ─
# One static map is the authority: no two chassis share a host port. A paired
# chassis publishes TWO surfaces — the worker dev-http and the mechanic owner
# console — so each slug reserves a worker port; the mechanic sits at +100 in the
# same block. The proxy publishes NO host port (internal only). Following the
# Foundry's scheme (skeleton was 8377), the default chatbot's worker is 8377.
# Add a new co-located chassis HERE before deploying it.
declare -A WORKER_PORTS=(
  [default-chatbot]=8377
)
if [ -z "${WORKER_PORTS[$SLUG]:-}" ]; then
  echo "slug '${SLUG}' has no static port assignment — add it to WORKER_PORTS in deploy.sh" >&2
  echo "before co-locating it with another chassis (collision-safe by construction)." >&2
  exit 65
fi
WORKER_BIND_PORT="${WORKER_PORTS[$SLUG]}"
MECHANIC_BIND_PORT=$(( WORKER_BIND_PORT + 100 ))

# Worker sqlite session store — inside the container on the /tmp tmpfs (ephemeral
# by design; the audit log is what persists, via the Stage-4 bundle bind).
SQLITE_PATH="/tmp/agent-keep-sessions.sqlite3"

echo "==> deploying chassis slug=${SLUG} worker-version=${VERSION}"
echo "    worker=${WORKER_IMG}:${VERSION}"
echo "    proxy=${PROXY_IMG}:${PROXY_VERSION}  mechanic=${MECHANIC_IMG}:${MECHANIC_VERSION}"
echo "    spec=${SPEC_FILE}  bind=${BIND_HOST} worker:${WORKER_BIND_PORT} mechanic:${MECHANIC_BIND_PORT}"

echo "==> pre-flight: $HOST"
ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" \
  "docker info >/dev/null; sudo -n $HELPER preflight-check >/dev/null 2>&1 || { echo 'helper/sudoers not bootstrapped (see deploy.sh header)'; exit 9; }; echo host-ok"

echo "==> ship unit template (via scoped root helper)"
scp -q "deploy/systemd/${UNIT_TEMPLATE}" "$HOST":"/tmp/${UNIT_TEMPLATE}"
ssh "$HOST" "sudo -n $HELPER install-unit /tmp/${UNIT_TEMPLATE} && rm -f /tmp/${UNIT_TEMPLATE}"

echo "==> ship read-only spec + ingress relay + (re)create Stage-4 bundle dir"
scp -q "$SPEC_FILE" "$HOST":"/tmp/agent-keep-${SLUG}-spec.yaml"
scp -q "deploy/ingress-forward.py" "$HOST":"/tmp/agent-keep-${SLUG}-ingress.py"
ssh "$HOST" "sudo -n $HELPER install-spec ${SLUG} /tmp/agent-keep-${SLUG}-spec.yaml && rm -f /tmp/agent-keep-${SLUG}-spec.yaml && sudo -n $HELPER install-ingress ${SLUG} /tmp/agent-keep-${SLUG}-ingress.py && rm -f /tmp/agent-keep-${SLUG}-ingress.py && sudo -n $HELPER prep-bundle ${SLUG}"

echo "==> pull each image by tag, resolve digests on the host"
pin() {
  # pin <image> <tag> -> prints the RepoDigest (image@sha256:...) resolved on the host.
  ssh "$HOST" "docker pull -q '$1:$2' >/dev/null && docker inspect --format '{{index .RepoDigests 0}}' '$1:$2'"
}
# The proxy + mechanic are GENERIC, registry-published images (:edge): they always
# pull+pin by RepoDigest, in BOTH modes. Only the WORKER's SOURCE varies.
PROXY_REF="$(pin "$PROXY_IMG" "$PROXY_VERSION")"
MECHANIC_REF="$(pin "$MECHANIC_IMG" "$MECHANIC_VERSION")"
if [ "$WORKER_LOCAL_IMAGE" = "1" ]; then
  # LOCAL-IMAGE MODE (ADR 0007): build the worker from the spec locally, stream it
  # to the host (docker save | ssh docker load — no registry write), and pin the
  # LOADED image by its immutable ID. scripts/deliver-worker-image.sh does the
  # build+deliver+pin and prints ONLY the id (sha256:...) on stdout. NO worker
  # `docker pull` runs — the whole point of the no-ghcr-write path.
  echo "==> local-image mode: build worker from ${SPEC_FILE} and load it to ${HOST} (no registry)"
  WORKER_REF="$(scripts/deliver-worker-image.sh "$SPEC_FILE" "${WORKER_IMG}:${VERSION}" "$HOST")"
  if [ -z "$WORKER_REF" ]; then
    echo "==> DEPLOY FAILED: local worker image build/deliver produced no image id." >&2
    exit 1
  fi
else
  # REGISTRY-PULL MODE (unchanged): pull the published worker tag, pin by RepoDigest.
  WORKER_REF="$(pin "$WORKER_IMG" "$VERSION")"
fi
echo "    worker  -> ${WORKER_REF}"
echo "    proxy   -> ${PROXY_REF}"
echo "    mechanic-> ${MECHANIC_REF}"

echo "==> write digest-pinned env (helper backs up the previous)"
# Digest-pinned DEPLOY vars only. Provider secret VALUES (e.g. ANTHROPIC_API_KEY,
# OPENAI_API_KEY, GOOGLE_API_KEY — or none for a local provider) are injected
# below the § secrets step from KEEP_DEPLOY_SECRETS stdin, BEFORE the worker
# starts, and land here on the host (root:0600) via the helper's append-env.
# The KEEP_EGRESS_* tunables are written empty so the unit's `-e VAR` pass-through
# hands the container nothing and the runner defaults apply (empty => default,
# issue #13) unless the operator sets them. KEEP_EGRESS_HOST is the exception: the
# unit PINS it to `egress-proxy` literally (bind the internal interface only,
# issue #11), so the blank value here is informational, not consulted.
printf '%s\n' \
  "# written by deploy.sh — digest-pinned deploy vars, do not hand-edit" \
  "# (provider secret VALUES are appended below this block by deploy.sh's secrets step)" \
  "WORKER_IMAGE_REF=${WORKER_REF}" \
  "PROXY_IMAGE_REF=${PROXY_REF}" \
  "MECHANIC_IMAGE_REF=${MECHANIC_REF}" \
  "WORKER_IMAGE_DIGEST=${WORKER_REF#*@}" \
  "MECHANIC_IMAGE_DIGEST=${MECHANIC_REF#*@}" \
  "BIND_HOST=${BIND_HOST}" \
  "WORKER_BIND_PORT=${WORKER_BIND_PORT}" \
  "MECHANIC_BIND_PORT=${MECHANIC_BIND_PORT}" \
  "SQLITE_PATH=${SQLITE_PATH}" \
  "KEEP_EGRESS_HOST=" \
  "KEEP_EGRESS_PORT=" \
  "KEEP_EGRESS_HEAD_TIMEOUT_SECONDS=" \
  "KEEP_EGRESS_MAX_CONNECTIONS=" \
  | ssh "$HOST" "sudo -n $HELPER write-env ${SLUG}"

# § secrets: inject provider secret VALUE(s) into the env file BEFORE the worker
# starts (a key-requiring provider crashes at boot otherwise). Provider-agnostic:
# whatever VAR=value lines the operator piped in. stdin-only to the helper — the
# values never reach argv or any log; DEPLOY_SECRETS is never echoed.
if [ -n "$DEPLOY_SECRETS" ]; then
  echo "==> inject deploy-time provider secret(s) into the env file (root:0600, before start)"
  printf '%s\n' "$DEPLOY_SECRETS" | ssh "$HOST" "sudo -n $HELPER append-env ${SLUG}"
fi

ssh "$HOST" "sudo -n $HELPER service ${SLUG} enable-now && sudo -n $HELPER service ${SLUG} restart"

echo "==> verify liveness (from the host itself, over ssh)"
# Verify FROM THE HOST: BIND_HOST is a local interface on the target (loopback by
# default), reachable from the host but generally NOT from the operator's machine.
sleep 5
# issue #13 Defect 2: the egress-proxy is launched fire-and-forget by the unit's
# `ExecStartPre=docker run -d` (returns 0 on detach). If it then exits — e.g. the
# empty-KEEP_EGRESS_PORT boot crash — the old verify step never noticed, because
# it only curled worker + mechanic /healthz. Assert the proxy container is
# actually RUNNING (State.Running == true) FIRST: a dead audited security boundary
# must FAIL the deploy loudly, never read as "verified live". The liveness logic
# lives in scripts/assert-proxy-running.sh (unit-tested with a stubbed docker);
# pipe it over ssh so `docker inspect` sees the host daemon.
PROXY_CONTAINER="agent-keep-${SLUG}-proxy"
if ! ssh "$HOST" "bash -s -- ${PROXY_CONTAINER}" < scripts/assert-proxy-running.sh; then
  echo "==> DEPLOY FAILED: egress-proxy ${PROXY_CONTAINER} is not running." >&2
  echo "    The proxy is the audited egress boundary; a chassis with it down is NOT live." >&2
  echo "    Inspect it on the host:  ssh ${HOST} docker logs ${PROXY_CONTAINER}" >&2
  exit 1
fi
echo "    egress-proxy     : running (audited boundary up)"
WORKER_HEALTH=$(ssh "$HOST" "curl -fsS --max-time 10 'http://${BIND_HOST}:${WORKER_BIND_PORT}/healthz'")
echo "    worker  /healthz: ${WORKER_HEALTH}"
MECHANIC_HEALTH=$(ssh "$HOST" "curl -fsS --max-time 10 'http://${BIND_HOST}:${MECHANIC_BIND_PORT}/healthz'")
echo "    mechanic/healthz: ${MECHANIC_HEALTH}"
echo "==> deploy verified live: chassis ${SLUG} (worker ${VERSION}) is up"
echo "    worker  dev-http : ${BIND_HOST}:${WORKER_BIND_PORT}"
echo "    mechanic console : ${BIND_HOST}:${MECHANIC_BIND_PORT}"
echo "    egress-proxy     : internal only (no host port)"
echo
echo "Next — the LIVE smoke (Operator, over the tailnet). See"
echo "docs/deploy/first-live-chassis.md for the exact commands:"
echo "  ssh ${HOST} scripts/smoke-chat.sh    ${BIND_HOST}:${WORKER_BIND_PORT}   docker:agent-keep-${SLUG}"
echo "  ssh ${HOST} scripts/smoke-egress.sh  agent-keep-${SLUG}                 docker:agent-keep-${SLUG}-proxy"
echo "  ssh ${HOST} scripts/smoke-mechanic.sh ${BIND_HOST}:${MECHANIC_BIND_PORT} docker:agent-keep-${SLUG}-mechanic"

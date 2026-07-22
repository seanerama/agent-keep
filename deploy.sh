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
#                          KEEP_SPEC_FILE=specs/default-chatbot.live.yaml.
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
WORKER_REF="$(pin "$WORKER_IMG" "$VERSION")"
PROXY_REF="$(pin "$PROXY_IMG" "$PROXY_VERSION")"
MECHANIC_REF="$(pin "$MECHANIC_IMG" "$MECHANIC_VERSION")"
echo "    worker  -> ${WORKER_REF}"
echo "    proxy   -> ${PROXY_REF}"
echo "    mechanic-> ${MECHANIC_REF}"

echo "==> write digest-pinned env (helper backs up the previous)"
# Digest-pinned DEPLOY vars only. The operator appends the ANTHROPIC_API_KEY
# secret VALUE below this block on the host (root:0600) for the live variant.
# KEEP_EGRESS_HOST/PORT are written empty so the unit's `-e VAR` pass-through
# hands the container nothing (runner defaults apply) unless the operator sets them.
printf '%s\n' \
  "# written by deploy.sh — digest-pinned deploy vars, do not hand-edit" \
  "# (append the ANTHROPIC_API_KEY secret VALUE below this block for the live variant)" \
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
  | ssh "$HOST" "sudo -n $HELPER write-env ${SLUG}"

ssh "$HOST" "sudo -n $HELPER service ${SLUG} enable-now && sudo -n $HELPER service ${SLUG} restart"

echo "==> verify liveness (from the host itself, over ssh)"
# Verify FROM THE HOST: BIND_HOST is a local interface on the target (loopback by
# default), reachable from the host but generally NOT from the operator's machine.
sleep 5
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

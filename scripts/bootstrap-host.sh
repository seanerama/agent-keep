#!/usr/bin/env bash
# Agent Keep — host bootstrap: any fresh Ubuntu+Docker host → deploy-ready
# (ADR 0007, "two inputs → deployed"; the first leg of the north star). Runs on
# the OPERATOR's workstation and drives a target host over SSH. It automates,
# exactly, the manual preflight in docs/deploy/first-live-chassis.md §1 so that
# deploy.sh's pre-flight (`docker info`; `sudo -n $HELPER preflight-check`)
# subsequently passes against the host.
#
# What it does (idempotent — safe to re-run):
#   1. Docker: if absent, install Docker Engine via the official convenience
#      script (curl -fsSL https://get.docker.com | sh — the HOST needs outbound
#      internet for this) and add the SSH user to the `docker` group; if present,
#      skip. Verify docker is operational (via `sudo docker` so it works even
#      before the group membership takes effect on a fresh install).
#   2. Scoped root helper + sudoers: install deploy/agent-keep-deploy →
#      /usr/local/sbin/agent-keep-deploy (0755 root) and the sudoers drop-in from
#      deploy/sudoers-agent-keep — with its `<deploy-user>` placeholder
#      substituted (CONTROL-SIDE, before scp) to the ACTUAL ssh login — installed
#      0440 at /etc/sudoers.d/agent-keep and validated with `visudo -c`. Re-runs
#      re-install the (possibly changed in-repo) helper/sudoers.
#   3. Verify (conformance): docker works, systemd is present, and the helper is
#      callable NON-INTERACTIVELY via `sudo -n` (proving the NOPASSWD sudoers is
#      live, not a password prompt). Prints `HOST READY` or `BOOTSTRAP FAILED: …`.
#
# Sudo model: the bootstrap's install/usermod/visudo steps need root and this is
# a FRESH host, so they run over `ssh -t` and WILL prompt the operator for the
# host password interactively — that is expected (unlike the POST-bootstrap
# helper, which the sudoers makes NOPASSWD). Do NOT assume passwordless sudo here.
#
# Nothing about the target is hardcoded: the host comes from the single argument
# (an `user@host` pair or a ~/.ssh/config alias), mirroring deploy.sh's
# DEPLOY_HOST. The ssh login is derived from the host (`ssh <target> whoami`), so
# the same command works for a local VM, a self-provisioned cloud VM, or a
# client-handed VM in their tenant.
#
# Usage:  scripts/bootstrap-host.sh <ssh-target>
#   e.g.  scripts/bootstrap-host.sh smahoney@dev-server
#         scripts/bootstrap-host.sh nsaf-dev            (an ~/.ssh/config alias)
#
# Every `ssh "$TARGET" "... $DEPLOY_USER ..."` below INTENTIONALLY expands
# client-side: DEPLOY_USER is derived from the target and the remote paths are
# fixed constants — exactly what we want sent to the host. So SC2029 (client-side
# expansion) is disabled file-wide. (Directive must precede the first command.)
# shellcheck disable=SC2029
set -euo pipefail

usage() {
  echo "usage: scripts/bootstrap-host.sh <ssh-target>" >&2
  echo "  <ssh-target>  an ssh alias or user@host (as in deploy.sh's DEPLOY_HOST)" >&2
  echo "  e.g. scripts/bootstrap-host.sh smahoney@dev-server" >&2
  exit 64
}

if [ "$#" -ne 1 ]; then
  usage
fi
TARGET="$1"

fail() {
  echo "BOOTSTRAP FAILED: $1" >&2
  exit 1
}

# Resolve script-relative repo paths so the script works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HELPER_SRC="${REPO_ROOT}/deploy/agent-keep-deploy"
SUDOERS_SRC="${REPO_ROOT}/deploy/sudoers-agent-keep"
[ -f "$HELPER_SRC" ] || fail "helper not found in repo: ${HELPER_SRC}"
[ -f "$SUDOERS_SRC" ] || fail "sudoers template not found in repo: ${SUDOERS_SRC}"

HELPER_DEST="/usr/local/sbin/agent-keep-deploy"

echo "==> bootstrapping host: ${TARGET}"

# ── Derive the ssh login (the sudoers `<deploy-user>`) from the target itself ──
# `whoami` on the host is authoritative and works for a bare alias too (where we
# cannot parse `user@` off the argument).
DEPLOY_USER="$(ssh -o ConnectTimeout=10 "$TARGET" whoami)" \
  || fail "cannot reach ${TARGET} over ssh (is it up / on the right network?)"
DEPLOY_USER="${DEPLOY_USER//[$'\t\r\n ']/}"   # trim any stray whitespace
[ -n "$DEPLOY_USER" ] || fail "could not determine the ssh login on ${TARGET}"
echo "    ssh login (sudoers <deploy-user>): ${DEPLOY_USER}"

# ── [1/3] Docker ──────────────────────────────────────────────────────────────
echo "==> [1/3] docker"
DOCKER_FRESH=0
if ssh "$TARGET" 'command -v docker >/dev/null 2>&1'; then
  echo "    docker already installed — skipping install"
else
  echo "    docker not found — installing Docker Engine (host needs outbound internet)"
  echo "    via the official convenience script: curl -fsSL https://get.docker.com | sh"
  ssh "$TARGET" 'curl -fsSL https://get.docker.com | sh' \
    || fail "Docker Engine install failed on ${TARGET}"
  echo "    adding ${DEPLOY_USER} to the docker group"
  ssh -t "$TARGET" "sudo usermod -aG docker ${DEPLOY_USER}" \
    || fail "could not add ${DEPLOY_USER} to the docker group"
  DOCKER_FRESH=1
fi

# Verify docker is operational. Use `sudo docker` so this works even on a fresh
# install where the just-added group membership has not taken effect yet in this
# ssh session (it needs a re-login) — see the re-login note printed at the end.
ssh -t "$TARGET" 'sudo docker info >/dev/null 2>&1' \
  || fail "docker is not operational on ${TARGET} (check the install / daemon)"
echo "    docker: operational"

# ── [2/3] Scoped root helper + sudoers (with <deploy-user> substituted) ────────
echo "==> [2/3] scoped root helper + sudoers"

# Substitute `<deploy-user>` CONTROL-SIDE, before scp, so the literal placeholder
# NEVER travels to or is installed on the host. Global replace guarantees no
# `<deploy-user>` token survives anywhere in the rendered file (rule line AND the
# explanatory comment).
SUDOERS_RENDERED="$(mktemp)"
trap 'rm -f "$SUDOERS_RENDERED"' EXIT
sed "s/<deploy-user>/${DEPLOY_USER}/g" "$SUDOERS_SRC" > "$SUDOERS_RENDERED"
if grep -q '<deploy-user>' "$SUDOERS_RENDERED"; then
  fail "internal error: <deploy-user> placeholder survived substitution"
fi
echo "    rendered sudoers for user '${DEPLOY_USER}'"

# Stage both artifacts on the host, then install as root and validate.
scp -q "$HELPER_SRC" "$TARGET":/tmp/agent-keep-deploy \
  || fail "could not copy the helper to ${TARGET}"
scp -q "$SUDOERS_RENDERED" "$TARGET":/tmp/sudoers-agent-keep \
  || fail "could not copy the rendered sudoers to ${TARGET}"

# One privileged, interactive (ssh -t) transaction. Validate the STAGED sudoers
# with `visudo -cf` FIRST, so a malformed file is rejected before it can reach
# /etc/sudoers.d/ (a bad drop-in there can break sudo host-wide). Then install
# the helper 0755 root, install the validated sudoers 0440 root, re-check the
# whole config with visudo -c, and clean up the staged copies.
ssh -t "$TARGET" "sudo visudo -cf /tmp/sudoers-agent-keep \
  && sudo install -o root -g root -m 0755 /tmp/agent-keep-deploy ${HELPER_DEST} \
  && sudo install -o root -g root -m 0440 /tmp/sudoers-agent-keep /etc/sudoers.d/agent-keep \
  && sudo visudo -c \
  && rm -f /tmp/agent-keep-deploy /tmp/sudoers-agent-keep" \
  || fail "installing the helper/sudoers on ${TARGET} failed (visudo rejected the sudoers, or the install did)"
echo "    helper installed 0755 at ${HELPER_DEST}"
echo "    sudoers installed 0440 at /etc/sudoers.d/agent-keep (visudo -c ok)"

# ── [3/3] Verify (conformance check) ───────────────────────────────────────────
echo "==> [3/3] verify (conformance check)"

ssh "$TARGET" 'systemctl --version >/dev/null 2>&1' \
  || fail "systemd not present on ${TARGET} (the chassis runs as a systemd unit)"
echo "    systemd: present"

# The helper must be callable NON-INTERACTIVELY via sudo -n — this is exactly the
# probe deploy.sh's pre-flight runs. `sudo -n` fails LOUDLY (no prompt) if the
# NOPASSWD sudoers is not live, which is precisely what we are asserting here.
ssh "$TARGET" "sudo -n ${HELPER_DEST} preflight-check >/dev/null 2>&1" \
  || fail "helper not callable via 'sudo -n' — the NOPASSWD sudoers is not in effect for ${DEPLOY_USER}"
echo "    helper: callable via sudo -n (NOPASSWD sudoers live)"

echo
echo "HOST READY — ${TARGET} is deploy-ready (docker + systemd + scoped helper)."
if [ "$DOCKER_FRESH" -eq 1 ]; then
  echo "NOTE: ${DEPLOY_USER} was just added to the docker group. deploy.sh runs"
  echo "      \`docker info\` WITHOUT sudo, so reconnect your ssh session (log out/in)"
  echo "      before deploying so the new group membership takes effect."
fi
echo "Next: DEPLOY_HOST=${TARGET} ./deploy.sh <slug> <worker-version-tag>"
echo "      (see docs/deploy/first-live-chassis.md)"

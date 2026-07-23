#!/usr/bin/env bash
# Agent Keep — THE single deploy entry point (ADR 0007 north star: "two inputs →
# deployed"). Give it (1) a BLUEPRINT (a keep/v1 agent spec) and (2) a TARGET (an
# ssh host, or `-`/`LOCAL` for the local docker daemon) and it deploys the audited
# paired chassis — bootstrap-if-needed → worker image → the worker+proxy+mechanic
# topology → liveness verify — with no manual steps between the two inputs.
#
# This is THIN ORCHESTRATION over already-proven, already-merged pieces. It does
# NOT reimplement any of them; it CALLS them:
#   scripts/bootstrap-host.sh        any fresh host → deploy-ready (Stage 13)
#   scripts/deliver-worker-image.sh  build a worker image from a spec + load it
#                                    (Stage 14, used for the LOCAL walking skeleton)
#   ./deploy.sh                      the low-level deploy ENGINE for an ssh host:
#                                    systemd unit, digest pins, secret injection,
#                                    its own liveness gate (Stage 5 + 7 + 14).
#   keep_spec.load_spec              read the blueprint's LOCKED slug (identity),
#                                    so the operator never passes it.
#
# Usage:
#   scripts/deploy-agent.sh <blueprint-spec> <target>
#     <blueprint-spec>  a keep/v1 AgentSpec YAML (the agent to deploy).
#     <target>          an ssh target (user@host or a ~/.ssh/config alias), OR
#                       `-` / `LOCAL` for the LOCAL docker daemon (walking
#                       skeleton — no ssh hop, no systemd; stands the trio up
#                       directly, the way tests/integration/test_paired_topology
#                       does, since a bare local daemon has no systemd).
#
# Image mode (which worker image gets deployed):
#   DEFAULT — the UNIVERSAL path: BUILD the worker image from the blueprint and
#     LOAD it (no registry write), via deploy.sh's KEEP_WORKER_LOCAL_IMAGE=1 mode
#     (ssh target) or scripts/deliver-worker-image.sh (LOCAL). Works for ANY spec.
#   KEEP_WORKER_VERSION=<tag> — OPT-IN published-registry path for the known
#     CI-published variants: pull `<tag>` from ghcr and digest-pin it (deploy.sh's
#     unchanged registry-pull mode). Only meaningful for an ssh target.
#
# Secrets (provider-agnostic, stdin-only — Stage 7 discipline):
#   KEEP_DEPLOY_SECRETS=1 — read `VAR=value` secret line(s) from THIS script's
#     stdin ONCE and thread them to deploy.sh on stdin (never argv/log). A
#     keyless/local blueprint (e.g. the static default chatbot, or a local Ollama)
#     needs no secret — just omit the flag.
#     e.g. printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" \
#            | KEEP_DEPLOY_SECRETS=1 scripts/deploy-agent.sh specs/foo.yaml op@host
#
# Every remote command is a fixed constant with client-known, validated args
# (same posture as deploy.sh / bootstrap-host.sh), so SC2029 is disabled file-wide.
# shellcheck disable=SC2029
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  echo "usage: scripts/deploy-agent.sh <blueprint-spec> <target>" >&2
  echo "  <blueprint-spec>  a keep/v1 AgentSpec YAML" >&2
  echo "  <target>          an ssh target (user@host / alias), or '-'/'LOCAL' for" >&2
  echo "                    the local docker daemon" >&2
  echo "  e.g. scripts/deploy-agent.sh specs/default-chatbot.yaml op@host" >&2
  echo "       scripts/deploy-agent.sh specs/default-chatbot.yaml LOCAL" >&2
  exit 64
}

if [ "$#" -ne 2 ]; then
  usage
fi
BLUEPRINT="$1"
TARGET="$2"

if [ ! -f "$BLUEPRINT" ]; then
  echo "blueprint spec not found: ${BLUEPRINT}" >&2
  exit 66
fi

# ── Provider-agnostic deploy-time secrets: read stdin ONCE, up front ────────────
# We must consume stdin BEFORE any sub-command (bootstrap / deploy.sh) might, so
# the secret bytes are captured exactly once and then THREADED to deploy.sh on
# stdin only. NEVER echo DEPLOY_SECRETS; it is stdin-only, never argv/log.
DEPLOY_SECRETS=""
if [ "${KEEP_DEPLOY_SECRETS:-}" = "1" ]; then
  DEPLOY_SECRETS="$(cat)"
  if [ -z "$DEPLOY_SECRETS" ]; then
    printf '%s\n' "KEEP_DEPLOY_SECRETS=1 but stdin was empty — pipe VAR=value line(s), e.g." >&2
    printf '  %s\n' "printf 'ANTHROPIC_API_KEY=%s\\n' \"\$KEY\" | KEEP_DEPLOY_SECRETS=1 scripts/deploy-agent.sh ${BLUEPRINT} ${TARGET}" >&2
    exit 64
  fi
fi

# ── Pick a python that can import keep_spec (to read the blueprint's slug) ───────
# Operators may run this with the repo venv active (python3 resolves to it) or
# not; CI runs under `uv run` (venv on PATH). Prefer an explicit override, then
# the repo-local venv, then whatever python3/python can import keep_spec.
_pick_python() {
  local candidate
  for candidate in "${KEEP_PYTHON:-}" "${REPO_ROOT}/.venv/bin/python" python3 python; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c "import keep_spec" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}
PYTHON="$(_pick_python)" || {
  echo "cannot find a python that imports keep_spec (activate the repo venv, or set KEEP_PYTHON)" >&2
  exit 69
}

# ── Derive the slug from the blueprint (operator never passes it) ────────────────
# The slug is the blueprint's LOCKED identity (metadata.slug); the worker image
# name follows agent-keep-<slug> (keep_spec/models.py Metadata.slug).
SLUG="$("$PYTHON" -c 'import sys
from keep_spec import load_spec
sys.stdout.write(load_spec(sys.argv[1]).metadata.slug)' "$BLUEPRINT")" || {
  echo "could not read the slug from ${BLUEPRINT} (is it a valid keep/v1 spec?)" >&2
  exit 65
}
if [ -z "$SLUG" ]; then
  echo "blueprint ${BLUEPRINT} has an empty slug" >&2
  exit 65
fi

# ── Image mode: default universal build+load, or the published-tag opt-in ───────
PUBLISHED_VERSION="${KEEP_WORKER_VERSION:-}"
if [ -n "$PUBLISHED_VERSION" ]; then
  IMAGE_MODE="published"
  VERSION="$PUBLISHED_VERSION"
else
  IMAGE_MODE="local"
  VERSION="local"   # a sensible local tag for the built-from-blueprint worker
fi

echo "==> deploy-agent: blueprint=${BLUEPRINT} slug=${SLUG} target=${TARGET}"
echo "    image mode: ${IMAGE_MODE} (worker version tag '${VERSION}')"
if [ -n "$DEPLOY_SECRETS" ]; then
  echo "    provider secret(s): supplied on stdin (threaded to deploy.sh, never logged)"
fi

# ────────────────────────────────────────────────────────────────────────────────
# LOCAL walking skeleton (`-` / `LOCAL`): the local docker daemon has no ssh and
# no systemd, so deploy.sh's engine cannot drive it. Instead, stand the audited
# paired topology up DIRECTLY on the local daemon — exactly the trio the systemd
# unit (deploy/systemd/agent-keep@.service) supervises and the CI paired-topology
# test composes — from the worker image BUILT+LOADED from the blueprint, then run
# the frozen smoke. Conformance is N/A here (no host to bootstrap).
# ────────────────────────────────────────────────────────────────────────────────
deploy_local() {
  if [ "$IMAGE_MODE" = "published" ]; then
    echo "note: KEEP_WORKER_VERSION (published-tag mode) is only meaningful for an ssh" >&2
    echo "      target; the LOCAL walking skeleton always builds+loads from the blueprint." >&2
  fi
  command -v docker >/dev/null 2>&1 || { echo "docker not found (required for a LOCAL deploy)" >&2; exit 69; }

  local owner worker_img worker_ref proxy_img mechanic_img suffix wport mport
  # Names the EXIT trap tears down are GLOBAL + pre-initialized empty: the trap
  # fires at script exit AFTER this function returns, when its locals are already
  # out of scope — under `set -u` that would be an "unbound variable" error.
  net="" egress="" proxy="" mechanic="" ingress="" worker="" bundle="" secret_env=""
  owner="${AGENT_KEEP_OWNER:-seanerama}"
  worker_img="ghcr.io/${owner}/agent-keep-${SLUG}:${VERSION}"
  # The generic proxy + mechanic images: `keep-build` bakes them locally under the
  # :latest tag (no registry write), so the LOCAL path stays fully offline — build
  # them from source if they are not already present rather than pulling.
  proxy_img="ghcr.io/${owner}/agent-keep-egress-proxy:${KEEP_PROXY_VERSION:-latest}"
  mechanic_img="ghcr.io/${owner}/agent-keep-mechanic:${KEEP_MECHANIC_VERSION:-latest}"

  echo "==> [LOCAL] build worker from blueprint and load it into the local daemon (no registry)"
  worker_ref="$("${SCRIPT_DIR}/deliver-worker-image.sh" "$BLUEPRINT" "$worker_img" LOCAL)"
  [ -n "$worker_ref" ] || { echo "==> DEPLOY FAILED: local worker build/load produced no image id" >&2; exit 1; }
  echo "    worker image: ${worker_ref}"

  # The generic proxy + mechanic images (bake locally if absent — no registry).
  echo "==> [LOCAL] ensure generic proxy + mechanic images are present"
  docker image inspect "$proxy_img" >/dev/null 2>&1 || keep-build build-proxy >&2
  docker image inspect "$mechanic_img" >/dev/null 2>&1 \
    || keep-build build "${REPO_ROOT}/specs/mechanic.yaml" >&2

  suffix="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  net="agent-keep-${SLUG}-net-${suffix}"          # --internal: NO route out
  egress="agent-keep-${SLUG}-egress-${suffix}"    # ordinary bridge (host publish)
  proxy="agent-keep-${SLUG}-proxy-${suffix}"
  mechanic="agent-keep-${SLUG}-mechanic-${suffix}"
  ingress="agent-keep-${SLUG}-ingress-${suffix}"
  worker="agent-keep-${SLUG}-${suffix}"
  wport="$(_free_port)"
  mport="$(_free_port)"

  # A Stage-4 bundle dir: the spec (ro source of truth) + a pre-created 0666 audit
  # file the uid-10001 worker appends through, and the mechanic reads read-only.
  bundle="$(mktemp -d)"
  chmod 0755 "$bundle"
  cp "$BLUEPRINT" "${bundle}/${SLUG}.yaml"; chmod 0644 "${bundle}/${SLUG}.yaml"
  : > "${bundle}/${SLUG}.audit.jsonl"; chmod 0666 "${bundle}/${SLUG}.audit.jsonl"

  # Teardown everything on exit (success OR failure) — a walking skeleton must not
  # leak containers/networks/tmp. Runtime endpoints are printed before we return.
  # shellcheck disable=SC2317
  _local_teardown() {
    local n
    for n in "$worker" "$ingress" "$mechanic" "$proxy"; do
      [ -n "$n" ] && docker rm -f "$n" >/dev/null 2>&1 || true
    done
    for n in "$net" "$egress"; do
      [ -n "$n" ] && docker network rm "$n" >/dev/null 2>&1 || true
    done
    [ -n "$secret_env" ] && rm -f "$secret_env" >/dev/null 2>&1 || true
    [ -n "$bundle" ] && rm -rf "$bundle" >/dev/null 2>&1 || true
  }
  trap _local_teardown EXIT

  echo "==> [LOCAL] stand up the audited paired topology (two networks + proxy + mechanic + ingress + worker)"
  docker network create --internal "$net" >/dev/null
  docker network create "$egress" >/dev/null

  # egress-proxy: dual-homed (internal alias + egress leg), spec mounted read-only.
  docker run -d --name "$proxy" \
    --network "$net" --network-alias egress-proxy \
    -e KEEP_EGRESS_HOST= -e KEEP_EGRESS_PORT= \
    --security-opt no-new-privileges --cap-drop ALL --read-only \
    --pids-limit 512 --memory 256m -e PYTHONDONTWRITEBYTECODE=1 \
    --tmpfs /tmp:mode=1777 \
    -v "${bundle}/${SLUG}.yaml:/etc/agent-keep/spec.yaml:ro" \
    "$proxy_img" >/dev/null
  docker network connect "$egress" "$proxy" >/dev/null

  # mechanic: internal only, bundle dir read-only, own audit on its own tmpfs.
  local mech_id
  mech_id="$(docker image inspect -f '{{.Id}}' "$mechanic_img")"
  docker run -d --name "$mechanic" \
    --network "$net" --network-alias mechanic \
    -e "AGENT_IMAGE_DIGEST=${mech_id}" \
    -e MECHANIC_WORKER_DIR=/srv/worker-bundle \
    -e SQLITE_PATH=/tmp/agent-keep-mechanic-sessions.sqlite3 \
    --security-opt no-new-privileges --cap-drop ALL --read-only \
    --pids-limit 512 --memory 512m -e PYTHONDONTWRITEBYTECODE=1 \
    --tmpfs /tmp:mode=1777 \
    -v "${bundle}:/srv/worker-bundle:ro" \
    "$mechanic_img" >/dev/null

  # ingress forwarder: host-published, relays host→worker / host→mechanic ONLY.
  docker run -d --name "$ingress" \
    --network "$egress" \
    -p "127.0.0.1:${wport}:8000" -p "127.0.0.1:${mport}:8001" \
    --security-opt no-new-privileges --cap-drop ALL --read-only \
    --pids-limit 128 --memory 128m -e PYTHONDONTWRITEBYTECODE=1 \
    --tmpfs /tmp:mode=1777 \
    -v "${REPO_ROOT}/deploy/ingress-forward.py:/ingress-forward.py:ro" \
    "$proxy_img" \
    python /ingress-forward.py 8000:worker:8000 8001:mechanic:8000 >/dev/null
  docker network connect "$net" "$ingress" >/dev/null

  # worker: internal ONLY (no route out) + HTTP(S)_PROXY → the paired proxy; audit
  # on its own tmpfs so smoke-chat can read a fresh line via `docker:<name>`.
  local worker_id secret_env_args=()
  worker_id="$(docker image inspect -f '{{.Id}}' "$worker_ref")"
  if [ -n "$DEPLOY_SECRETS" ]; then
    # Thread provider secret(s) to the worker via an --env-file (0600 tmp), NOT
    # argv, so the VALUE never lands in `docker run`'s command line or any log.
    # (`secret_env` is the global the EXIT teardown removes.)
    secret_env="$(mktemp)"; chmod 0600 "$secret_env"
    printf '%s\n' "$DEPLOY_SECRETS" > "$secret_env"
    secret_env_args=(--env-file "$secret_env")
  fi
  docker run -d --name "$worker" \
    --network "$net" --network-alias worker \
    -e HTTP_PROXY=http://egress-proxy:3128 -e HTTPS_PROXY=http://egress-proxy:3128 \
    -e http_proxy=http://egress-proxy:3128 -e https_proxy=http://egress-proxy:3128 \
    -e NO_PROXY=localhost,127.0.0.1 -e no_proxy=localhost,127.0.0.1 \
    -e "AGENT_IMAGE_DIGEST=${worker_id}" \
    -e SQLITE_PATH=/tmp/agent-keep-sessions.sqlite3 \
    "${secret_env_args[@]}" \
    --security-opt no-new-privileges --cap-drop ALL --read-only \
    --pids-limit 512 --memory 512m -e PYTHONDONTWRITEBYTECODE=1 \
    --tmpfs /tmp:mode=1777 --tmpfs /var/lib/agent-keep:mode=1777 \
    "$worker_ref" >/dev/null

  echo "==> [LOCAL] wait for liveness (worker + mechanic /healthz via the forwarder)"
  _wait_http_ok "http://127.0.0.1:${wport}/healthz"
  _wait_http_ok "http://127.0.0.1:${mport}/healthz"

  echo "==> [LOCAL] smoke: one dev-http round-trip against the worker (audited)"
  if ! "${SCRIPT_DIR}/smoke-chat.sh" "127.0.0.1:${wport}" "docker:${worker}"; then
    echo "==> DEPLOY FAILED: smoke did not pass against the LOCAL chassis" >&2
    exit 1
  fi

  echo
  echo "DEPLOYED: ${SLUG} on ${TARGET}"
  echo "    worker  dev-http : 127.0.0.1:${wport}"
  echo "    mechanic console : 127.0.0.1:${mport}"
  echo "    egress-proxy     : internal only (no host port)"
  echo "    (LOCAL walking skeleton — the audited trio is torn down on exit)"
}

_free_port() {
  "$PYTHON" -c 'import socket
s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

_wait_http_ok() {
  local url="$1" deadline
  deadline=$(( $(date +%s) + 60 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "==> DEPLOY FAILED: ${url} never became ready" >&2
  exit 1
}

# ────────────────────────────────────────────────────────────────────────────────
# SSH target: the full north-star path over the real deploy ENGINE (deploy.sh).
#   1. Ensure conformant (idempotent): probe the helper; bootstrap if it is absent.
#   2. Resolve image + deploy: deploy.sh with KEEP_SPEC_FILE=<blueprint>, in local
#      build+load mode (default) or registry-pull mode (KEEP_WORKER_VERSION),
#      threading secrets on stdin. deploy.sh runs its own liveness gate.
# ────────────────────────────────────────────────────────────────────────────────
deploy_ssh() {
  local helper="/usr/local/sbin/agent-keep-deploy"

  echo "==> [1/2] ensure ${TARGET} is conformant (deploy-ready)"
  if ssh -o ConnectTimeout=10 -o BatchMode=yes "$TARGET" "sudo -n ${helper} preflight-check" \
    >/dev/null 2>&1; then
    echo "    already deploy-ready (helper + sudoers bootstrapped) — skipping bootstrap"
  else
    echo "    not deploy-ready — running scripts/bootstrap-host.sh ${TARGET}"
    # Bootstrap must NOT read our (already-consumed) stdin; give it /dev/null.
    "${SCRIPT_DIR}/bootstrap-host.sh" "$TARGET" </dev/null
  fi

  echo "==> [2/2] deploy via ./deploy.sh (its engine: image pin, systemd unit, liveness gate)"
  local -a deploy_env=(
    "DEPLOY_HOST=${TARGET}"
    "KEEP_SPEC_FILE=${BLUEPRINT}"
  )
  if [ "$IMAGE_MODE" = "local" ]; then
    echo "    image: build the blueprint's worker locally + load it (KEEP_WORKER_LOCAL_IMAGE=1)"
    deploy_env+=("KEEP_WORKER_LOCAL_IMAGE=1")
  else
    echo "    image: pull the published worker tag '${VERSION}' from the registry"
  fi

  # Thread secrets to deploy.sh on stdin ONLY (never argv/log); deploy.sh reads
  # them once when KEEP_DEPLOY_SECRETS=1. Without secrets, run with empty stdin.
  local rc=0
  if [ -n "$DEPLOY_SECRETS" ]; then
    printf '%s\n' "$DEPLOY_SECRETS" \
      | env "${deploy_env[@]}" KEEP_DEPLOY_SECRETS=1 \
          "${REPO_ROOT}/deploy.sh" "$SLUG" "$VERSION" || rc=$?
  else
    env "${deploy_env[@]}" "${REPO_ROOT}/deploy.sh" "$SLUG" "$VERSION" </dev/null || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    echo "==> DEPLOY FAILED: deploy.sh exited ${rc} for ${SLUG} on ${TARGET}" >&2
    exit "$rc"
  fi

  echo
  echo "DEPLOYED: ${SLUG} on ${TARGET}"
  echo "    the worker + mechanic endpoints are printed by deploy.sh above"
  echo "    (bind interface + ports per deploy.sh; internal egress-proxy has no host port)"
}

if [ "$TARGET" = "-" ] || [ "$TARGET" = "LOCAL" ]; then
  deploy_local
else
  deploy_ssh
fi

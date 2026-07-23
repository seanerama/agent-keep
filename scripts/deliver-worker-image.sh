#!/usr/bin/env bash
# Agent Keep — build an arbitrary blueprint's WORKER image locally and DELIVER it
# to a target host WITHOUT any registry write (ADR 0007, "two inputs → deployed",
# the arbitrary-blueprint image path). The published-variant path (deploy.sh's
# pull+pin) is unchanged; this is the ADDED no-ghcr-write mechanism deploy.sh uses
# in local-image mode, and the seam Stage 15's single entry point reuses.
#
# Usage:
#   deliver-worker-image.sh <spec-file> <image-ref> <ssh-host>
#     <spec-file>  the blueprint spec.yaml (keep-build bakes it)
#     <image-ref>  the local build tag, e.g. ghcr.io/<owner>/agent-keep-<slug>:<tag>
#     <ssh-host>   ssh target to load the image onto. Pass "-" (or "LOCAL") to
#                  load into the LOCAL docker daemon (a local-host deploy / the
#                  container test path — no ssh hop).
#
# What it does:
#   1. `keep-build build <spec> --tag <ref>` — bake the image locally (it already
#      produces a local image; verified). Diagnostics go to stderr.
#   2. Deliver it with NO intermediate file and NO registry:
#        docker save <ref> | ssh <host> docker load        (remote host)
#        docker save <ref> | docker load                   (local host)
#   3. Print the delivered image's IMMUTABLE ID (sha256:...) to stdout — and
#      NOTHING else on stdout. THIS is the pin for a LOADED image: a `docker
#      load`ed image has NO RepoDigests (it was never pulled/pushed), so deploy.sh
#      CANNOT pin it via `{{index .RepoDigests 0}}`. `{{.Id}}` is content-addressed
#      and immutable, and `docker run <sha256-id>` is a valid, immutable ref — the
#      same immutability guarantee the registry-digest pin gives, sourced locally.
#
# Client-side expansion of the fixed remote commands is intentional (the ref is a
# client-known, validated string) — same posture as deploy.sh.
# shellcheck disable=SC2029
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: deliver-worker-image.sh <spec-file> <image-ref> <ssh-host|->" >&2
  exit 64
fi
SPEC="$1"
REF="$2"
HOST="$3"

if [ ! -f "$SPEC" ]; then
  echo "spec file not found: ${SPEC}" >&2
  exit 66
fi

# Resolve how to invoke keep-build. It is a uv workspace console script, so it is
# only on PATH inside an activated venv (e.g. CI); an operator running from a
# plain shell has it via `uv run keep-build`. Prefer the bare command if present,
# else fall back to `uv run` (issue #47). Overridable via $KEEP_BUILD.
if [ -n "${KEEP_BUILD:-}" ]; then
  read -r -a _kb <<<"$KEEP_BUILD"
elif command -v keep-build >/dev/null 2>&1; then
  _kb=(keep-build)
elif command -v uv >/dev/null 2>&1; then
  _kb=(uv run keep-build)
else
  echo "keep-build not found — need the keep-build console script on PATH, or 'uv' to run it from the repo" >&2
  exit 69
fi

# 1. Bake locally. All keep-build/docker-build chatter to stderr so stdout stays
#    clean for the single id line at the end.
echo "==> build worker image locally: ${_kb[*]} build ${SPEC} --tag ${REF}" >&2
"${_kb[@]}" build "$SPEC" --tag "$REF" >&2

# 2. Deliver (stream save|load, no file, no registry) and 3. pin by immutable id.
if [ "$HOST" = "-" ] || [ "$HOST" = "LOCAL" ]; then
  echo "==> load into the LOCAL docker daemon (no registry, no ssh)" >&2
  docker save "$REF" | docker load >&2
  docker inspect --format '{{.Id}}' "$REF"
else
  echo "==> deliver to ${HOST}: docker save | ssh ${HOST} docker load (no registry)" >&2
  docker save "$REF" | ssh "$HOST" "docker load" >&2
  # Pin by the loaded image's immutable ID, resolved ON THE HOST (a loaded image
  # has NO RepoDigests, so .Id is the pin — see header).
  ssh "$HOST" "docker inspect --format '{{.Id}}' '${REF}'"
fi

#!/usr/bin/env bash
# Assert a docker container is actually RUNNING — the egress-proxy liveness gate
# (issue #13, Defect 2). The proxy is launched fire-and-forget by the systemd
# unit's `ExecStartPre=docker run -d`, which returns 0 the instant it detaches;
# if the container then exits 1 (e.g. the empty-KEEP_EGRESS_PORT boot crash),
# NOTHING notices — deploy.sh's old verify step only curled worker + mechanic
# /healthz, so it printed "deploy verified live" with the audited security
# boundary DOWN. This helper closes that gap: a dead proxy FAILS the deploy.
#
# Run ON THE DEPLOY HOST (deploy.sh pipes it over ssh: `ssh "$HOST" bash -s --
# <container> < scripts/assert-proxy-running.sh`), so `docker inspect` sees the
# host's daemon. Kept standalone (not inline in deploy.sh) so it is unit-testable
# with a stubbed `docker` on PATH — see tests/deploy/test_proxy_liveness.py.
#
# Usage: assert-proxy-running.sh <container-name>
# Exit:  0 if State.Running == true (within the settle window); non-zero otherwise.
#
# Settle: `docker run -d` may still be starting; poll a few times before failing.
# Overridable for tests via PROXY_LIVENESS_ATTEMPTS / PROXY_LIVENESS_INTERVAL.
set -euo pipefail

CONTAINER="${1:?usage: assert-proxy-running.sh <container-name>}"
attempts="${PROXY_LIVENESS_ATTEMPTS:-10}"
interval="${PROXY_LIVENESS_INTERVAL:-1}"

state=""
for _ in $(seq 1 "$attempts"); do
  # `|| true`: `docker inspect` exits non-zero for an absent container; treat
  # that as "not running" (state stays "") rather than aborting under `set -e`.
  state="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)"
  if [ "$state" = "true" ]; then
    echo "egress-proxy '${CONTAINER}' is running"
    exit 0
  fi
  sleep "$interval"
done

echo "ERROR: egress-proxy '${CONTAINER}' is NOT running (State.Running=${state:-<absent>})." >&2
echo "       The proxy is the audited egress security boundary — a dead proxy FAILS the deploy." >&2
echo "       Inspect why it exited:  docker logs ${CONTAINER}" >&2
exit 1

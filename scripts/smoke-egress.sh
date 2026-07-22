#!/usr/bin/env bash
# scripts/smoke-egress.sh — the Stage-3 observably-works asset.
#
# Exec into the live agent container, attempt an outbound fetch of a
# NON-allowlisted host, and assert BOTH halves of the egress boundary:
#   1. the attempt is refused (the proxy's 403 — or, with no proxy env at
#      all, the internal network's no-route: either way it must NOT succeed);
#   2. a NEW denied `egress` audit record for that host appears in the
#      PROXY's append-only audit log (egress-audit.jsonl — the proxy's own
#      file, separate from the worker's audit.jsonl).
#
# Usage:
#   scripts/smoke-egress.sh <agent-container> [proxy-audit-source] [denied-host]
#
#   <agent-container>     docker container name of the running agent.
#   [proxy-audit-source]  where to read the proxy's audit log:
#                           docker:<proxy-container>  docker exec cat inside
#                                                     the proxy container
#                           <path>                    the egress-audit.jsonl
#                                                     path on the deploy host
#                         Omitted = the audit assertion is skipped with a
#                         notice (refusal alone is NOT the full proof).
#   [denied-host]         host to attempt (default smoke-denied.example.com —
#                         never allowlist this).
#
# The agent image is python-slim (no curl); the fetch runs via python3 inside
# the container, honoring the container's HTTP(S)_PROXY env exactly as the
# agent's own runtime would. Operator runs this in Stage 5's live smoke;
# tests/integration/test_egress_proxy.py runs it in CI so it cannot rot.
set -euo pipefail

AGENT="${1:?usage: $0 <agent-container> [proxy-audit-source] [denied-host]}"
AUDIT_SOURCE="${2:-}"
DENIED_HOST="${3:-smoke-denied.example.com}"
PROXY_AUDIT_PATH="/var/lib/agent-keep/egress-audit.jsonl"

read_audit() {
  case "$AUDIT_SOURCE" in
    docker:*) docker exec "${AUDIT_SOURCE#docker:}" cat "$PROXY_AUDIT_PATH" 2>/dev/null || true ;;
    *) cat "$AUDIT_SOURCE" 2>/dev/null || true ;;
  esac
}

count_denials() {
  # denied `egress` records for the smoke host (audit-record v1, kind egress)
  read_audit | python3 -c '
import json, sys
host = sys.argv[1]
count = 0
for line in sys.stdin:
    if not line.strip():
        continue
    record = json.loads(line)
    if (record["event"] == "egress" and record["verdict"] == "denied"
            and record["target"].startswith(host + ":")):
        count += 1
print(count)
' "$DENIED_HOST"
}

if [ -n "$AUDIT_SOURCE" ]; then
  BEFORE_DENIALS="$(count_denials)"
fi

echo "==> 1/2 refusal (attempting http://${DENIED_HOST}/ from inside ${AGENT})"
docker exec "$AGENT" python3 -c '
import sys, urllib.error, urllib.request
host = sys.argv[1]
try:
    urllib.request.urlopen(f"http://{host}/", timeout=15)
except urllib.error.HTTPError as exc:
    # the proxy refused it observably — the expected path
    print(f"    refused by proxy: HTTP {exc.code}")
    sys.exit(0 if exc.code == 403 else 1)
except OSError as exc:
    # no proxy env / no route: still a refusal, but not proxy-observed
    print(f"    refused at network level: {type(exc).__name__}")
    sys.exit(0)
print("    FAIL: fetch SUCCEEDED — the egress boundary is open")
sys.exit(1)
' "$DENIED_HOST" || { echo "FAIL: egress attempt was not refused as expected"; exit 1; }
echo "    refusal: OK"

echo "==> 2/2 audited denial"
if [ -z "$AUDIT_SOURCE" ]; then
  echo "    SKIPPED: no proxy audit source given — re-run with docker:<proxy-name>"
  echo "    (or the egress-audit.jsonl path on the deploy host) for the full proof"
  echo "SMOKE PASS: refusal verified (audited-denial check skipped)"
  exit 0
fi
AFTER_DENIALS="$(count_denials)"
if [ "${AFTER_DENIALS}" -le "${BEFORE_DENIALS}" ]; then
  echo "FAIL: no new denied egress record for ${DENIED_HOST} (before=${BEFORE_DENIALS}, after=${AFTER_DENIALS})"
  exit 1
fi
echo "    new denied egress audit record for ${DENIED_HOST}: OK (before=${BEFORE_DENIALS}, after=${AFTER_DENIALS})"
echo "SMOKE PASS: refusal + audited denial verified"

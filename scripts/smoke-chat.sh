#!/usr/bin/env bash
# scripts/smoke-chat.sh — the Stage-2 observably-works asset.
#
# Curl /healthz + one message round-trip against a running default-chatbot
# chassis, asserting a NON-EMPTY reply and (when an audit source is given) a
# NEW audit line appended whose trigger carries the run-correlation key
# (trigger.message_id == the reply's message_id — contract audit-record).
#
# Usage:
#   scripts/smoke-chat.sh <host:port> [audit-source]
#
#   <host:port>     the dev-http surface, e.g. 127.0.0.1:8000 (local container)
#                   or the tailnet address post-deploy (Stage 5, Operator).
#   [audit-source]  optional — where to read the append-only audit log:
#                     docker:<container-name>  docker exec cat inside a local
#                                              container
#                     <path>                   the audit.jsonl file/volume path
#                                              when running ON the deploy host
#                   Omitted = the audit assertion is skipped with a notice
#                   (running remotely, the log is host-only by design).
#
# Exit 0 = healthz ok + non-empty reply (+ new correlated audit line when an
# audit source was given). No secrets involved; nothing here is provider-
# specific — it passes on the static provider in CI and the live provider in
# production alike.
set -euo pipefail

TARGET="${1:?usage: $0 <host:port> [audit-source]   (e.g. $0 127.0.0.1:8000 docker:my-chatbot)}"
AUDIT_SOURCE="${2:-}"
AUDIT_PATH_IN_CONTAINER="/var/lib/agent-keep/audit.jsonl"
BASE="http://${TARGET}"

read_audit() {
  case "$AUDIT_SOURCE" in
    docker:*) docker exec "${AUDIT_SOURCE#docker:}" cat "$AUDIT_PATH_IN_CONTAINER" 2>/dev/null || true ;;
    *) cat "$AUDIT_SOURCE" 2>/dev/null || true ;;
  esac
}

echo "==> 1/3 healthz"
curl -fsS --max-time 10 "${BASE}/healthz" >/dev/null || {
  echo "FAIL: ${BASE}/healthz not answering"; exit 1;
}
echo "    healthz: OK"

if [ -n "$AUDIT_SOURCE" ]; then
  BEFORE_LINES="$(read_audit | grep -c . || true)"
fi

echo "==> 2/3 message round-trip"
REPLY_JSON="$(curl -fsS --max-time 60 -X POST "${BASE}/message" \
  -H 'Content-Type: application/json' \
  -d "{\"text\": \"Say hello in one short sentence.\", \"conversation_id\": \"smoke-chat-$$\", \"sender_id\": \"operator-smoke\"}")"
echo "    reply: ${REPLY_JSON}"
MESSAGE_ID="$(python3 - "$REPLY_JSON" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
reply = payload.get("reply", "")
assert isinstance(reply, str) and reply.strip(), f"empty reply: {payload!r}"
print(payload["message_id"])
PY
)"
echo "    non-empty reply: OK (message_id ${MESSAGE_ID})"

echo "==> 3/3 audit line"
if [ -z "$AUDIT_SOURCE" ]; then
  echo "    SKIPPED: no audit source given — the append-only log is host-only;"
  echo "    re-run on the deploy host with the audit.jsonl path (or docker:<name>)"
  echo "SMOKE PASS: healthz + non-empty reply verified (audit check skipped)"
  exit 0
fi
AFTER="$(read_audit)"
AFTER_LINES="$(printf '%s' "$AFTER" | grep -c . || true)"
if [ "${AFTER_LINES}" -le "${BEFORE_LINES}" ]; then
  echo "FAIL: no new audit line appended (before=${BEFORE_LINES}, after=${AFTER_LINES})"
  exit 1
fi
printf '%s\n' "$AFTER" | python3 -c '
import json, sys
message_id = sys.argv[1]
records = [json.loads(line) for line in sys.stdin if line.strip()]
calls = [r for r in records if r["event"] == "model_call"]
assert calls, "no model_call audit record"
correlated = [r for r in calls if r["trigger"]["message_id"] == message_id]
assert correlated, f"no model_call audit record correlated to message {message_id}"
record = correlated[-1]
assert record["outcome"]["status"] == "ok", record["outcome"]
assert record["cost"]["tokens_in"] > 0 and record["cost"]["tokens_out"] > 0, record["cost"]
print("    new audit line, run-correlated (trigger.message_id match, token cost): OK")
' "$MESSAGE_ID"
echo "SMOKE PASS: healthz + non-empty reply + new correlated audit line verified"

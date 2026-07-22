#!/usr/bin/env bash
# scripts/smoke-mechanic.sh — the Stage-4 observably-works asset.
#
# One question to a running mechanic ("What did the worker just do?"),
# asserting a NON-EMPTY reply that carries at least one CITATION MARKER, and
# (when an audit source is given) a NEW line appended to the mechanic's OWN
# audit log whose trigger carries the run-correlation key
# (trigger.message_id == the reply's message_id — contract audit-record).
#
# Citation marker: analyzer-backed replies talk in audit-record terms — the
# analyzer's outputs and the baked static reply both carry "audit_record_ids"
# / "audit record" phrasing — so the assertion is a case-insensitive match on
# "audit record" / "audit_record" / "cited". Chosen from what analyzer replies
# actually look like (worker_analyzer.py: explain_behavior statements say
# "cited by the worker spec value and audit record(s)"; read_bundle returns
# "audit_record_ids").
#
# Usage:
#   scripts/smoke-mechanic.sh <host:port> [audit-source]
#
#   <host:port>     the mechanic's dev-http surface, e.g. 127.0.0.1:8001.
#   [audit-source]  optional — where to read the MECHANIC'S OWN append-only
#                   audit log (never the worker bundle — ADR 0011):
#                     docker:<container-name>  docker exec cat inside a local
#                                              container
#                     <path>                   the audit.jsonl path on the host
#                   Omitted = the audit assertion is skipped with a notice.
#
# The mechanic is gated owner-only (specs/mechanic.yaml roster:
# dev-http:owner), so the question is sent with sender_id "owner".
#
# Exit 0 = healthz ok + non-empty CITED reply (+ new correlated audit line
# when an audit source was given). No secrets involved; passes on the static
# provider in CI and a live provider in production alike.
set -euo pipefail

TARGET="${1:?usage: $0 <host:port> [audit-source]   (e.g. $0 127.0.0.1:8001 docker:my-mechanic)}"
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

echo "==> 2/3 cited answer"
REPLY_JSON="$(curl -fsS --max-time 60 -X POST "${BASE}/message" \
  -H 'Content-Type: application/json' \
  -d "{\"text\": \"What did the worker just do?\", \"conversation_id\": \"smoke-mechanic-$$\", \"sender_id\": \"owner\"}")"
echo "    reply: ${REPLY_JSON}"
MESSAGE_ID="$(python3 - "$REPLY_JSON" <<'PY'
import json, re, sys
payload = json.loads(sys.argv[1])
reply = payload.get("reply", "")
assert isinstance(reply, str) and reply.strip(), f"empty reply: {payload!r}"
# The citation-marker assertion (see the header for why these markers).
assert re.search(r"audit[_ ]record|cited", reply, re.IGNORECASE), (
    f"reply carries no citation marker (audit record / audit_record / cited): {reply!r}"
)
print(payload["message_id"])
PY
)"
echo "    non-empty cited reply: OK (message_id ${MESSAGE_ID})"

echo "==> 3/3 mechanic's own audit line"
if [ -z "$AUDIT_SOURCE" ]; then
  echo "    SKIPPED: no audit source given — the append-only log is host-only;"
  echo "    re-run on the deploy host with the audit.jsonl path (or docker:<name>)"
  echo "SMOKE PASS: healthz + non-empty cited reply verified (audit check skipped)"
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
assert record["agent"]["slug"] == "mechanic", record["agent"]
print("    new audit line, run-correlated (trigger.message_id match): OK")
tools = [
    r for r in records
    if r["event"] == "tool_call" and r["trigger"]["message_id"] == message_id
    and r["action"]["name"].startswith("analyzer.")
]
if tools:
    names = sorted({r["action"]["name"] for r in tools})
    print(f"    analyzer tool_call(s) correlated to this question: {names}")
' "$MESSAGE_ID"
echo "SMOKE PASS: healthz + non-empty cited reply + new correlated audit line verified"

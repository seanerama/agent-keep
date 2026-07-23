"""keep_egress — the egress observation proxy (contract egress-observation v1,
ADR 0002).

The observed outbound choke point: the agent container has no direct network
route out; every outbound connection is forced through this paired forward
proxy (HTTP absolute-form + HTTPS `CONNECT`), which enforces the spec's
`sandbox.egress` allowlist at runtime, FAIL-CLOSED, and appends one
audit-record v1 line per connection ATTEMPT — allowed and denied — to its own
append-only jsonl file. The proxy ships as its OWN container image
(`ghcr.io/seanerama/agent-keep-egress-proxy`, built by `keep-build
build-proxy`), never inside the agent's image: the choke point observes the
agent from outside.

Allowlist source of truth (no second list, ever)
------------------------------------------------
The proxy is configured by mounting the SAME `spec.yaml` the agent image was
baked from (read-only, `KEEP_SPEC_PATH`, default `/etc/agent-keep/spec.yaml`).
It loads the spec through `keep_spec.load_spec` — the same validator build-time
cross-validation uses — and takes `spec.sandbox.egress` as the allowlist and
`metadata.slug`/`metadata.specVersion` as the observed-agent identity. Matching
semantics live in ONE importable place, `keep_spec.egress` (`host[:port]`,
`*.` wildcard subdomains, case-insensitive, entry-without-port covers every
port). A missing or invalid spec refuses to start; an EMPTY allowlist runs and
denies everything.

Kill-switch: NONE, deliberately. The proxy is a security boundary, not a
dark-launchable feature — deny-all on an empty allowlist IS the safe default.

The `egress` audit record (frozen field names)
----------------------------------------------
audit-record v1, ADDITIVE record kind `egress` with `action: connect` (and,
since issue #24, an additive `action: open` for real-time CONNECT establish —
contracts/egress-observation.md amendment 2026-07-22). Field names froze with
this stage's first green test and are additive-only from then on:

    id            uuid of the record
    ts            RFC 3339 UTC timestamp
    agent.slug    metadata.slug of the observed agent's spec
    agent.spec_version  metadata.specVersion of that spec
    event         literal "egress"           (the additive record kind)
    action        "connect" | "open"         (per the contract's wire section;
                  "open" added additively — issue #24 / egress-observation
                  amendment 2026-07-22 — for the real-time record emitted at
                  CONNECT establish, paired to its "connect" close record by
                  connection_id)
    target        "host:port" of the attempt ("invalid" for unparseable
                  requests — a safe representation; raw request bytes are
                  never logged)
    verdict       "allowed" | "denied"
    matched_entry the sandbox.egress entry that allowed it, or null on deny
    bytes_up      bytes relayed client->target, counted on close (0 on `open`)
    bytes_down    bytes relayed target->client, counted on close (0 on `open`)
    run_id        run-correlation key when the attempt is attributable to a
                  run (contract: "when attributable"); the v1 proxy is not
                  run-aware, so this is null on every record today
    connection_id correlation seam pairing an allowed CONNECT's `open` record
                  (at establish) with its `connect` record (on close); single
                  records get their own unique one. Additive field (issue #24 /
                  egress-observation amendment 2026-07-22), the same kind of
                  change as audit-record's additive `trace_id`

Digests-not-payloads discipline: the proxy records connection TARGETS as
host:port only — never URLs beyond host:port, never headers, never bodies,
never decrypted CONNECT payloads (payload-level inspection would be a NEW
contract, per egress-observation v1).

Runtime configuration (all env, read by `keep_egress.runner`):

    KEEP_SPEC_PATH  (default /etc/agent-keep/spec.yaml) — mounted spec.yaml
    KEEP_EGRESS_AUDIT_PATH  (default /var/lib/agent-keep/egress-audit.jsonl) — proxy-own audit
    KEEP_EGRESS_HOST  (default egress-proxy) — bind address
    KEEP_EGRESS_PORT  (default 3128) — bind port
    KEEP_EGRESS_HEAD_TIMEOUT_SECONDS  (default 10) — request-head read timeout
    KEEP_EGRESS_MAX_CONNECTIONS  (default 256) — max concurrent client connections

KEEP_EGRESS_HOST defaults to the proxy's internal-net alias, so the control port
binds the internal interface ONLY (reachable only from the paired worker, issue
#11); it falls back to all interfaces if the alias is unresolvable (local dev).
The head timeout (slowloris / half-open guard) and connection cap (excess shed
promptly) are ingress robustness only — enforcement + audit semantics unchanged.

The audit path is the PROXY's own file, separate from the worker's
`audit.jsonl` — same append-only plane, no write collision.
"""

from keep_egress.proxy import EgressProxy
from keep_egress.records import (
    EgressAuditRecord,
    EgressAuditSink,
    EgressJsonlSink,
    ObservedAgent,
)

__all__ = [
    "EgressAuditRecord",
    "EgressAuditSink",
    "EgressJsonlSink",
    "EgressProxy",
    "ObservedAgent",
]

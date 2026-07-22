# Contract: egress-observation

- **Status:** frozen v1
- **Owner:** the egress proxy component (new in Agent Keep — ADR 0002)

The observed outbound choke point. The agent container has no direct network
route out; every outbound connection is forced through a paired forward proxy
that enforces the spec's egress allowlist at runtime and writes every attempt
to the audit plane. This is the successor's core new architecture: it turns
`sandbox.egress` from a build-time declaration into a runtime boundary.

## Exposes

- A forward-proxy endpoint reachable ONLY from the paired agent container
  (private container network; never published on the host).
  - HTTP proxying and HTTPS via `CONNECT` (target observed as `host:port`;
    v1 observes connection targets, not decrypted payloads — payload-level
    inspection would be a NEW contract, never an edit here).
- **Runtime allowlist enforcement, fail-closed:** a target not matched by the
  allowlist is refused at the proxy; refusal is an observable event, not a
  silent drop.
- **Audit emission:** one record per outbound connection ATTEMPT — allowed or
  denied — in the `audit-record` v1 shape (additive record kind), carrying at
  minimum: target host:port, verdict (`allowed | denied`), matched allowlist
  entry (or none), byte counts up/down on close, and the run-correlation key
  when the attempt is attributable to a run.

## Consumes

- The egress allowlist from the baked spec (`sandbox.egress`, `host[:port]`
  entries, wildcard subdomains) — the SAME source of truth build-time
  cross-validation uses; the proxy never has its own separate list.
- The append-only audit sink (same plane the agent writes; digests-not-payloads
  discipline applies).
- Container topology guarantee from the deploy layer: the agent's network
  namespace routes outbound only via the proxy (`HTTP(S)_PROXY` env plus no
  default route — belt and suspenders; the env var alone is not the boundary).

## Schema / wire

- Proxy speaks standard HTTP proxy semantics to the agent (no custom client
  code in the agent; stdlib/httpx proxy support suffices).
- Audit record kind: `egress` with `action: connect`, fields as in Exposes.
  Exact field names freeze with the walking skeleton's first green test and are
  then additive-only.
- Denials MUST also surface to the agent as ordinary connection failures —
  the agent needs no knowledge of the proxy's existence beyond proxy env vars.

## Versioning

Frozen at **v1**. Changes are **additive only** — a breaking change is a NEW
contract, not an edit (framework-spec §4.3). Every consumer depends on this shape.

# Stage 3: Egress observation proxy: runtime allowlist + audited attempts

- **Type:** feature
- **Depends on:** 2

## Objectives

The successor's core new architecture (ADR 0002, contract
`egress-observation` v1): every outbound connection from the agent container is
forced through a paired forward proxy that enforces the spec's egress allowlist
at runtime, fail-closed, and audits every attempt — allowed and denied. This
turns `sandbox.egress` from a build-time declaration into a runtime boundary.
NOTHING like this exists in the Foundry source (verified 2026-07-22): this
stage is a build, not a transplant.

## What to build

- `packages/agent_runtime` egress-proxy component (or a small sibling package —
  builder's call, ADR-worthy only if it becomes a separately-built service):
  HTTP proxying + HTTPS `CONNECT`, allowlist matcher reusing the spec's
  `host[:port]`/wildcard grammar (same source of truth as build-time
  cross-validation — no second list), audit emission per attempt: target,
  verdict, matched entry, byte counts on close, run-correlation key when
  attributable.
- Container topology: agent + proxy as a paired composition on a private
  network; agent gets `HTTP_PROXY`/`HTTPS_PROXY` AND no default route out
  (belt and suspenders — the env var alone is not the boundary).
- Proxy audit records land in the same append-only plane (`audit-record` v1,
  additive record kind `egress`); field names freeze with this stage's first
  green test, then additive-only.
- CI: extend the container job with the DENY path — from inside the agent
  container, attempt an outbound connection to a non-allowlisted host; assert
  it is refused AND an `egress`/`denied` audit record exists. (The static
  provider makes no network calls, so CI proves deny hermetically; the ALLOW
  path is proven live in Stage 5.)

## Interface contracts

- **Exposes:** the enforced+observed egress boundary all later channels/tools
  ride through; the `egress` audit record kind the mechanic cites.
- **Consumes:** `egress-observation.md` (frozen v1 — build to it exactly),
  `agent-spec.md` (`sandbox.egress` as the only allowlist source),
  `audit-record.md` (record shape, digests-not-payloads).

## Testing requirements

- Unit: allowlist matcher (exact, port, wildcard-subdomain, deny-by-default).
- Container/integration: the DENY-path CI check above; a hermetic ALLOW-path
  test against a local in-network stub server on an allowlisted name.
- Contract test: emitted audit records validate against `audit-record` v1.

## Acceptance conditions

- [ ] Kill-switch: NONE, deliberately — the proxy is a security boundary, not a
      dark-launchable feature; a spec with an empty allowlist yields
      deny-everything, which is the safe default
- [ ] Observably-works asset authored: `scripts/smoke-egress.sh` — exec into
      the live agent container, curl a non-allowlisted host, assert refusal +
      audited denial (Operator runs it in Stage 5's live smoke)
- [ ] Additive migration only (new audit record kind is additive)
- [ ] Existing suite stays green; CI all-green

## Pipeline test: YES — extends the container job with the egress-deny check

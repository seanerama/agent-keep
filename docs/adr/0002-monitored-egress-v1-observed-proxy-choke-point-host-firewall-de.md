# 0002. Monitored egress v1: observed proxy choke point, host firewall deferred

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

The Foundry validated `sandbox.egress` at build time but the image did not
firewall itself — enforcing the perimeter was a manual deploy-side checklist
step, so the allowlist was a declaration, not a boundary. The successor brief
names "outbound through an observed choke point" as the most valuable new
architecture, and the operator confirmed the walking skeleton must include
monitored egress. The concrete v1 shape was this session's call: observed
proxy vs. host firewall + audit.

## Decision

**Proxy first, firewall later** (operator-selected, 2026-07-22):

- v1 ships an **observed forward-proxy choke point** paired with the agent
  container. All outbound traffic from the agent is forced through it (container
  network has no direct route out). The proxy:
  - enforces the spec's egress allowlist (`host[:port]`, wildcard subdomains) at
    **runtime**, fail-closed;
  - writes every connection attempt — allowed and denied — to the audit plane
    (digests-not-payloads, per the audit-record contract);
  - is part of the chassis envelope, present from the walking skeleton onward.
- The seam between agent, proxy, and audit is frozen as the **new**
  `egress-observation` v1 contract (this is new architecture, so it gets a new
  contract rather than an edit to any carried one).
- A **deploy-side host firewall** generated from the same allowlist is a later
  defense-in-depth stage, not a v1 requirement.

## Alternatives considered

- **Host firewall + audit records only:** simpler and faster, but keeps
  enforcement deploy-side — the exact gap the Foundry left open, and invisible
  to the audit plane (a firewall drop leaves no audit record).
- **Proxy only, never firewall:** rejected; a single enforcement layer inverts
  the predecessor's defense-in-depth posture. The firewall stage is deferred,
  not discarded.
- **Kernel/eBPF-level observation:** far heavier operationally than the chassis
  needs for HTTP-speaking agents; revisit only if non-HTTP egress appears.

## Consequences

- The walking skeleton gets thicker: the proxy must exist, be composed into the
  container topology, and be exercised by a real test before any feature stage.
- TLS means the proxy observes CONNECT targets (host:port), not plaintext
  bodies — "observed" v1 = every outbound connection attempt is allowlisted and
  audited, not payload inspection. Payload-level observation would be a new
  contract.
- Denied egress becomes evidence the mechanic can cite (audit records exist for
  attempts), directly serving the "every byte and token in or out, observed"
  identity.

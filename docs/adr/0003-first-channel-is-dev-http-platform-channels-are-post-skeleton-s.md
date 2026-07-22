# 0003. First channel is dev-http; platform channels are post-skeleton stages

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

The successor brief left open which channel the default chatbot speaks first.
The Foundry's proven adapters are transplant candidates: `dev_http` (localhost,
hermetic, no verification), `webex_channel` (HMAC-SHA1 verified webhook), and
`slack_channel` (HMAC-SHA256 Events API). Every adapter normalizes to the
`internal-message` contract at the boundary, so the choice does not shape the
core.

## Decision

The walking skeleton and first live chassis speak **dev-http only**
(operator-selected, 2026-07-22). The deploy target is tailnet-only (ADR 0004),
so dev-http is reachable to the operator without being public. dev-http remains
the hermetic CI channel permanently. A real platform channel (WebEx or Slack,
both transplantable in one stage) is an early post-skeleton stage, chosen at
intake time.

## Alternatives considered

- **WebEx or Slack from day one:** proven adapters exist, but each drags
  platform secrets (bot tokens, signing secrets) into the very first deploy and
  widens the egress allowlist before the observation choke point (ADR 0002) has
  proven itself. Nothing about the chassis spine needs a platform to be proven.

## Consequences

- Day-one deploy needs zero platform secrets; the only secret on the host is
  the model provider key.
- The first live chassis is operator-facing only (tailnet). Real-platform
  verification (signature checks, outbound fetches through the proxy) is
  deferred evidence — the egress proxy meets its first authenticated outbound
  platform call only when that stage lands.

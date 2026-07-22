# 0006. Ollama provider: local inference, proxied through the egress observation boundary

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

The chassis must be agnostic to who does inference (operator direction, issue
#15); Anthropic is the only implemented cloud adapter, and the `ModelProvider`
seam already supports adding more. Ollama — a local inference server (default
`:11434`, no API key) — is the first non-cloud provider, and the natural first
"real" provider to run because a local model can demonstrate the observed-egress
model without any cloud dependency.

The design question this ADR settles: the worker sits on a `--internal` no-route
network and can reach the outside ONLY through the audited egress proxy (ADR
0002). Ollama runs on the host (`3090-tuf`, RTX 3090, Ollama 0.23.1 on
`localhost:11434`). How does the worker reach it?

## Decision

**Proxied & audited** (operator-selected, 2026-07-22):

- The worker reaches the host's Ollama **through the egress proxy**, exactly like
  the cloud path. The worker's Ollama base host is `host.docker.internal:11434`;
  its `HTTP(S)_PROXY` already points at the egress proxy, so the call traverses
  the proxy.
- The spec's `sandbox.egress` allowlist contains **only** the Ollama endpoint
  (`host.docker.internal:11434`). Every model call is therefore allowlist-enforced
  and written to the proxy audit as an `egress` ALLOW record — same observability
  as Anthropic, but the endpoint is local. Nothing leaves the machine, and the
  audit proves every model call went to localhost and nowhere else.
- The proxy container gains `--add-host=host.docker.internal:host-gateway` (deploy
  topology) so it can resolve the host from its egress leg. The worker never gets
  a direct route to the host — the boundary (ADR 0002 / issue #11) is intact.
- New `ollama` provider adapter implements `ModelProvider` (httpx, Ollama
  `/api/chat`, **no API key**), mirroring the hand-rolled `anthropic` adapter.
- Additive schema on the frozen `agent-spec` contract: a new `models.ollama`
  config block and `ollama` in the provider enum — additive within the existing
  envelope (the contract permits sections growing additively; no contract edit).

## Alternatives considered

- **Co-resident Ollama container on the worker's internal network** (empty cloud
  allowlist; model traffic is an unaudited internal peer call): a valid "nothing
  leaves; empty allowlist proves it" reading, but it needs GPU passthrough into a
  container and re-pulling models, and — decisively — it puts model traffic
  OUTSIDE the observation choke point. The operator chose to keep every model
  call audited.
- **Worker reaches the host directly** (no proxy): rejected — it would give the
  worker a route off the internal network, breaking the egress boundary.

## Consequences

- The observed-egress story is maximal: even a fully-local model shows every call
  in the proxy audit, pointed at a local endpoint. Token/cost accounting is
  unchanged (the worker's own `model_call` audit records tokens for any provider).
- The proxy now legitimately reaches a host-local endpoint via the docker gateway;
  the allowlist keeps that scoped to exactly the Ollama host:port.
- Ollama has no token pricing in the same sense as a cloud API; USD pricing is
  simply omitted (local compute), while input/output token COUNTS still record.
- Adds one provider; the pattern (adapter + additive `models.<provider>` block +
  egress cross-validation for the provider host) is now the template for OpenAI /
  Google (issue #15).

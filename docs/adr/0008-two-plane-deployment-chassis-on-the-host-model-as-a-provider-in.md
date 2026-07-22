# 0008. Two-plane deployment: chassis on the host, model as a provider (incl. in-tenant GPU)

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

Some agents need GPU inference (custom/fine-tuned models, or data that must not
leave a client's environment), but only intermittently — e.g. 2–3 runs a day,
minutes each. Always-on GPU hardware is wasteful for that duty cycle, and the
agent **chassis** (worker + proxy + mechanic) is CPU-light and needs no GPU at
all. Agent Keep's provider abstraction (issue #15) already separates the *agent*
from the *model*. This ADR makes that separation the basis of deployment so the
cost/scale/GPU concerns don't distort where the security-sensitive chassis runs
(ADR 0007).

## Decision

Deployment has **two independent planes**:

- **Chassis plane** — the worker + proxy + mechanic deploy to the conformant host
  (ADR 0007). CPU-light; cheap to run always-on; no GPU. This is where the
  audited egress boundary lives.
- **Model plane** — the model is a **provider** (the existing adapter seam):
  - **Public data → a hosted API** (anthropic / openai / future vertex / bedrock).
    For 2–3 calls/day this is pennies and needs zero GPU infrastructure.
  - **Data must stay local → an in-tenant self-hosted model.** The agent reaches
    it **through the audited proxy** as an allowlisted egress target — exactly the
    Ollama-on-the-3090 pattern proven live. GPU economics for the intermittent
    duty cycle come from **on-demand / serverless GPU endpoints co-located in the
    client's tenant** (Cloud Run GPU, Azure Container Apps GPU, SageMaker async),
    not always-on GPU on the chassis host.

**Data sovereignty = both planes in the client's tenant.** The chassis on a host
in their cloud, the model on in-tenant GPU, and the proxy audit proving every
model call went to that in-tenant endpoint and nothing left.

## Alternatives considered

- **GPU on the chassis host (always-on):** couples the CPU agent to expensive GPU
  hardware and wastes it at a 2–3-runs/day duty cycle. Rejected.
- **Always a hosted API:** simplest, but impossible for data-sovereignty clients
  whose data cannot leave their environment. Rejected as the only option.
- **Chassis on serverless GPU (Cloud Run GPU etc.):** conflates the two planes and
  puts the security-sensitive chassis on a shared-namespace platform where the
  egress boundary degrades (ADR 0007). Rejected.

## Consequences

- The model plane is just the **provider catalog** (issue #15): a serverless GPU
  endpoint that speaks the OpenAI API can be reached by the existing `openai`
  adapter pointed at an in-tenant `baseHost`; a bespoke endpoint is a new adapter
  in the same additive pattern. No new deployment machinery for the model.
- The deployment-target abstraction (ADR 0007) concerns ONLY the chassis — a clean
  separation of concerns.
- The egress allowlist is where in-tenant systems, VPN-routed targets, and the
  model endpoint are all declared and audited — the agent reaching internal
  systems is additional `sandbox.egress` entries, observed like everything else.
- Provisioning an in-tenant GPU endpoint is a per-cloud, on-demand convenience
  (like the chassis provisioners in ADR 0007), not part of the core.

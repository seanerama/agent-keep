# Stage 8: Ollama provider (proxied-local): adapter, additive schema, deploy topology, hermetic test

- **Type:** feature
- **Depends on:** none

## Objectives

Add `ollama` as a first-class model provider (ADR 0006, issue #15 first cut): a
local inference server reached by the worker THROUGH the egress proxy, so every
model call is allowlist-enforced and audited exactly like the cloud path, but the
endpoint is local. Proves the chassis is provider-agnostic and that a fully-local
model still rides the observed-egress choke point. No API key.

## What to build

- **Adapter** `packages/agent_runtime/src/agent_runtime/components/ollama_provider.py`
  implementing the `ModelProvider` interface, mirroring `anthropic_provider.py`
  (httpx, hand-rolled, no SDK): POST to Ollama `POST /api/chat`
  (`{model, messages, stream:false}`), map the response to the runtime's
  message/usage shape, record token counts from Ollama's `prompt_eval_count` /
  `eval_count` into the usage the audit plane reads. **No API key.** Base URL from
  config (default `http://host.docker.internal:11434`). Honor `maxTokens` via
  Ollama `options.num_predict` if set. Reuse the anthropic adapter's timeout/error
  shapes; raise clear typed errors on non-200 / malformed.
- **Additive schema** (`packages/keep_spec/src/keep_spec/models.py`): a new
  `OllamaProviderConfig(StrictModel)` — `model: str` (min_length 1), `baseHost:
  str` (default `host.docker.internal:11434`, validated as a `host[:port]` — reuse
  the egress host grammar), optional `maxTokens`, optional `pricing` (usually
  omitted — local compute). Add `"ollama"` to the provider `Literal` in `Models`
  and `ModelTier`; add the `ollama: OllamaProviderConfig | None` field to both;
  extend `_check_provider_config` to include ollama. Additive only — the
  `agent-spec` contract permits new `models.<provider>` blocks; do NOT edit
  `contracts/`.
- **Wiring** (`wiring.py`): add `"ollama": "ollama-provider"` to
  `_PROVIDER_COMPONENTS`; register `"ollama-provider": "ollama_provider"` in the
  component map; add egress cross-validation so a spec selecting `ollama` must
  have the ollama `baseHost` in `sandbox.egress` (mirror the ANTHROPIC_API_HOST
  cross-check at wiring.py:466-510, but the host comes from the ollama config, not
  a constant).
- **Composer** (`keep_build`): include `ollama_provider` in the image when the
  spec selects it (absence composition — mirror how anthropic_provider is
  included).
- **Deploy topology** (`deploy/systemd/agent-keep@.service`): add
  `--add-host=host.docker.internal:host-gateway` to the **egress-proxy** container
  run so the proxy can resolve the host from its egress leg (ADR 0006). Worker
  gets no new route. Keep everything else identical.
- **Live spec variant** `specs/default-chatbot.ollama.yaml` (`keep/v1`):
  `provider: ollama`, `models.ollama.model: llama3.2:latest`, `baseHost:
  host.docker.internal:11434`; `sandbox.egress: [host.docker.internal:11434]`
  (ONLY); dev-http, jsonl audit, token accounting on. Validates via keep_spec.

## Interface contracts

- **Consumes:** `agent-spec.md` (additive `models.ollama` — permitted, no edit),
  `egress-observation.md` (the ollama host is allowlisted; calls audited),
  `audit-record.md` (model_call + tokens unchanged).
- **Exposes:** the `ollama` provider option; the template for future providers.

## Testing requirements

- **Hermetic (CI, `-m "not container"`):** a stub Ollama HTTP server (stdlib, like
  the egress test's stub) returning an `/api/chat` shape; assert the adapter posts
  the right body, parses the reply, and reports token counts. Schema tests: the
  ollama spec validates; provider/config cross-checks (ollama selected without
  config → error; config for unselected provider → error). Wiring test: ollama
  selected but baseHost NOT in egress → cross-validation error.
- **Container (`-m container`):** build an image from the ollama spec; assert
  `ollama_provider` present, absence of unselected providers; the worker boots.
  (A real model reply is NOT hermetic — that's the live test, below.)
- The live test (real llama3.2 on 3090-tuf, through the proxy, egress ALLOW record
  for host.docker.internal:11434) is the Operator's post-merge step.

## Acceptance conditions

- [ ] Kill-switch: provider choice is per-spec; the ollama image is only run where
      deployed (dark-launch by absence — no runtime flag, consistent with the
      chassis)
- [ ] Observably-works asset: reuse `smoke-chat.sh` (a real reply) + `smoke-egress`
      shows the ALLOW record for the ollama host; both run in the live step
- [ ] Additive only — `contracts/` untouched; provider enum + config are additive;
      existing static/anthropic specs still validate and build
- [ ] Existing suite stays green; CI all-green

## Pipeline test: YES — the container job builds + boots the ollama-spec image

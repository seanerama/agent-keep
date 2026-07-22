# Stage 10: OpenAI provider: cloud adapter, additive schema, deployable variant

- **Type:** feature
- **Depends on:** none (rides the established provider pattern: ADR 0006 / stage 8 ollama)

## Objectives

Add `openai` as a model provider (issue #15, second adapter). A cloud provider
like `anthropic` — needs an API key and egress to `api.openai.com` — implemented
against the same `ModelProvider` seam that ollama validated. No new architecture
decision; it is the anthropic-shaped cloud variant of the stage-8 pattern.

## What to build

- **Adapter** `packages/agent_runtime/src/agent_runtime/components/openai_provider.py`
  implementing `ModelProvider`, mirroring `anthropic_provider.py` (hand-rolled
  httpx, no SDK): `POST https://{baseHost}/v1/chat/completions` with header
  `Authorization: Bearer <key>` (key read from the config's `apiKeyEnv` env var —
  NEVER the value in the spec). Body `{model, messages, max_tokens?}`; map runtime
  history → OpenAI `messages` (system/user/assistant/tool → roles; keep the
  anthropic adapter's tool-turn handling shape). Parse
  `.choices[0].message.content`; tokens from `.usage.prompt_tokens` →
  `tokens_in`, `.usage.completion_tokens` → `tokens_out`, into the SAME
  `ProviderReply` fields anthropic/ollama return (study those two + provider.py
  and match EXACTLY so the audit/cost path is identical). Like anthropic, require
  the key at construction — raise the `MissingApiKeyError`-equivalent if the
  env var is empty (eager provider build at boot). `self.name = f"openai:{model}"`.
  Typed `OpenAIRequestError` (5xx/transport retried with capped backoff, 4xx
  hard-fails), mirroring anthropic. mypy strict.

- **Additive schema** (keep_spec/models.py): `OpenAIProviderConfig(StrictModel)` —
  `model: str` (min_length 1), `baseHost: str` (default `api.openai.com:443`,
  validated against the reused `EGRESS_HOST` host[:port] grammar — configurable so
  OpenAI-compatible endpoints work and the egress cross-check reads it), `apiKeyEnv:
  str` (default `OPENAI_API_KEY`, pattern `ENV_VAR_NAME`), optional `maxTokens`,
  optional `pricing`. Add `"openai"` to the provider `Literal` in BOTH `Models` and
  `ModelTier`; add `openai: OpenAIProviderConfig | None` to both; extend
  `_check_provider_config` (and both call sites) to include openai. Additive only —
  do NOT edit `contracts/`.

- **Wiring** (wiring.py): `"openai": "openai-provider"` in `_PROVIDER_COMPONENTS`;
  register the module; add egress cross-validation reading the host from the
  SELECTED openai config's `baseHost` (mirror the ollama `_ollama_egress_targets`
  approach the stage-8 work added — per site/tier, not a constant).

- **Composer** (keep_build): include `openai_provider` (+ its httpx dep marker)
  when the spec selects openai — mirror anthropic/ollama inclusion.

- **Runner** (runner.py): `_build_provider` constructs `OpenAIProvider` for
  `provider == "openai"` (base_url from baseHost, api key env from config); mirror
  the ollama/anthropic branches.

- **Deployable variant**: `specs/default-chatbot.openai.yaml` (`keep/v1`): provider
  openai, `models.openai.model` a small current model (use `gpt-4o-mini`),
  `baseHost api.openai.com:443`, `apiKeyEnv OPENAI_API_KEY`; `sandbox.egress:
  [api.openai.com:443]` ONLY; dev-http, jsonl audit, token accounting on. Validates
  via keep_spec. Extend the CI publish job (mirror stage 9's ollama step) to build
  + push this as `:openai-edge` + `:openai-${GITHUB_SHA}` (explicit --tag; same
  slug; never `:latest`).

## Interface contracts

- **Consumes:** `agent-spec.md` (additive `models.openai` — permitted, no edit),
  `egress-observation.md` (openai host allowlisted + audited), `audit-record.md`
  (model_call + tokens unchanged). The KEEP_DEPLOY_SECRETS mechanism (stage 7)
  already injects OPENAI_API_KEY — no deploy change needed for the key.

## Testing requirements

- **Hermetic (CI):** stdlib stub HTTP server returning an OpenAI chat-completions
  shape; assert the adapter posts the right body + Bearer header, parses
  `.choices[0].message.content`, reports usage token counts; a non-200 → typed
  error; **missing key → the MissingApiKey error at construction** (mirror
  anthropic's test).
- **Schema tests:** openai spec validates; openai-selected-without-config → error;
  config-for-unselected-provider → error; bad baseHost grammar → error.
- **Wiring test:** openai selected but baseHost absent from egress → cross-validation
  error; present → ok.
- **Container (`-m container`):** build the openai-spec image with a distinct
  `--tag`; assert openai_provider present, unselected providers absent, worker
  boots (the openai key is NOT needed at image build; the boot test must supply a
  dummy `OPENAI_API_KEY` env so construction succeeds — mirror the anthropic boot
  test's handling).

## Acceptance conditions

- [ ] Kill-switch: per-spec provider choice; dark-launch by absence (no runtime flag)
- [ ] Additive only — `contracts/` untouched; static/anthropic/ollama specs still
      validate + build; provider enum + config additive
- [ ] No API key VALUE anywhere in git (env NAME only); key read from apiKeyEnv at
      runtime
- [ ] Existing suite stays green; CI all-green

## Pipeline test: YES — the container job builds + boots the openai-spec image

## Note (live test)

Going live needs the operator's OPENAI_API_KEY and real egress to api.openai.com
(billed) — that is a later operator step via
`printf 'OPENAI_API_KEY=%s\n' "$KEY" | KEEP_DEPLOY_SECRETS=1
KEEP_SPEC_FILE=specs/default-chatbot.openai.yaml ./deploy.sh default-chatbot
openai-edge`. Not part of this stage's acceptance (hermetic CI only).

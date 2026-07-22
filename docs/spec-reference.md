# keep/v1 AgentSpec — field reference

GENERATED from the `keep_spec` Pydantic models — do not edit by hand.
Regenerate with `uv run python -m keep_spec.reference_export docs/spec-reference.md`.

Envelope contract: `contracts/agent-spec.md` (frozen v1; strict validation —
unknown fields are an error). Decision coverage: every agent-level decision of
ADR 0003 maps to fields below (`keep_spec.decision_coverage`).

## AgentSpec

The keep/v1 AgentSpec envelope.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `apiVersion` | one of: `keep/v1` | yes | — |  |
| `kind` | one of: `AgentSpec` | yes | — |  |
| `metadata` | [Metadata](#metadata) | yes | — |  |
| `spec` | [SpecSections](#specsections) | yes | — |  |

## Metadata

`metadata` — identity of the spec document itself.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | str | yes | — | Human-readable agent name. (min_length: `1`) |
| `slug` | str | yes | — | Kebab-case slug; the image becomes ghcr.io/<owner>/agent-keep-<slug>. (pattern: `^[a-z0-9]+(-[a-z0-9]+)*$`) |
| `description` | str | yes | — | One-line description. (min_length: `1`) |
| `specVersion` | str | yes | — | Semver of THIS document, bumped on any edit. (pattern: `^\d+\.\d+\.\d+$`) |

## SpecSections

`spec` — the agent-level sections (contract agent-spec schema block). Sections required by the walking skeleton stay required; every section and field added since stage 1 is optional or defaulted (additive, rule 4).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `persona` | [Persona](#persona) | yes | — |  |
| `triggers` | [Triggers](#triggers) or null | no | `null` | What activates the agent; absent = message-only. |
| `channels` | list of [DevHttpChannel](#devhttpchannel) or [DiscordChannel](#discordchannel) or [SlackChannel](#slackchannel) or [WebexChannel](#webexchannel) or [SmsChannel](#smschannel) | yes | — | (min_length: `1`) |
| `gateway` | [Gateway](#gateway) | yes | — |  |
| `sessions` | [Sessions](#sessions) | yes | — |  |
| `memory` | [Memory](#memory) or null | no | `null` | Long-term memory; absent = none built (absence semantics). |
| `skills` | list of [SkillPack](#skillpack) | no | `[]` | Instruction packs enabled for this agent; absent tools/skills are absent. |
| `tools` | list of [McpServer](#mcpserver) | no | `[]` | Per-agent MCP server allowlist; empty = no external tools in the image. |
| `approval` | [Approval](#approval) | yes | — |  |
| `sandbox` | [Sandbox](#sandbox) | yes | — |  |
| `models` | [Models](#models) | yes | — |  |
| `observability` | [Observability](#observability) | yes | — |  |
| `persistence` | [Persistence](#persistence) | yes | — |  |

## Persona

`spec.persona` — identity, tone, standing instructions, and where personalization lives (blueprint `core/persona`, decision "Where does personalization live?": static config / learned memory / both with clear precedence).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `identity` | str | yes | — | Who the agent is (system-prompt identity). (min_length: `1`) |
| `tone` | str or null | no | `null` | Voice/tone guidance merged into the system prompt. |
| `instructions` | list of str | no | `[]` | Standing instructions, merged deterministically after identity and tone. |
| `source` | one of: `static`, `learned`, `both` | no | `"static"` | Where personalization lives (blueprint core/persona): 'static' config in this spec, 'learned' memory the agent updates, or 'both'. Learned-persona writes are privileged, audited memory writes (see spec.memory.writePolicy). |
| `precedence` | one of: `static-over-learned`, `learned-over-static` or null | no | `null` | Conflict rule when source is 'both' (blueprint core/persona: 'Both, with clear precedence'). Required iff source is 'both'. |

## Triggers

`spec.triggers` — what activates the agent (ADR 0003 addition: message / schedule / event subscription). Absent section = message-only.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `activations` | list of [MessageTrigger](#messagetrigger) or [ScheduleTrigger](#scheduletrigger) or [EventTrigger](#eventtrigger) | no | `[{"kind": "message"}]` | Exhaustive positive list of activations; default is message-only. (min_length: `1`) |

## MessageTrigger

A human message activates the agent (ADR 0003 addition: triggers). The skeleton's implicit behavior, made declarable.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | one of: `message` | yes | — | Trigger kind discriminator. |

## ScheduleTrigger

Cron-scheduled activation (ADR 0003 addition: triggers / schedule). v1 clarification note (#10, stage 22 — the stage-2/5 format-clarification procedure under contract rule 4): the original `cron` pattern validated token COUNT only, so `never gonna give you up` passed. Real five-field validation (`keep_spec.cron.parse_cron`) pins the always-intended value space — an executable cron expression — now that the schedule trigger has a runtime component. Every in-repo spec's cron string was already valid.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | one of: `schedule` | yes | — | Trigger kind discriminator. |
| `cron` | str | yes | — | Five-field cron expression (minute hour day-of-month month day-of-week), evaluated in UTC. Per field: '*', numbers, ranges (N-M), comma lists, and '/step' on '*' or a range; day-of-week 0-7 (0 and 7 both Sunday). Exactly five fields — @names are not supported. (pattern: `^\s*[0-9*,/-]+(\s+[0-9*,/-]+){4}\s*$`) |
| `prompt` | str | yes | — | Instruction delivered to the agent when the schedule fires. (min_length: `1`) |

## EventTrigger

Event-subscription activation (ADR 0003 addition: triggers / event subscription) — e.g. the worked example's alarm-driven outage agent. `secretEnv` (v1 additive amendment, stage 18): the event-intake endpoint fails closed on a shared secret, and the spec names the env var holding it — the same spec-honesty posture as `ChannelVerification.secretEnv` (the value is deploy-time only, never in the spec or image; contract rule 3). Defaulted so every pre-amendment spec validates and behaves unchanged.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | one of: `event-subscription` | yes | — | Trigger kind discriminator. |
| `source` | str | yes | — | Event source the agent subscribes to (e.g. 'alertmanager'). (min_length: `1`) |
| `event` | str or null | no | `null` | Event name/type filter within the source; None = all events. |
| `prompt` | str or null | no | `null` | Instruction delivered with the event payload; None = payload only. |
| `secretEnv` | str | no | `"EVENT_WEBHOOK_SECRET"` | Env var NAME holding the shared secret that authenticates event deliveries to the intake endpoint (never the value — contract rule 3). v1 additive amendment, stage 18. (pattern: `^[A-Z][A-Z0-9_]*$`) |

## DevHttpChannel

`spec.channels[]` dev-http — localhost HTTP adapter, zero platform deps (blueprint `channels/adapters`; transport option: webhooks — a plain HTTP endpoint; no verification, honestly unverified).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `type` | one of: `dev-http` | yes | — | Channel adapter component. |
| `port` | int | no | `8000` | TCP port the adapter listens on. (ge: `1`; le: `65535`) |
| `transport` | one of: `webhook` | no | `"webhook"` | Delivery transport (blueprint channels/adapters): local HTTP endpoint. |

## DiscordChannel

`spec.channels[]` discord (blueprint `channels/adapters`; transport options: websocket gateway (Discord) / polling (fallback)).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `type` | one of: `discord` | yes | — | Channel adapter component. |
| `transport` | one of: `websocket`, `polling` | no | `"websocket"` | Delivery transport (blueprint channels/adapters): outbound-only websocket gateway (firewall-friendly) or polling fallback. |
| `verification` | [ChannelVerification](#channelverification) | no | `{"method": "token", "secretEnv": "DISCORD_BOT_TOKEN"}` | Bot-token verification; names the token env var. |

## ChannelVerification

Inbound verification for a platform channel (blueprint `gateway/identity`, responsibility: verify webhook signatures / bot token scopes). Names the verifying secret's env var; values never appear in the spec (contract agent-spec, binding rule 3).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `method` | one of: `signature`, `token`, `none` | yes | — | 'signature' = platform-signed payloads (webhook signing secret); 'token' = authenticated bot session (bot token); 'none' = unverified (dev only). |
| `secretEnv` | str or null | no | `null` | Env var NAME holding the verifying secret (never the value). (pattern: `^[A-Z][A-Z0-9_]*$`) |

## SlackChannel

`spec.channels[]` slack (blueprint `channels/adapters`; transport options: webhooks (Slack) / websocket (Socket Mode) / polling (fallback)).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `type` | one of: `slack` | yes | — | Channel adapter component. |
| `transport` | one of: `webhook`, `websocket`, `polling` | no | `"webhook"` | Delivery transport (blueprint channels/adapters): webhook (Events API, needs a public HTTPS endpoint), websocket (Socket Mode), or polling fallback. |
| `verification` | [ChannelVerification](#channelverification) | no | `{"method": "signature", "secretEnv": "SLACK_SIGNING_SECRET"}` | Request-signature verification; names the signing-secret env var. |

## WebexChannel

`spec.channels[]` webex (blueprint `channels/adapters`; transport options: webhooks (WebEx) / polling (fallback)).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `type` | one of: `webex` | yes | — | Channel adapter component. |
| `transport` | one of: `webhook`, `polling` | no | `"webhook"` | Delivery transport (blueprint channels/adapters). |
| `verification` | [ChannelVerification](#channelverification) | no | `{"method": "signature", "secretEnv": "WEBEX_WEBHOOK_SECRET"}` | Webhook-signature verification; names the secret env var. |

## SmsChannel

`spec.channels[]` sms (blueprint `channels/adapters`; transport options: webhooks (SMS gateway callback) / polling (fallback)).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `type` | one of: `sms` | yes | — | Channel adapter component. |
| `transport` | one of: `webhook`, `polling` | no | `"webhook"` | Delivery transport (blueprint channels/adapters). |
| `verification` | [ChannelVerification](#channelverification) | no | `{"method": "signature", "secretEnv": "SMS_WEBHOOK_SECRET"}` | Gateway-signature verification; names the secret env var. |

## Gateway

`spec.gateway` — the control plane: who gets in and how work is queued (blueprint `gateway/identity` and `gateway/queue`).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `queue` | one of: `in-process`, `redis` | yes | — | Queue weight (blueprint gateway/queue, decision 'How heavy should the queue be?'): asyncio.Queue in-process, or Redis (persistence + pub/sub). |
| `concurrency` | one of: `serial`, `concurrent-locked` | no | `"serial"` | Per-conversation handling (blueprint gateway/queue, decision 'Serial or concurrent handling per conversation?'): strict serial per session, or concurrent with locking. |
| `allowlist` | [GatewayAllowlist](#gatewayallowlist) or null | no | `null` | Access-control policy (blueprint gateway/identity). Absent = no identity layer (dev-only; the skeleton's unverified dev-http channel). |
| `identityUnification` | one of: `manual-link`, `challenge`, `separate` | no | `"separate"` | How one human is unified across channels (blueprint gateway/identity, decision 'How do you unify one human across channels?'): manual linking table, verification challenge, or keep identities separate. |

## GatewayAllowlist

Who may talk to the agent (blueprint `gateway/identity`, decision "Who is allowed to talk to the agent?": owner-only allowlist / pairing code flow / open with tiered permissions).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `policy` | one of: `owner-only`, `pairing`, `tiered` | yes | — | Allowlist policy (blueprint gateway/identity): 'owner-only', 'pairing' (code flow for inviting users), or 'tiered' (open with tiered permissions). |
| `roster` | list of [AllowlistEntry](#allowlistentry) | no | `[]` | Statically-declared principals; pairing may add more at runtime. |

## AllowlistEntry

One allowed principal (blueprint `gateway/identity`).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | str | yes | — | Platform-scoped principal id, e.g. 'discord:1002003004' or 'sms:+15550100'. (min_length: `1`) |
| `tier` | one of: `owner`, `trusted`, `guest` | no | `"trusted"` | Access tier (blueprint gateway/identity: tiered access strangers vs owner). |

## Sessions

`spec.sessions` — session definition and lifecycle (blueprint `core/sessions`).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `mode` | one of: `single` | yes | — | Stage-1 session manager component. Skeleton subset: single. |
| `definition` | one of: `per-channel`, `per-user`, `hybrid` or null | no | `null` | What a session IS (blueprint core/sessions, decision 'What is a session, exactly?'): per channel-conversation, per user (shared across channels), or hybrid (shared memory, separate transcripts). Absent = the skeleton's single session. |
| `history` | [SlidingWindowHistory](#slidingwindowhistory) or [SummarizationHistory](#summarizationhistory) or [RetrievalHistory](#retrievalhistory) or [LayeredHistory](#layeredhistory) or null | no | `null` | How history fits the context window (blueprint core/sessions, decision 'How do you fit history into the context window?'): sliding-window / summarization / retrieval / layered. Absent = whole-session history (skeleton). |

## SlidingWindowHistory

History strategy: drop oldest turns (blueprint `core/sessions`).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `strategy` | one of: `sliding-window` | yes | — | History strategy discriminator. |
| `maxTurns` | int | no | `50` | Turns kept before the oldest is dropped. (ge: `1`) |

## SummarizationHistory

History strategy: rolling summarization (blueprint `core/sessions`).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `strategy` | one of: `summarization` | yes | — | History strategy discriminator. |
| `summarizeAfterTurns` | int | no | `20` | Turns accumulated before a summarization pass. (ge: `1`) |

## RetrievalHistory

History strategy: retrieval of relevant past turns (blueprint `core/sessions`).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `strategy` | one of: `retrieval` | yes | — | History strategy discriminator. |
| `topK` | int | no | `5` | Past turns retrieved per request. (ge: `1`) |

## LayeredHistory

History strategy: window + periodic summary + retrieval — the blueprint's 'most real systems layer them' (blueprint `core/sessions`).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `strategy` | one of: `layered` | yes | — | History strategy discriminator. |
| `windowTurns` | int | no | `20` | Recent turns kept verbatim. (ge: `1`) |
| `summarize` | bool | no | `true` | Roll older turns into a summary. |
| `retrievalTopK` | int | no | `5` | Past turns retrieved per request. (ge: `1`) |

## Memory

`spec.memory` — long-term memory (blueprint `core/memorysys`). Absent section = no durable memory beyond the session (absence semantics, rule 2).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `structure` | [FactsMemory](#factsmemory) or [VectorMemory](#vectormemory) or [LayeredMemory](#layeredmemory) | yes | — | Structured memory or embeddings-first (blueprint core/memorysys, decision 'Structured memory or embeddings-first?'): facts / vectors / layered. |
| `writePolicy` | one of: `user-command`, `agent-autonomous`, `off` | no | `"user-command"` | Who may write memory (blueprint core/memorysys considerations: 'define who may write memory — that's a trust decision'): explicit user command, the agent autonomously, or nobody (read-only). Agent-autonomous writes are privileged tool calls — always audited (blueprint core/persona considerations). |

## FactsMemory

Memory structure: structured facts — key-value / markdown files, human- auditable (blueprint `core/memorysys`, option 'Structured facts').

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | one of: `facts` | yes | — | Memory structure discriminator. |
| `store` | one of: `none` | no | `"none"` | No vector store (blueprint core/memorysys, decision 'Which vector store?'): facts live in the persistence tier. |

## VectorMemory

Memory structure: vector store over transcripts (blueprint `core/memorysys`, option 'Vector store over transcripts'; `corpus` scopes what gets embedded per the memorysys retrieval responsibility).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | one of: `vectors` | yes | — | Memory structure discriminator. |
| `store` | one of: `sqlite-vec`, `pgvector` | yes | — | Vector store choice (blueprint core/memorysys, decision 'Which vector store, when you get there?'): SQLite + sqlite-vec or Postgres + pgvector. |
| `corpus` | one of: `agent-summaries`, `transcripts`, `documents` or null | no | `null` | What gets embedded (blueprint core/memorysys, retrieval responsibility: 'embed and search past conversations and documents'): 'agent-summaries' (agent-written summaries), 'transcripts' (raw conversation transcripts), or 'documents'. None = the structure's default corpus (transcripts). |

## LayeredMemory

Memory structure: facts + vectors (blueprint `core/memorysys`, option 'Layered: facts + vectors'; `corpus` scopes what gets embedded per the memorysys retrieval responsibility).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | one of: `layered` | yes | — | Memory structure discriminator. |
| `store` | one of: `sqlite-vec`, `pgvector` | yes | — | Vector store choice for the vector layer (blueprint core/memorysys): SQLite + sqlite-vec or Postgres + pgvector. |
| `corpus` | one of: `agent-summaries`, `transcripts`, `documents` or null | no | `null` | What gets embedded (blueprint core/memorysys, retrieval responsibility: 'embed and search past conversations and documents'): 'agent-summaries' (agent-written summaries), 'transcripts' (raw conversation transcripts), or 'documents'. None = the structure's default corpus (transcripts). |

## SkillPack

One entry of `spec.skills` — an instruction pack (knowledge, never code; factory-level ADR 0003 item 5 fixes the skill definition) with its selection strategy (blueprint `capabilities/skills`, decision 'How are skills selected per request?').

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | str | yes | — | Skill pack id in the skill registry. (pattern: `^[a-z0-9]+(-[a-z0-9]+)*$`) |
| `version` | str or null | no | `null` | Version pin (blueprint capabilities/skills: pin what production agents use). |
| `selection` | one of: `always`, `keyword`, `model-driven` | no | `"always"` | Selection strategy (blueprint capabilities/skills): 'always' in prompt, 'keyword' (keyword/intent triggering), or 'model-driven' (descriptions in prompt, bodies on demand). |
| `keywords` | list of str | no | `[]` | Trigger keywords; required iff selection is 'keyword'. |

## McpServer

One entry of `spec.tools` — an MCP server attached to THIS agent (blueprint `capabilities/mcpmgr`, decision 'Which MCP servers get attached to which agent?', option 'Per-agent allowlist'). The `spec.tools` list IS the per-agent allowlist; anything not listed is absent from the image (absence semantics, rule 2).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | str | yes | — | Server id, used to namespace its tools. (pattern: `^[a-z0-9]+(-[a-z0-9]+)*$`) |
| `transport` | [StdioTransport](#stdiotransport) or [HttpTransport](#httptransport) or [LocalTransport](#localtransport) | yes | — | stdio child process or remote HTTP. |
| `allow` | list of [ToolGrant](#toolgrant) | yes | — | Exhaustive positive per-tool allowlist with scopes; a server with no grants may not be attached. (min_length: `1`) |
| `secretEnvs` | list of str | no | `[]` | Env var NAMES the server needs (never values — contract rule 3); injected at runtime, never visible to the model. |

## StdioTransport

MCP server as a local child process (blueprint `capabilities/mcpmgr`, transport option 'stdio'; the factory supports both transports behind one interface — ADR 0003 item 4).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | one of: `stdio` | yes | — | MCP transport discriminator. |
| `command` | str | yes | — | Executable to spawn. (min_length: `1`) |
| `args` | list of str | no | `[]` | Arguments for the command. |

## HttpTransport

MCP server over streamable HTTP (blueprint `capabilities/mcpmgr`, transport option 'Streamable HTTP (remote/shared)').

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | one of: `http` | yes | — | MCP transport discriminator. |
| `url` | str | yes | — | Base URL of the remote MCP server. (pattern: `^https?://\S+$`) |

## LocalTransport

Tool server backed by the runtime's in-process local tool registry (stage-6 v1 additive amendment — a new transport enum value under contract rule 4). No external process, no network: the grants select from the `local_tools` component's registry of harmless demo tools, executed by the same constraint-enforcing tool executor MCP transports plug into (stage 7).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `kind` | one of: `local` | yes | — | Tool transport discriminator. |

## ToolGrant

One allowed tool on an MCP server (blueprint `capabilities/mcpmgr`: 'tool availability IS the permission model'). `constraints` (v1 additive amendment, stage 4) narrows a grant further: hard parameter pins the runtime tool executor MUST enforce (e.g. `room: noc-outages` on a paging tool). No constraint-enforcing executor exists yet, so any grant carrying constraints fails the buildable-check loudly (same fail-loud pattern as egress/approval).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | str | yes | — | Tool name as exposed by the MCP server. (min_length: `1`) |
| `scope` | one of: `read-only`, `read-write` | no | `"read-only"` | Grant scope (blueprint capabilities/mcpmgr considerations: 'the public-facing agent gets read-only tools'). Default read-only — least privilege. |
| `constraints` | mapping of str to str or int or bool or null | no | `null` | Hard parameter pins the tool executor MUST enforce: constraint name -> validated scalar (identifier-ish string, int, or bool — never free prose), e.g. pinning a paging tool to `room: noc-outages`. None = no pins. |

## Approval

`spec.approval` — what requires human confirmation (blueprint `capabilities/executor`, decision 'What requires human approval?').

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `policy` | one of: `autonomous`, `allowlist-confirm-rest`, `everything` | no | `"allowlist-confirm-rest"` | Approval policy (blueprint capabilities/executor): 'autonomous' (nothing confirmed), 'allowlist-confirm-rest' (auto-approve the allowlist, confirm the rest — default-deny), or 'everything' (everything confirmed). Default matches the blueprint's 'default-deny with a growing auto-approve list'. |
| `autoApprove` | list of str | no | `[]` | Auto-approved tool names ('<server>.<tool>'); only meaningful with policy 'allowlist-confirm-rest'. Every entry must name a grant declared in spec.tools (cross-validated at the envelope level). |

## Sandbox

`spec.sandbox` — execution isolation + network egress (blueprint `capabilities/executor`, decision 'How isolated is code/shell execution?'; egress is ADR 0003's second addition). v1 clarification note: `egress` entries were an unconstrained `list[str]` in the stage-1 schema. Host[:port] format validation was added in stage 2, while the field had exactly one consumer (the walking skeleton's empty list), as a format clarification of the always-intended value space — a network egress allowlist of hosts — not a v1 break.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `profile` | one of: `same-process`, `restricted-user`, `container` | no | `"container"` | Tool-execution isolation (blueprint capabilities/executor): 'same-process' (same process/user — the blueprint says don't), 'restricted-user' (dedicated unprivileged user), or 'container' (container/VM per execution). Default container — every built agent is already a container (ADR 0003 item 8). |
| `egress` | list of str | no | `[]` | Network egress allowlist of host[:port] entries (ADR 0003 addition: egress). Exhaustive positive declaration; default EMPTY — nothing else is reachable by construction. |

## Models

`spec.models` — model routing, providers, and budgets (blueprint `model/llmrouter`). 'static' is a first-class provider (ADR 0004).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `provider` | one of: `static`, `anthropic`, `ollama`, `openai` | yes | — | Default provider when no tier routing applies (blueprint model/llmrouter). |
| `static` | [StaticProviderConfig](#staticproviderconfig) or null | no | `null` | Static provider config; required iff provider is 'static'. |
| `anthropic` | [AnthropicProviderConfig](#anthropicproviderconfig) or null | no | `null` | Anthropic provider config; required iff provider is 'anthropic'. |
| `ollama` | [OllamaProviderConfig](#ollamaproviderconfig) or null | no | `null` | Ollama provider config; required iff provider is 'ollama'. |
| `openai` | [OpenAIProviderConfig](#openaiproviderconfig) or null | no | `null` | OpenAI provider config; required iff provider is 'openai'. |
| `tiers` | list of [ModelTier](#modeltier) | no | `[]` | Routing tiers by task type (blueprint model/llmrouter: cheap model for triage, flagship for reasoning). Empty = single default provider. |
| `budgets` | [ModelBudgets](#modelbudgets) or null | no | `null` | Per-session cost control (blueprint model/llmrouter); None = no budgets. |

## StaticProviderConfig

Configuration for the `static` model provider (ADR 0004 — deterministic, hermetic, first-class).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `script` | list of str | yes | — | Deterministic scripted replies; call N returns script[N % len(script)]. (min_length: `1`) |

## AnthropicProviderConfig

Configuration for the `anthropic` model provider (blueprint `model/llmrouter` — provider adapters behind one interface).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `model` | str | yes | — | Model name, e.g. 'claude-sonnet-4-5'. (min_length: `1`) |
| `apiKeyEnv` | str | no | `"ANTHROPIC_API_KEY"` | Env var NAME holding the API key (never the value — contract rule 3). (pattern: `^[A-Z][A-Z0-9_]*$`) |
| `maxTokens` | int or null | no | `null` | Max output tokens per model call (Messages API max_tokens); None = the adapter default (4096). Ceiling 128000 matches the largest current model output cap. v1 additive amendment, stage 13. (ge: `1`; le: `128000`) |
| `pricing` | [Pricing](#pricing) or null | no | `null` | Operator-declared token pricing for this model path; required on every selectable path iff budgets.maxUsdPerSession is set (cross-validated). None = no pricing declared. v1 additive amendment, stage 25. |

## Pricing

Operator-declared token pricing for one model path (blueprint `model/llmrouter`, decision 'Where does cost control live?'). There is NO library price table: prices drift and a stale table silently mis-enforces, so the OPERATOR declares pricing in the spec, right next to the model it prices, and `budgets.maxUsdPerSession` enforces against these exact numbers (honest, auditable, no hidden knobs — ADR 0003 spirit). Both rates are required together (both-or-neither): omit the `pricing` block entirely for no pricing. v1 additive amendment, stage 25.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `usdPerMillionInputTokens` | float or null | no | `null` | USD charged per 1,000,000 input (prompt) tokens for this model path. (gt: `0`) |
| `usdPerMillionOutputTokens` | float or null | no | `null` | USD charged per 1,000,000 output (completion) tokens for this model path. (gt: `0`) |

## OllamaProviderConfig

Configuration for the `ollama` model provider (ADR 0006 — local inference reached THROUGH the audited egress proxy, no API key). The worker's Ollama base host is `host.docker.internal:11434` by default: the host's Ollama server, reached over the docker gateway via the egress proxy (the worker never routes there directly). No `apiKeyEnv` — Ollama takes no key. `pricing` is usually omitted (local compute has no per-token USD cost in the cloud-API sense); token COUNTS still record. v1 additive amendment, stage 8 (issue #15 first cut).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `model` | str | yes | — | Ollama model name, e.g. 'llama3.2:latest'. (min_length: `1`) |
| `baseHost` | str | no | `"host.docker.internal:11434"` | Ollama server host[:port] the worker reaches THROUGH the egress proxy (cross-validated against sandbox.egress). Same host[:port] grammar as sandbox.egress; default 'host.docker.internal:11434' (the host's Ollama over the docker gateway — ADR 0006). |
| `maxTokens` | int or null | no | `null` | Max output tokens per model call (Ollama options.num_predict); None = the adapter default (Ollama's own num_predict default). Ceiling 128000 matches the anthropic config's convention. (ge: `1`; le: `128000`) |
| `pricing` | [Pricing](#pricing) or null | no | `null` | Operator-declared token pricing for this model path; required on every selectable path iff budgets.maxUsdPerSession is set (cross-validated). Usually omitted for ollama (local compute). None = no pricing declared. |

## OpenAIProviderConfig

Configuration for the `openai` model provider (issue #15 — the second provider-agnostic adapter, the anthropic-shaped CLOUD variant of the stage-8 ollama pattern). A cloud provider like `anthropic`: it needs an API key and egress to `api.openai.com`. Unlike the ollama config it names an `apiKeyEnv` (the key VALUE is never in the spec — contract rule 3); like the ollama config its `baseHost` is configurable (so OpenAI-compatible endpoints work and the egress cross-check reads the host from the CONFIG, not a constant). The worker builds `https://<baseHost>` and reaches it THROUGH the audited egress proxy, exactly like the anthropic path. v1 additive amendment, stage 10 (issue #15 second cut).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `model` | str | yes | — | OpenAI model name, e.g. 'gpt-4o-mini'. (min_length: `1`) |
| `baseHost` | str | no | `"api.openai.com:443"` | OpenAI API host[:port] the worker reaches THROUGH the egress proxy (cross-validated against sandbox.egress). Same host[:port] grammar as sandbox.egress; default 'api.openai.com:443' (the public OpenAI API). Configurable so OpenAI-compatible endpoints work. |
| `apiKeyEnv` | str | no | `"OPENAI_API_KEY"` | Env var NAME holding the API key (never the value — contract rule 3). (pattern: `^[A-Z][A-Z0-9_]*$`) |
| `maxTokens` | int or null | no | `null` | Max output tokens per model call (Chat Completions max_tokens); None = the adapter default (the API's own default). Ceiling 128000 matches the anthropic/ollama config convention. (ge: `1`; le: `128000`) |
| `pricing` | [Pricing](#pricing) or null | no | `null` | Operator-declared token pricing for this model path; required on every selectable path iff budgets.maxUsdPerSession is set (cross-validated). None = no pricing declared. |

## ModelTier

One routing tier (blueprint `model/llmrouter`, decisions 'Route by task' and 'Where does cost control live?', option 'Tiered routing by task type').

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `name` | str | yes | — | Tier name, e.g. 'triage' or 'reasoning'. (pattern: `^[a-z0-9]+(-[a-z0-9]+)*$`) |
| `provider` | one of: `static`, `anthropic`, `ollama`, `openai` | yes | — | Provider for this tier (blueprint model/llmrouter). |
| `static` | [StaticProviderConfig](#staticproviderconfig) or null | no | `null` | Static provider config; required iff provider is 'static'. |
| `anthropic` | [AnthropicProviderConfig](#anthropicproviderconfig) or null | no | `null` | Anthropic provider config; required iff provider is 'anthropic'. |
| `ollama` | [OllamaProviderConfig](#ollamaproviderconfig) or null | no | `null` | Ollama provider config; required iff provider is 'ollama'. |
| `openai` | [OpenAIProviderConfig](#openaiproviderconfig) or null | no | `null` | OpenAI provider config; required iff provider is 'openai'. |

## ModelBudgets

Cost control (blueprint `model/llmrouter`, decision 'Where does cost control live?', options 'Per-session token budgets' and 'budget alerts').

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `maxTokensPerSession` | int or null | no | `null` | Token ceiling per session; None = unlimited. (ge: `1`) |
| `maxUsdPerSession` | float or null | no | `null` | USD ceiling per session; None = unlimited. Enforced against operator-declared pricing (models.*.pricing) — every selectable model path must declare pricing when this is set (cross-validated at load). v1 additive amendment, stage 25. (gt: `0`) |
| `onExceed` | one of: `block`, `warn` | no | `"block"` | Exceeding a budget blocks further model calls, or warns (budget alerts). |

## Observability

`spec.observability` — logging, audit sink, health (blueprint `persistence/auditobs`).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `audit` | [AuditConfig](#auditconfig) | yes | — | Append-only audit record sink. |
| `logLevel` | one of: `debug`, `info`, `warning`, `error` | no | `"info"` | Structured-logging level for the runtime. |
| `health` | [HealthConfig](#healthconfig) or null | no | `null` | Health-check declaration; None = the channel adapter's default surface. |

## AuditConfig

Audit sink selection inside `spec.observability` (blueprint `persistence/auditobs`; the append-only format is factory-level, ADR 0003 item 7 — the sink location is per-agent config).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `sink` | one of: `jsonl` | yes | — | Audit sink component. Current library: jsonl. |
| `path` | str | yes | — | Append-only JSONL file path inside the image. (min_length: `1`) |

## HealthConfig

Health-check surface (blueprint `persistence/auditobs`, responsibility: health checks and alerting for the daemon).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `path` | str | no | `"/healthz"` | HTTP health-check path. |

## Persistence

`spec.persistence` — storage tier for sessions/transcripts/vectors (blueprint `persistence/stores`, decision 'Files, SQLite, or Postgres?').

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `tier` | one of: `files`, `sqlite`, `postgres` | yes | — | Storage tier (blueprint persistence/stores): 'files' (JSONL + markdown), 'sqlite', or 'postgres'. You can graduate through all three. |

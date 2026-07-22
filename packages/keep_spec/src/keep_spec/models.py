"""Pydantic models for the keep/v1 AgentSpec envelope.

Contract: contracts/agent-spec.md (frozen v1). Binding rule 1 — strict
validation: unknown fields are a hard error (`extra="forbid"` on every model).

This module implements the COMPLETE keep/v1 schema: a field home for all 18
agent-level decisions of ADR 0003 (16 blueprint decisions + triggers + egress).
Every enum option traces to an option of a `docs/blueprint-data.json` decision
(the owning component id is cited in each docstring as `<layer>/<component>`)
or to ADR 0003's two additions. The walking-skeleton subset (stage 1) is a
strict subset of this schema: every new section/field is optional or defaulted,
so `examples/skeleton.yaml` validates unchanged (binding rule 4 — additive).

The machine-checkable decision -> field-path map lives in
`keep_spec.decision_coverage`.
"""

import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from keep_spec.cron import parse_cron

#: Env var NAME pattern — the spec names required secrets, never values
#: (contract agent-spec, binding rule 3).
ENV_VAR_NAME = r"^[A-Z][A-Z0-9_]*$"

#: kebab-case identifier (matches metadata.slug).
KEBAB = r"^[a-z0-9]+(-[a-z0-9]+)*$"

#: Egress allowlist entry: host[:port], optionally wildcard subdomain.
EGRESS_HOST = r"^(\*\.)?[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?$"

#: Five-field cron expression (minute hour day-of-month month day-of-week):
#: structural shape only — five fields drawn from the cron alphabet — kept as
#: a pattern so pure-JSON-schema consumers reject prose outright. Real
#: per-field syntax/range validation (#10, stage 22) lives in
#: `keep_spec.cron.parse_cron`, applied by ScheduleTrigger's validator.
CRON = r"^\s*[0-9*,/-]+(\s+[0-9*,/-]+){4}\s*$"

#: Constraint NAME on a tool grant: a tool-parameter-ish identifier.
CONSTRAINT_NAME = r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"

#: Constraint string VALUE: one identifier-ish token (letters/digits plus
#: ``. _ : / @ + -``), at most 64 chars. Rejects prose, whitespace, newlines,
#: and secret-shaped long blobs — a pin is a value like 'noc-outages', not text.
CONSTRAINT_VALUE = r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,63}$"

#: approval.autoApprove entry: '<server>.<tool>' — a kebab-case server id
#: (matches McpServer.name), a literal dot, then a tool-ish name. Never prose.
#: v1 clarification note (stage 5, mirrors the stage-2 egress precedent): these
#: entries were an unconstrained list[str]; format validation + the cross-check
#: against declared grants pin the always-intended value space ('name a granted
#: tool') while the field's only consumers are the in-repo reference specs.
#: Treated as a format clarification under contract rule 4, not a v1 break.
AUTO_APPROVE_ENTRY = r"^[a-z0-9]+(-[a-z0-9]+)*\.[A-Za-z0-9_][A-Za-z0-9_.-]*$"


class StrictModel(BaseModel):
    """Base for every spec model: unknown field = error, never ignored."""

    model_config = ConfigDict(extra="forbid")


class Metadata(StrictModel):
    """`metadata` — identity of the spec document itself."""

    name: str = Field(min_length=1, description="Human-readable agent name.")
    slug: str = Field(
        pattern=KEBAB,
        description="Kebab-case slug; the image becomes ghcr.io/<owner>/agent-keep-<slug>.",
    )
    description: str = Field(min_length=1, description="One-line description.")
    specVersion: str = Field(
        pattern=r"^\d+\.\d+\.\d+$",
        description="Semver of THIS document, bumped on any edit.",
    )


# --------------------------------------------------------------------------- persona


class Persona(StrictModel):
    """`spec.persona` — identity, tone, standing instructions, and where
    personalization lives (blueprint `core/persona`, decision "Where does
    personalization live?": static config / learned memory / both with clear
    precedence).
    """

    identity: str = Field(min_length=1, description="Who the agent is (system-prompt identity).")
    tone: str | None = Field(
        default=None, description="Voice/tone guidance merged into the system prompt."
    )
    instructions: list[str] = Field(
        default_factory=list,
        description="Standing instructions, merged deterministically after identity and tone.",
    )
    source: Literal["static", "learned", "both"] = Field(
        default="static",
        description=(
            "Where personalization lives (blueprint core/persona): 'static' config in this "
            "spec, 'learned' memory the agent updates, or 'both'. Learned-persona writes are "
            "privileged, audited memory writes (see spec.memory.writePolicy)."
        ),
    )
    precedence: Literal["static-over-learned", "learned-over-static"] | None = Field(
        default=None,
        description=(
            "Conflict rule when source is 'both' (blueprint core/persona: 'Both, with clear "
            "precedence'). Required iff source is 'both'."
        ),
    )

    @model_validator(mode="after")
    def _precedence_iff_both(self) -> Self:
        if self.source == "both" and self.precedence is None:
            raise ValueError("persona.precedence is required when persona.source is 'both'")
        if self.source != "both" and self.precedence is not None:
            raise ValueError("persona.precedence is only meaningful when persona.source is 'both'")
        return self


# -------------------------------------------------------------------------- triggers


class MessageTrigger(StrictModel):
    """A human message activates the agent (ADR 0003 addition: triggers).

    The skeleton's implicit behavior, made declarable.
    """

    kind: Literal["message"] = Field(description="Trigger kind discriminator.")


class ScheduleTrigger(StrictModel):
    """Cron-scheduled activation (ADR 0003 addition: triggers / schedule).

    v1 clarification note (#10, stage 22 — the stage-2/5 format-clarification
    procedure under contract rule 4): the original `cron` pattern validated
    token COUNT only, so `never gonna give you up` passed. Real five-field
    validation (`keep_spec.cron.parse_cron`) pins the always-intended value
    space — an executable cron expression — now that the schedule trigger has
    a runtime component. Every in-repo spec's cron string was already valid.
    """

    kind: Literal["schedule"] = Field(description="Trigger kind discriminator.")
    cron: str = Field(
        pattern=CRON,
        description=(
            "Five-field cron expression (minute hour day-of-month month day-of-week), "
            "evaluated in UTC. Per field: '*', numbers, ranges (N-M), comma lists, and "
            "'/step' on '*' or a range; day-of-week 0-7 (0 and 7 both Sunday). Exactly "
            "five fields — @names are not supported."
        ),
    )
    prompt: str = Field(
        min_length=1,
        description="Instruction delivered to the agent when the schedule fires.",
    )

    @field_validator("cron")
    @classmethod
    def _cron_is_real(cls, value: str) -> str:
        """Real per-field validation (#10): ranges, lists, steps, bounds —
        delegated to the shared parser the runtime component also uses, so a
        spec that validates is a spec whose schedule can actually run."""
        parse_cron(value)  # raises CronSyntaxError (a ValueError) with the field detail
        return value


class EventTrigger(StrictModel):
    """Event-subscription activation (ADR 0003 addition: triggers / event
    subscription) — e.g. the worked example's alarm-driven outage agent.

    `secretEnv` (v1 additive amendment, stage 18): the event-intake endpoint
    fails closed on a shared secret, and the spec names the env var holding it
    — the same spec-honesty posture as `ChannelVerification.secretEnv` (the
    value is deploy-time only, never in the spec or image; contract rule 3).
    Defaulted so every pre-amendment spec validates and behaves unchanged.
    """

    kind: Literal["event-subscription"] = Field(description="Trigger kind discriminator.")
    source: str = Field(
        min_length=1, description="Event source the agent subscribes to (e.g. 'alertmanager')."
    )
    event: str | None = Field(
        default=None, description="Event name/type filter within the source; None = all events."
    )
    prompt: str | None = Field(
        default=None,
        description="Instruction delivered with the event payload; None = payload only.",
    )
    secretEnv: str = Field(
        default="EVENT_WEBHOOK_SECRET",
        pattern=ENV_VAR_NAME,
        description=(
            "Env var NAME holding the shared secret that authenticates event deliveries "
            "to the intake endpoint (never the value — contract rule 3). "
            "v1 additive amendment, stage 18."
        ),
    )


Trigger = Annotated[MessageTrigger | ScheduleTrigger | EventTrigger, Field(discriminator="kind")]


def _default_activations() -> list["Trigger"]:
    return [MessageTrigger(kind="message")]


class Triggers(StrictModel):
    """`spec.triggers` — what activates the agent (ADR 0003 addition:
    message / schedule / event subscription). Absent section = message-only.
    """

    activations: list[Trigger] = Field(
        default_factory=_default_activations,
        min_length=1,
        description="Exhaustive positive list of activations; default is message-only.",
    )


# -------------------------------------------------------------------------- channels


class ChannelVerification(StrictModel):
    """Inbound verification for a platform channel (blueprint `gateway/identity`,
    responsibility: verify webhook signatures / bot token scopes).

    Names the verifying secret's env var; values never appear in the spec
    (contract agent-spec, binding rule 3).
    """

    method: Literal["signature", "token", "none"] = Field(
        description=(
            "'signature' = platform-signed payloads (webhook signing secret); 'token' = "
            "authenticated bot session (bot token); 'none' = unverified (dev only)."
        )
    )
    secretEnv: str | None = Field(
        default=None,
        pattern=ENV_VAR_NAME,
        description="Env var NAME holding the verifying secret (never the value).",
    )

    @model_validator(mode="after")
    def _secret_iff_verified(self) -> Self:
        if self.method != "none" and self.secretEnv is None:
            raise ValueError(
                f"verification method '{self.method}' requires secretEnv (the env var NAME)"
            )
        if self.method == "none" and self.secretEnv is not None:
            raise ValueError("verification method 'none' must not name a secretEnv")
        return self


class DevHttpChannel(StrictModel):
    """`spec.channels[]` dev-http — localhost HTTP adapter, zero platform deps
    (blueprint `channels/adapters`; transport option: webhooks — a plain HTTP
    endpoint; no verification, honestly unverified).
    """

    type: Literal["dev-http"] = Field(description="Channel adapter component.")
    port: int = Field(default=8000, ge=1, le=65535, description="TCP port the adapter listens on.")
    transport: Literal["webhook"] = Field(
        default="webhook",
        description="Delivery transport (blueprint channels/adapters): local HTTP endpoint.",
    )


class DiscordChannel(StrictModel):
    """`spec.channels[]` discord (blueprint `channels/adapters`; transport
    options: websocket gateway (Discord) / polling (fallback)).
    """

    type: Literal["discord"] = Field(description="Channel adapter component.")
    transport: Literal["websocket", "polling"] = Field(
        default="websocket",
        description=(
            "Delivery transport (blueprint channels/adapters): outbound-only websocket "
            "gateway (firewall-friendly) or polling fallback."
        ),
    )
    verification: ChannelVerification = Field(
        default_factory=lambda: ChannelVerification(method="token", secretEnv="DISCORD_BOT_TOKEN"),
        description="Bot-token verification; names the token env var.",
    )


class SlackChannel(StrictModel):
    """`spec.channels[]` slack (blueprint `channels/adapters`; transport
    options: webhooks (Slack) / websocket (Socket Mode) / polling (fallback)).
    """

    type: Literal["slack"] = Field(description="Channel adapter component.")
    transport: Literal["webhook", "websocket", "polling"] = Field(
        default="webhook",
        description=(
            "Delivery transport (blueprint channels/adapters): webhook (Events API, needs a "
            "public HTTPS endpoint), websocket (Socket Mode), or polling fallback."
        ),
    )
    verification: ChannelVerification = Field(
        default_factory=lambda: ChannelVerification(
            method="signature", secretEnv="SLACK_SIGNING_SECRET"
        ),
        description="Request-signature verification; names the signing-secret env var.",
    )


class WebexChannel(StrictModel):
    """`spec.channels[]` webex (blueprint `channels/adapters`; transport
    options: webhooks (WebEx) / polling (fallback)).
    """

    type: Literal["webex"] = Field(description="Channel adapter component.")
    transport: Literal["webhook", "polling"] = Field(
        default="webhook",
        description="Delivery transport (blueprint channels/adapters).",
    )
    verification: ChannelVerification = Field(
        default_factory=lambda: ChannelVerification(
            method="signature", secretEnv="WEBEX_WEBHOOK_SECRET"
        ),
        description="Webhook-signature verification; names the secret env var.",
    )


class SmsChannel(StrictModel):
    """`spec.channels[]` sms (blueprint `channels/adapters`; transport options:
    webhooks (SMS gateway callback) / polling (fallback)).
    """

    type: Literal["sms"] = Field(description="Channel adapter component.")
    transport: Literal["webhook", "polling"] = Field(
        default="webhook",
        description="Delivery transport (blueprint channels/adapters).",
    )
    verification: ChannelVerification = Field(
        default_factory=lambda: ChannelVerification(
            method="signature", secretEnv="SMS_WEBHOOK_SECRET"
        ),
        description="Gateway-signature verification; names the secret env var.",
    )


Channel = Annotated[
    DevHttpChannel | DiscordChannel | SlackChannel | WebexChannel | SmsChannel,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- gateway


class AllowlistEntry(StrictModel):
    """One allowed principal (blueprint `gateway/identity`)."""

    id: str = Field(
        min_length=1,
        description="Platform-scoped principal id, e.g. 'discord:1002003004' or 'sms:+15550100'.",
    )
    tier: Literal["owner", "trusted", "guest"] = Field(
        default="trusted",
        description="Access tier (blueprint gateway/identity: tiered access strangers vs owner).",
    )


class GatewayAllowlist(StrictModel):
    """Who may talk to the agent (blueprint `gateway/identity`, decision "Who is
    allowed to talk to the agent?": owner-only allowlist / pairing code flow /
    open with tiered permissions).
    """

    policy: Literal["owner-only", "pairing", "tiered"] = Field(
        description=(
            "Allowlist policy (blueprint gateway/identity): 'owner-only', 'pairing' (code "
            "flow for inviting users), or 'tiered' (open with tiered permissions)."
        )
    )
    roster: list[AllowlistEntry] = Field(
        default_factory=list,
        description="Statically-declared principals; pairing may add more at runtime.",
    )

    @model_validator(mode="after")
    def _no_duplicate_roster_entries(self) -> Self:
        """Reject EXACT (byte-equal) duplicate roster ids at `foundry validate`
        time — before stage 19 (#58) duplicates died only at AllowlistGate
        construction, i.e. boot. Exact duplicates ONLY: the runtime's roster
        normalization (the webex '@' heuristic + ascii_lower) is the gateway
        gate's security transform, so detecting normalized collisions (e.g.
        case-variant emails) remains the gate's job — the schema must not
        carry a second implementation of it."""
        seen: set[str] = set()
        for entry in self.roster:
            if entry.id in seen:
                raise ValueError(
                    f"gateway.allowlist.roster: duplicate entry for principal "
                    f"'{entry.id}' — remove or merge the duplicates"
                )
            seen.add(entry.id)
        return self


class Gateway(StrictModel):
    """`spec.gateway` — the control plane: who gets in and how work is queued
    (blueprint `gateway/identity` and `gateway/queue`).
    """

    queue: Literal["in-process", "redis"] = Field(
        description=(
            "Queue weight (blueprint gateway/queue, decision 'How heavy should the queue "
            "be?'): asyncio.Queue in-process, or Redis (persistence + pub/sub)."
        )
    )
    concurrency: Literal["serial", "concurrent-locked"] = Field(
        default="serial",
        description=(
            "Per-conversation handling (blueprint gateway/queue, decision 'Serial or "
            "concurrent handling per conversation?'): strict serial per session, or "
            "concurrent with locking."
        ),
    )
    allowlist: GatewayAllowlist | None = Field(
        default=None,
        description=(
            "Access-control policy (blueprint gateway/identity). Absent = no identity layer "
            "(dev-only; the skeleton's unverified dev-http channel)."
        ),
    )
    identityUnification: Literal["manual-link", "challenge", "separate"] = Field(
        default="separate",
        description=(
            "How one human is unified across channels (blueprint gateway/identity, decision "
            "'How do you unify one human across channels?'): manual linking table, "
            "verification challenge, or keep identities separate."
        ),
    )


# -------------------------------------------------------------------------- sessions


class SlidingWindowHistory(StrictModel):
    """History strategy: drop oldest turns (blueprint `core/sessions`)."""

    strategy: Literal["sliding-window"] = Field(description="History strategy discriminator.")
    maxTurns: int = Field(default=50, ge=1, description="Turns kept before the oldest is dropped.")


class SummarizationHistory(StrictModel):
    """History strategy: rolling summarization (blueprint `core/sessions`)."""

    strategy: Literal["summarization"] = Field(description="History strategy discriminator.")
    summarizeAfterTurns: int = Field(
        default=20, ge=1, description="Turns accumulated before a summarization pass."
    )


class RetrievalHistory(StrictModel):
    """History strategy: retrieval of relevant past turns (blueprint `core/sessions`)."""

    strategy: Literal["retrieval"] = Field(description="History strategy discriminator.")
    topK: int = Field(default=5, ge=1, description="Past turns retrieved per request.")


class LayeredHistory(StrictModel):
    """History strategy: window + periodic summary + retrieval — the blueprint's
    'most real systems layer them' (blueprint `core/sessions`).
    """

    strategy: Literal["layered"] = Field(description="History strategy discriminator.")
    windowTurns: int = Field(default=20, ge=1, description="Recent turns kept verbatim.")
    summarize: bool = Field(default=True, description="Roll older turns into a summary.")
    retrievalTopK: int = Field(default=5, ge=1, description="Past turns retrieved per request.")


History = Annotated[
    SlidingWindowHistory | SummarizationHistory | RetrievalHistory | LayeredHistory,
    Field(discriminator="strategy"),
]


class Sessions(StrictModel):
    """`spec.sessions` — session definition and lifecycle (blueprint
    `core/sessions`).
    """

    mode: Literal["single"] = Field(
        description="Stage-1 session manager component. Skeleton subset: single."
    )
    definition: Literal["per-channel", "per-user", "hybrid"] | None = Field(
        default=None,
        description=(
            "What a session IS (blueprint core/sessions, decision 'What is a session, "
            "exactly?'): per channel-conversation, per user (shared across channels), or "
            "hybrid (shared memory, separate transcripts). Absent = the skeleton's single "
            "session."
        ),
    )
    history: History | None = Field(
        default=None,
        description=(
            "How history fits the context window (blueprint core/sessions, decision 'How do "
            "you fit history into the context window?'): sliding-window / summarization / "
            "retrieval / layered. Absent = whole-session history (skeleton)."
        ),
    )


# ---------------------------------------------------------------------------- memory


class FactsMemory(StrictModel):
    """Memory structure: structured facts — key-value / markdown files, human-
    auditable (blueprint `core/memorysys`, option 'Structured facts').
    """

    kind: Literal["facts"] = Field(description="Memory structure discriminator.")
    store: Literal["none"] = Field(
        default="none",
        description=(
            "No vector store (blueprint core/memorysys, decision 'Which vector store?'): "
            "facts live in the persistence tier."
        ),
    )


#: What gets embedded into the vector layer (blueprint `core/memorysys`,
#: retrieval responsibility: 'embed and search past conversations and
#: documents'). v1 additive amendment, stage 4.
MemoryCorpus = Literal["agent-summaries", "transcripts", "documents"]

_CORPUS_DESCRIPTION = (
    "What gets embedded (blueprint core/memorysys, retrieval responsibility: 'embed and "
    "search past conversations and documents'): 'agent-summaries' (agent-written "
    "summaries), 'transcripts' (raw conversation transcripts), or 'documents'. "
    "None = the structure's default corpus (transcripts)."
)


class VectorMemory(StrictModel):
    """Memory structure: vector store over transcripts (blueprint
    `core/memorysys`, option 'Vector store over transcripts'; `corpus` scopes
    what gets embedded per the memorysys retrieval responsibility).
    """

    kind: Literal["vectors"] = Field(description="Memory structure discriminator.")
    store: Literal["sqlite-vec", "pgvector"] = Field(
        description=(
            "Vector store choice (blueprint core/memorysys, decision 'Which vector store, "
            "when you get there?'): SQLite + sqlite-vec or Postgres + pgvector."
        )
    )
    corpus: MemoryCorpus | None = Field(default=None, description=_CORPUS_DESCRIPTION)


class LayeredMemory(StrictModel):
    """Memory structure: facts + vectors (blueprint `core/memorysys`, option
    'Layered: facts + vectors'; `corpus` scopes what gets embedded per the
    memorysys retrieval responsibility).
    """

    kind: Literal["layered"] = Field(description="Memory structure discriminator.")
    store: Literal["sqlite-vec", "pgvector"] = Field(
        description=(
            "Vector store choice for the vector layer (blueprint core/memorysys): "
            "SQLite + sqlite-vec or Postgres + pgvector."
        )
    )
    corpus: MemoryCorpus | None = Field(default=None, description=_CORPUS_DESCRIPTION)


MemoryStructure = Annotated[FactsMemory | VectorMemory | LayeredMemory, Field(discriminator="kind")]


class Memory(StrictModel):
    """`spec.memory` — long-term memory (blueprint `core/memorysys`). Absent
    section = no durable memory beyond the session (absence semantics, rule 2).
    """

    structure: MemoryStructure = Field(
        description=(
            "Structured memory or embeddings-first (blueprint core/memorysys, decision "
            "'Structured memory or embeddings-first?'): facts / vectors / layered."
        )
    )
    writePolicy: Literal["user-command", "agent-autonomous", "off"] = Field(
        default="user-command",
        description=(
            "Who may write memory (blueprint core/memorysys considerations: 'define who may "
            "write memory — that's a trust decision'): explicit user command, the agent "
            "autonomously, or nobody (read-only). Agent-autonomous writes are privileged "
            "tool calls — always audited (blueprint core/persona considerations)."
        ),
    )


# ---------------------------------------------------------------------------- skills


class SkillPack(StrictModel):
    """One entry of `spec.skills` — an instruction pack (knowledge, never code;
    factory-level ADR 0003 item 5 fixes the skill definition) with its selection
    strategy (blueprint `capabilities/skills`, decision 'How are skills selected
    per request?').
    """

    name: str = Field(pattern=KEBAB, description="Skill pack id in the skill registry.")
    version: str | None = Field(
        default=None,
        description="Version pin (blueprint capabilities/skills: pin what production agents use).",
    )
    selection: Literal["always", "keyword", "model-driven"] = Field(
        default="always",
        description=(
            "Selection strategy (blueprint capabilities/skills): 'always' in prompt, "
            "'keyword' (keyword/intent triggering), or 'model-driven' (descriptions in "
            "prompt, bodies on demand)."
        ),
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Trigger keywords; required iff selection is 'keyword'.",
    )

    @model_validator(mode="after")
    def _keywords_iff_keyword_selection(self) -> Self:
        if self.selection == "keyword" and not self.keywords:
            raise ValueError(
                f"skill pack '{self.name}': selection 'keyword' requires non-empty keywords"
            )
        if self.selection != "keyword" and self.keywords:
            raise ValueError(
                f"skill pack '{self.name}': keywords are only meaningful with selection 'keyword'"
            )
        return self


# ----------------------------------------------------------------------------- tools


class StdioTransport(StrictModel):
    """MCP server as a local child process (blueprint `capabilities/mcpmgr`,
    transport option 'stdio'; the factory supports both transports behind one
    interface — ADR 0003 item 4).
    """

    kind: Literal["stdio"] = Field(description="MCP transport discriminator.")
    command: str = Field(min_length=1, description="Executable to spawn.")
    args: list[str] = Field(default_factory=list, description="Arguments for the command.")


class HttpTransport(StrictModel):
    """MCP server over streamable HTTP (blueprint `capabilities/mcpmgr`,
    transport option 'Streamable HTTP (remote/shared)').
    """

    kind: Literal["http"] = Field(description="MCP transport discriminator.")
    url: str = Field(pattern=r"^https?://\S+$", description="Base URL of the remote MCP server.")

    @model_validator(mode="after")
    def _no_embedded_credentials(self) -> Self:
        parts = urlsplit(self.url)
        if parts.username is not None or parts.password is not None:
            raise ValueError(
                f"transport url {self.url!r} embeds credentials (userinfo@) — secrets never "
                "appear in the spec (contract rule 3); name them in secretEnvs instead"
            )
        return self


class LocalTransport(StrictModel):
    """Tool server backed by the runtime's in-process local tool registry
    (stage-6 v1 additive amendment — a new transport enum value under contract
    rule 4). No external process, no network: the grants select from the
    `local_tools` component's registry of harmless demo tools, executed by the
    same constraint-enforcing tool executor MCP transports plug into (stage 7).
    """

    kind: Literal["local"] = Field(description="Tool transport discriminator.")


McpTransport = Annotated[
    StdioTransport | HttpTransport | LocalTransport, Field(discriminator="kind")
]


#: A validated scalar constraint value (stage-4 v1 amendment): an
#: identifier-ish string, an int, or a bool — never free prose. Strict int/bool
#: so YAML floats (`2.0`) or stringly-typed values cannot slip through.
ConstraintValue = Annotated[str, Field(pattern=CONSTRAINT_VALUE)] | StrictInt | StrictBool

#: The `constraints` mapping on a tool grant: constraint name -> pinned scalar.
#: Non-empty when present — an empty mapping pins nothing and may not be
#: declared (the spec is an exhaustive positive declaration, rule 2).
ConstraintMap = Annotated[
    dict[Annotated[str, Field(pattern=CONSTRAINT_NAME)], ConstraintValue],
    Field(min_length=1),
]


class ToolGrant(StrictModel):
    """One allowed tool on an MCP server (blueprint `capabilities/mcpmgr`:
    'tool availability IS the permission model').

    `constraints` (v1 additive amendment, stage 4) narrows a grant further:
    hard parameter pins the runtime tool executor MUST enforce (e.g.
    `room: noc-outages` on a paging tool). No constraint-enforcing executor
    exists yet, so any grant carrying constraints fails the buildable-check
    loudly (same fail-loud pattern as egress/approval).
    """

    name: str = Field(min_length=1, description="Tool name as exposed by the MCP server.")
    scope: Literal["read-only", "read-write"] = Field(
        default="read-only",
        description=(
            "Grant scope (blueprint capabilities/mcpmgr considerations: 'the public-facing "
            "agent gets read-only tools'). Default read-only — least privilege."
        ),
    )
    constraints: ConstraintMap | None = Field(
        default=None,
        description=(
            "Hard parameter pins the tool executor MUST enforce: constraint name -> "
            "validated scalar (identifier-ish string, int, or bool — never free prose), "
            "e.g. pinning a paging tool to `room: noc-outages`. None = no pins."
        ),
    )


class McpServer(StrictModel):
    """One entry of `spec.tools` — an MCP server attached to THIS agent
    (blueprint `capabilities/mcpmgr`, decision 'Which MCP servers get attached
    to which agent?', option 'Per-agent allowlist'). The `spec.tools` list IS
    the per-agent allowlist; anything not listed is absent from the image
    (absence semantics, rule 2).
    """

    name: str = Field(pattern=KEBAB, description="Server id, used to namespace its tools.")
    transport: McpTransport = Field(description="stdio child process or remote HTTP.")
    allow: list[ToolGrant] = Field(
        min_length=1,
        description=(
            "Exhaustive positive per-tool allowlist with scopes; a server with no grants "
            "may not be attached."
        ),
    )
    secretEnvs: list[Annotated[str, Field(pattern=ENV_VAR_NAME)]] = Field(
        default_factory=list,
        description=(
            "Env var NAMES the server needs (never values — contract rule 3); injected at "
            "runtime, never visible to the model."
        ),
    )

    @model_validator(mode="after")
    def _grant_names_are_unique(self) -> Self:
        """allow[].name values must be unique PER SERVER (additive validation, #46).

        Two grants naming one tool on one server are ambiguous — which scope
        and constraints apply? — and would boot MCP children only to die at
        executor construction on the stage-12 duplicate-name check. Caught at
        load time instead (same theme as the server-name uniqueness check);
        the same tool name on two DIFFERENT servers stays fine — the server
        name namespaces it.
        """
        names = [grant.name for grant in self.allow]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"spec.tools server '{self.name}' declares duplicate grant name(s): "
                f"{duplicates} — each tool may be granted once per server"
            )
        return self


# -------------------------------------------------------------------------- approval


class Approval(StrictModel):
    """`spec.approval` — what requires human confirmation (blueprint
    `capabilities/executor`, decision 'What requires human approval?').
    """

    policy: Literal["autonomous", "allowlist-confirm-rest", "everything"] = Field(
        default="allowlist-confirm-rest",
        description=(
            "Approval policy (blueprint capabilities/executor): 'autonomous' (nothing "
            "confirmed), 'allowlist-confirm-rest' (auto-approve the allowlist, confirm the "
            "rest — default-deny), or 'everything' (everything confirmed). Default matches "
            "the blueprint's 'default-deny with a growing auto-approve list'."
        ),
    )
    autoApprove: list[Annotated[str, Field(pattern=AUTO_APPROVE_ENTRY)]] = Field(
        default_factory=list,
        description=(
            "Auto-approved tool names ('<server>.<tool>'); only meaningful with policy "
            "'allowlist-confirm-rest'. Every entry must name a grant declared in "
            "spec.tools (cross-validated at the envelope level)."
        ),
    )

    @model_validator(mode="after")
    def _auto_approve_iff_allowlist_policy(self) -> Self:
        if self.policy != "allowlist-confirm-rest" and self.autoApprove:
            raise ValueError(
                f"approval.autoApprove is only meaningful with policy "
                f"'allowlist-confirm-rest', not '{self.policy}'"
            )
        return self


# --------------------------------------------------------------------------- sandbox


class Sandbox(StrictModel):
    """`spec.sandbox` — execution isolation + network egress (blueprint
    `capabilities/executor`, decision 'How isolated is code/shell execution?';
    egress is ADR 0003's second addition).

    v1 clarification note: `egress` entries were an unconstrained `list[str]`
    in the stage-1 schema. Host[:port] format validation was added in stage 2,
    while the field had exactly one consumer (the walking skeleton's empty
    list), as a format clarification of the always-intended value space — a
    network egress allowlist of hosts — not a v1 break.
    """

    profile: Literal["same-process", "restricted-user", "container"] = Field(
        default="container",
        description=(
            "Tool-execution isolation (blueprint capabilities/executor): 'same-process' "
            "(same process/user — the blueprint says don't), 'restricted-user' (dedicated "
            "unprivileged user), or 'container' (container/VM per execution). Default "
            "container — every built agent is already a container (ADR 0003 item 8)."
        ),
    )
    egress: list[str] = Field(
        default_factory=list,
        description=(
            "Network egress allowlist of host[:port] entries (ADR 0003 addition: egress). "
            "Exhaustive positive declaration; default EMPTY — nothing else is reachable "
            "by construction."
        ),
    )

    @model_validator(mode="after")
    def _egress_hosts_wellformed(self) -> Self:
        for host in self.egress:
            if not re.match(EGRESS_HOST, host):
                raise ValueError(
                    f"sandbox.egress entry {host!r} is not a host[:port] "
                    "(optionally '*.' wildcard-subdomain) entry"
                )
            _, colon, port = host.rpartition(":")
            if colon and not 1 <= int(port) <= 65535:
                raise ValueError(f"sandbox.egress entry {host!r} has port {port} outside 1-65535")
        return self


# ---------------------------------------------------------------------------- models


class StaticProviderConfig(StrictModel):
    """Configuration for the `static` model provider (ADR 0004 — deterministic,
    hermetic, first-class).
    """

    script: list[str] = Field(
        min_length=1,
        description="Deterministic scripted replies; call N returns script[N % len(script)].",
    )


class Pricing(StrictModel):
    """Operator-declared token pricing for one model path (blueprint
    `model/llmrouter`, decision 'Where does cost control live?').

    There is NO library price table: prices drift and a stale table silently
    mis-enforces, so the OPERATOR declares pricing in the spec, right next to
    the model it prices, and `budgets.maxUsdPerSession` enforces against these
    exact numbers (honest, auditable, no hidden knobs — ADR 0003 spirit).
    Both rates are required together (both-or-neither): omit the `pricing`
    block entirely for no pricing. v1 additive amendment, stage 25.
    """

    usdPerMillionInputTokens: float | None = Field(
        default=None,
        gt=0,
        description="USD charged per 1,000,000 input (prompt) tokens for this model path.",
    )
    usdPerMillionOutputTokens: float | None = Field(
        default=None,
        gt=0,
        description="USD charged per 1,000,000 output (completion) tokens for this model path.",
    )

    @model_validator(mode="after")
    def _both_rates_present(self) -> Self:
        if self.usdPerMillionInputTokens is None or self.usdPerMillionOutputTokens is None:
            raise ValueError(
                "pricing must declare BOTH usdPerMillionInputTokens and "
                "usdPerMillionOutputTokens (both-or-neither) — omit the pricing block for no "
                "pricing"
            )
        return self


class AnthropicProviderConfig(StrictModel):
    """Configuration for the `anthropic` model provider (blueprint
    `model/llmrouter` — provider adapters behind one interface).
    """

    model: str = Field(min_length=1, description="Model name, e.g. 'claude-sonnet-4-5'.")
    apiKeyEnv: str = Field(
        default="ANTHROPIC_API_KEY",
        pattern=ENV_VAR_NAME,
        description="Env var NAME holding the API key (never the value — contract rule 3).",
    )
    maxTokens: int | None = Field(
        default=None,
        ge=1,
        le=128_000,
        description=(
            "Max output tokens per model call (Messages API max_tokens); None = the adapter "
            "default (4096). Ceiling 128000 matches the largest current model output cap. "
            "v1 additive amendment, stage 13."
        ),
    )
    pricing: Pricing | None = Field(
        default=None,
        description=(
            "Operator-declared token pricing for this model path; required on every "
            "selectable path iff budgets.maxUsdPerSession is set (cross-validated). "
            "None = no pricing declared. v1 additive amendment, stage 25."
        ),
    )


class OllamaProviderConfig(StrictModel):
    """Configuration for the `ollama` model provider (ADR 0006 — local
    inference reached THROUGH the audited egress proxy, no API key).

    The worker's Ollama base host is `host.docker.internal:11434` by default:
    the host's Ollama server, reached over the docker gateway via the egress
    proxy (the worker never routes there directly). No `apiKeyEnv` — Ollama
    takes no key. `pricing` is usually omitted (local compute has no per-token
    USD cost in the cloud-API sense); token COUNTS still record. v1 additive
    amendment, stage 8 (issue #15 first cut).
    """

    model: str = Field(min_length=1, description="Ollama model name, e.g. 'llama3.2:latest'.")
    baseHost: str = Field(
        default="host.docker.internal:11434",
        description=(
            "Ollama server host[:port] the worker reaches THROUGH the egress proxy "
            "(cross-validated against sandbox.egress). Same host[:port] grammar as "
            "sandbox.egress; default 'host.docker.internal:11434' (the host's Ollama over "
            "the docker gateway — ADR 0006)."
        ),
    )
    maxTokens: int | None = Field(
        default=None,
        ge=1,
        le=128_000,
        description=(
            "Max output tokens per model call (Ollama options.num_predict); None = the "
            "adapter default (Ollama's own num_predict default). Ceiling 128000 matches the "
            "anthropic config's convention."
        ),
    )
    pricing: Pricing | None = Field(
        default=None,
        description=(
            "Operator-declared token pricing for this model path; required on every "
            "selectable path iff budgets.maxUsdPerSession is set (cross-validated). Usually "
            "omitted for ollama (local compute). None = no pricing declared."
        ),
    )

    @model_validator(mode="after")
    def _base_host_wellformed(self) -> Self:
        if not re.match(EGRESS_HOST, self.baseHost):
            raise ValueError(
                f"models.ollama.baseHost {self.baseHost!r} is not a host[:port] "
                "(optionally '*.' wildcard-subdomain) entry"
            )
        _, colon, port = self.baseHost.rpartition(":")
        if colon and not 1 <= int(port) <= 65535:
            raise ValueError(
                f"models.ollama.baseHost {self.baseHost!r} has port {port} outside 1-65535"
            )
        return self


class OpenAIProviderConfig(StrictModel):
    """Configuration for the `openai` model provider (issue #15 — the second
    provider-agnostic adapter, the anthropic-shaped CLOUD variant of the
    stage-8 ollama pattern).

    A cloud provider like `anthropic`: it needs an API key and egress to
    `api.openai.com`. Unlike the ollama config it names an `apiKeyEnv` (the
    key VALUE is never in the spec — contract rule 3); like the ollama config
    its `baseHost` is configurable (so OpenAI-compatible endpoints work and the
    egress cross-check reads the host from the CONFIG, not a constant). The
    worker builds `https://<baseHost>` and reaches it THROUGH the audited
    egress proxy, exactly like the anthropic path. v1 additive amendment,
    stage 10 (issue #15 second cut).
    """

    model: str = Field(min_length=1, description="OpenAI model name, e.g. 'gpt-4o-mini'.")
    baseHost: str = Field(
        default="api.openai.com:443",
        description=(
            "OpenAI API host[:port] the worker reaches THROUGH the egress proxy "
            "(cross-validated against sandbox.egress). Same host[:port] grammar as "
            "sandbox.egress; default 'api.openai.com:443' (the public OpenAI API). "
            "Configurable so OpenAI-compatible endpoints work."
        ),
    )
    apiKeyEnv: str = Field(
        default="OPENAI_API_KEY",
        pattern=ENV_VAR_NAME,
        description="Env var NAME holding the API key (never the value — contract rule 3).",
    )
    maxTokens: int | None = Field(
        default=None,
        ge=1,
        le=128_000,
        description=(
            "Max output tokens per model call (Chat Completions max_tokens); None = the "
            "adapter default (the API's own default). Ceiling 128000 matches the "
            "anthropic/ollama config convention."
        ),
    )
    pricing: Pricing | None = Field(
        default=None,
        description=(
            "Operator-declared token pricing for this model path; required on every "
            "selectable path iff budgets.maxUsdPerSession is set (cross-validated). "
            "None = no pricing declared."
        ),
    )

    @model_validator(mode="after")
    def _base_host_wellformed(self) -> Self:
        if not re.match(EGRESS_HOST, self.baseHost):
            raise ValueError(
                f"models.openai.baseHost {self.baseHost!r} is not a host[:port] "
                "(optionally '*.' wildcard-subdomain) entry"
            )
        _, colon, port = self.baseHost.rpartition(":")
        if colon and not 1 <= int(port) <= 65535:
            raise ValueError(
                f"models.openai.baseHost {self.baseHost!r} has port {port} outside 1-65535"
            )
        return self


def _check_provider_config(
    where: str,
    provider: str,
    static: StaticProviderConfig | None,
    anthropic: AnthropicProviderConfig | None,
    ollama: OllamaProviderConfig | None,
    openai: OpenAIProviderConfig | None,
) -> None:
    configured = {
        name
        for name, cfg in (
            ("static", static),
            ("anthropic", anthropic),
            ("ollama", ollama),
            ("openai", openai),
        )
        if cfg is not None
    }
    if provider not in configured:
        raise ValueError(f"{where}: provider '{provider}' requires a '{provider}' config block")
    extra = sorted(configured - {provider})
    if extra:
        raise ValueError(
            f"{where}: config present for unselected provider(s) {extra} — the spec is an "
            "exhaustive positive declaration"
        )


class ModelTier(StrictModel):
    """One routing tier (blueprint `model/llmrouter`, decisions 'Route by task'
    and 'Where does cost control live?', option 'Tiered routing by task type').
    """

    name: str = Field(pattern=KEBAB, description="Tier name, e.g. 'triage' or 'reasoning'.")
    provider: Literal["static", "anthropic", "ollama", "openai"] = Field(
        description="Provider for this tier (blueprint model/llmrouter)."
    )
    static: StaticProviderConfig | None = Field(
        default=None, description="Static provider config; required iff provider is 'static'."
    )
    anthropic: AnthropicProviderConfig | None = Field(
        default=None,
        description="Anthropic provider config; required iff provider is 'anthropic'.",
    )
    ollama: OllamaProviderConfig | None = Field(
        default=None,
        description="Ollama provider config; required iff provider is 'ollama'.",
    )
    openai: OpenAIProviderConfig | None = Field(
        default=None,
        description="OpenAI provider config; required iff provider is 'openai'.",
    )

    @model_validator(mode="after")
    def _config_matches_provider(self) -> Self:
        _check_provider_config(
            f"models.tiers['{self.name}']",
            self.provider,
            self.static,
            self.anthropic,
            self.ollama,
            self.openai,
        )
        return self


class ModelBudgets(StrictModel):
    """Cost control (blueprint `model/llmrouter`, decision 'Where does cost
    control live?', options 'Per-session token budgets' and 'budget alerts').
    """

    maxTokensPerSession: int | None = Field(
        default=None, ge=1, description="Token ceiling per session; None = unlimited."
    )
    maxUsdPerSession: float | None = Field(
        default=None,
        gt=0,
        description=(
            "USD ceiling per session; None = unlimited. Enforced against operator-declared "
            "pricing (models.*.pricing) — every selectable model path must declare pricing "
            "when this is set (cross-validated at load). v1 additive amendment, stage 25."
        ),
    )
    onExceed: Literal["block", "warn"] = Field(
        default="block",
        description="Exceeding a budget blocks further model calls, or warns (budget alerts).",
    )


class Models(StrictModel):
    """`spec.models` — model routing, providers, and budgets (blueprint
    `model/llmrouter`). 'static' is a first-class provider (ADR 0004).
    """

    provider: Literal["static", "anthropic", "ollama", "openai"] = Field(
        description="Default provider when no tier routing applies (blueprint model/llmrouter)."
    )
    static: StaticProviderConfig | None = Field(
        default=None, description="Static provider config; required iff provider is 'static'."
    )
    anthropic: AnthropicProviderConfig | None = Field(
        default=None,
        description="Anthropic provider config; required iff provider is 'anthropic'.",
    )
    ollama: OllamaProviderConfig | None = Field(
        default=None,
        description="Ollama provider config; required iff provider is 'ollama'.",
    )
    openai: OpenAIProviderConfig | None = Field(
        default=None,
        description="OpenAI provider config; required iff provider is 'openai'.",
    )
    tiers: list[ModelTier] = Field(
        default_factory=list,
        description=(
            "Routing tiers by task type (blueprint model/llmrouter: cheap model for triage, "
            "flagship for reasoning). Empty = single default provider."
        ),
    )
    budgets: ModelBudgets | None = Field(
        default=None,
        description="Per-session cost control (blueprint model/llmrouter); None = no budgets.",
    )

    @model_validator(mode="after")
    def _config_matches_provider(self) -> Self:
        _check_provider_config(
            "models", self.provider, self.static, self.anthropic, self.ollama, self.openai
        )
        names = [tier.name for tier in self.tiers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"models.tiers: duplicate tier name(s) {duplicates}")
        return self

    @model_validator(mode="after")
    def _usd_budget_requires_pricing(self) -> Self:
        """A USD budget enforces against declared pricing, so EVERY selectable
        model path (the default provider + every tier) must declare pricing —
        a USD budget over an unpriced path is the decorative, unenforceable
        cost control this amendment eliminates (stage 25; the autoApprove
        cross-check precedent, stage 5). The `static` provider has no
        meaningful, priceable cost (ADR 0004: hermetic, deterministic), so a
        static path under a USD budget is refused as unpriced — declare a
        priced provider or drop the budget.
        """
        budgets = self.budgets
        if budgets is None or budgets.maxUsdPerSession is None:
            return self
        unpriced: list[str] = []

        def check(
            where: str,
            provider: str,
            anthropic: AnthropicProviderConfig | None,
            ollama: OllamaProviderConfig | None,
            openai: OpenAIProviderConfig | None,
        ) -> None:
            if provider == "static":
                unpriced.append(f"{where} (provider 'static' has no declarable price)")
            elif provider == "anthropic" and (anthropic is None or anthropic.pricing is None):
                unpriced.append(f"{where}.pricing")
            elif provider == "ollama" and (ollama is None or ollama.pricing is None):
                unpriced.append(f"{where}.pricing")
            elif provider == "openai" and (openai is None or openai.pricing is None):
                unpriced.append(f"{where}.pricing")

        check("models", self.provider, self.anthropic, self.ollama, self.openai)
        for tier in self.tiers:
            check(
                f"models.tiers['{tier.name}']",
                tier.provider,
                tier.anthropic,
                tier.ollama,
                tier.openai,
            )
        if unpriced:
            raise ValueError(
                "models.budgets.maxUsdPerSession is set but these selectable model paths "
                f"declare no pricing: {unpriced} — a USD budget over an unpriced model is "
                "decorative; declare pricing on every path or remove maxUsdPerSession"
            )
        return self


# --------------------------------------------------------------------- observability


class AuditConfig(StrictModel):
    """Audit sink selection inside `spec.observability` (blueprint
    `persistence/auditobs`; the append-only format is factory-level, ADR 0003
    item 7 — the sink location is per-agent config).
    """

    sink: Literal["jsonl"] = Field(description="Audit sink component. Current library: jsonl.")
    path: str = Field(min_length=1, description="Append-only JSONL file path inside the image.")


class HealthConfig(StrictModel):
    """Health-check surface (blueprint `persistence/auditobs`, responsibility:
    health checks and alerting for the daemon).
    """

    path: str = Field(default="/healthz", description="HTTP health-check path.")


class Observability(StrictModel):
    """`spec.observability` — logging, audit sink, health (blueprint
    `persistence/auditobs`).
    """

    audit: AuditConfig = Field(description="Append-only audit record sink.")
    logLevel: Literal["debug", "info", "warning", "error"] = Field(
        default="info", description="Structured-logging level for the runtime."
    )
    health: HealthConfig | None = Field(
        default=None,
        description="Health-check declaration; None = the channel adapter's default surface.",
    )


# ------------------------------------------------------------------------ persistence


class Persistence(StrictModel):
    """`spec.persistence` — storage tier for sessions/transcripts/vectors
    (blueprint `persistence/stores`, decision 'Files, SQLite, or Postgres?').
    """

    tier: Literal["files", "sqlite", "postgres"] = Field(
        description=(
            "Storage tier (blueprint persistence/stores): 'files' (JSONL + markdown), "
            "'sqlite', or 'postgres'. You can graduate through all three."
        )
    )


# ----------------------------------------------------------------------- the envelope


class SpecSections(StrictModel):
    """`spec` — the agent-level sections (contract agent-spec schema block).

    Sections required by the walking skeleton stay required; every section and
    field added since stage 1 is optional or defaulted (additive, rule 4).
    """

    persona: Persona
    triggers: Triggers | None = Field(
        default=None, description="What activates the agent; absent = message-only."
    )
    channels: list[Channel] = Field(min_length=1)
    gateway: Gateway
    sessions: Sessions
    memory: Memory | None = Field(
        default=None, description="Long-term memory; absent = none built (absence semantics)."
    )
    skills: list[SkillPack] = Field(
        default_factory=list,
        description="Instruction packs enabled for this agent; absent tools/skills are absent.",
    )
    tools: list[McpServer] = Field(
        default_factory=list,
        description="Per-agent MCP server allowlist; empty = no external tools in the image.",
    )
    approval: Approval
    sandbox: Sandbox
    models: Models
    observability: Observability
    persistence: Persistence

    @model_validator(mode="after")
    def _tool_server_names_are_unique(self) -> Self:
        """spec.tools[].name values must be unique (additive validation, #34).

        The server name namespaces its tools ('<server>.<tool>'): two servers
        sharing one name would make grants, autoApprove entries, and the
        runtime tool registry ambiguous — an author-controlled config footgun
        caught at load time, not at bind time.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for server in self.tools:
            if server.name in seen and server.name not in duplicates:
                duplicates.append(server.name)
            seen.add(server.name)
        if duplicates:
            raise ValueError(
                f"spec.tools declares duplicate server name(s): {duplicates} — "
                "server names namespace their tools and must be unique"
            )
        return self

    @model_validator(mode="after")
    def _auto_approve_names_declared_grants(self) -> Self:
        """approval.autoApprove entries must name grants declared in spec.tools.

        The spec is an exhaustive positive declaration (contract agent-spec,
        rule 2): pre-approving a tool that no server grants is a dangling
        permission — either a typo or a leftover — and fails loudly here.
        """
        declared = {
            f"{server.name}.{grant.name}" for server in self.tools for grant in server.allow
        }
        missing = [entry for entry in self.approval.autoApprove if entry not in declared]
        if missing:
            raise ValueError(
                f"approval.autoApprove names grant(s) not declared in spec.tools: "
                f"{missing} — declared grants: {sorted(declared) if declared else 'none'}"
            )
        return self


class AgentSpec(StrictModel):
    """The keep/v1 AgentSpec envelope."""

    apiVersion: Literal["keep/v1"]
    kind: Literal["AgentSpec"]
    metadata: Metadata
    spec: SpecSections


def validate_spec_data(data: Any) -> AgentSpec:
    """Validate already-parsed YAML/JSON data into an AgentSpec (strict)."""
    return AgentSpec.model_validate(data)


def load_spec(path: str | Path) -> AgentSpec:
    """Load and strictly validate an AgentSpec YAML document from disk."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return validate_spec_data(data)


def dump_spec_data(spec: AgentSpec) -> dict[str, Any]:
    """The spec as plain data, containing exactly what the source document set.

    `exclude_unset` keeps defaulted fields out, so YAML -> models -> YAML is
    lossless (round-trip property, stage-2 testing requirements).
    """
    return spec.model_dump(mode="json", exclude_unset=True)


def dump_spec_yaml(spec: AgentSpec) -> str:
    """Serialize a spec back to YAML (see `dump_spec_data`)."""
    return yaml.safe_dump(dump_spec_data(spec), sort_keys=False)

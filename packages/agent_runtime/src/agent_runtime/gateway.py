"""Generic gateway allowlist enforcement — spec-driven, platform-agnostic.

The control-plane's identity layer (blueprint gateway/identity, "Who is allowed
to talk to the agent?"). Maps a normalized message's platform identity onto the
spec's roster and either admits it (resolving its internal identity) or drops it
and records a `gateway_reject` audit event. NOTHING about this module is WebEx-
specific: it operates on `internal-message` (every adapter's output), so the
runner wires ONE gate — exactly when `spec.gateway.allowlist` is declared — and
every channel adapter consults it. dev-http gains the same enforcement, which is
how the roster is exercised end-to-end with no platform credentials.

`gateway_reject` is an ADDITIVE audit event type: the audit-record contract
(frozen v1) permits new event types under its versioning rule ("Changes are
additive only (new event types, ...)" — audit-record.md §Versioning). No
contract edit is required.
"""

import hashlib
import string

from agent_runtime.audit import Action, AgentIdentity, AuditRecord, AuditSink, Outcome, Trigger
from agent_runtime.messages import InternalMessage
from keep_spec.models import GatewayAllowlist

#: Allowlist policies this enforcement understands. 'pairing' (a runtime code
#: flow that GROWS the roster at run time) is deliberately absent — a pairing
#: spec stays unbuildable at the wiring guard rather than silently admitting
#: strangers (see wiring.unimplemented_selections).
SUPPORTED_POLICIES = frozenset({"owner-only", "tiered"})

#: ASCII A-Z -> a-z and NOTHING else (see ascii_lower).
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _sha256(data: str) -> str:
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def ascii_lower(value: str) -> str:
    """Lowercase ASCII ``A-Z`` only; every other codepoint passes untouched.

    Deliberately NOT ``str.casefold()`` (or even ``str.lower()``): Unicode
    case folding merges formally DISTINCT SMTPUTF8 local parts —
    ``'straße'.casefold() == 'strasse'`` — so a rostered ``strasse@x.com``
    would admit a WebEx-attested ``straße@x.com`` (#45). Only the A-Z/a-z
    merge is universally case-insensitive for email; non-ASCII characters are
    compared exactly. The webex adapter's ``personEmail`` extraction and the
    gate's roster normalization both use THIS helper, so the two sides can
    never drift.
    """
    return value.translate(_ASCII_LOWER)


def _normalize_roster_id(roster_id: str) -> str:
    """Case-normalize webex EMAIL roster entries: 'webex:<value containing @>'.

    Emails are case-insensitive in their ASCII letters, and the webex adapter
    ASCII-lowercases the extracted ``personEmail`` before principal
    construction (``ascii_lower`` — never ``casefold()``, which would merge
    distinct SMTPUTF8 local parts like ``straße``/``strasse``), so the roster
    side must match or a roster ``Bob@Example.com`` silently drops the legit
    ``bob@example.com``. The ``'@'`` heuristic is safe: emails always contain
    ``@``, while WebEx personIds are base64/base64url and cannot. Entries
    without ``@`` (personIds) and non-webex platforms are returned untouched —
    dev-http behavior is unchanged.
    """
    platform, sep, platform_id = roster_id.partition(":")
    if sep and platform == "webex" and "@" in platform_id:
        return f"webex:{ascii_lower(platform_id.strip())}"
    return roster_id


class AllowlistGate:
    """Enforces `spec.gateway.allowlist` against inbound messages.

    Roster ids are platform-scoped principals ('<platform>:<platform_id>', e.g.
    'webex:nina@example.com' or 'dev-http:owner-1'); an inbound message's
    principal is derived the same way, so the mapping is a dictionary lookup.
    Admission by policy:

    - ``owner-only``: admit only roster members whose tier is ``owner``.
    - ``tiered``: admit any roster member (owner / trusted / guest).

    Tier currently gates ADMISSION only; the runtime has no per-tier capability
    differentiation yet, so a rostered guest is admitted under ``tiered`` — the
    honest current behavior, documented rather than implied.

    Webex EMAIL roster entries ('webex:<value containing @>') are ASCII-
    lowercased at construction to mirror the adapter's ``personEmail``
    normalization (see ``ascii_lower`` for why never ``casefold()``, and
    ``_normalize_roster_id`` for the documented ``@`` heuristic); personId
    entries and non-webex platforms keep their exact casing.

    Two roster entries that normalize to the SAME principal (including the
    same key spelled twice) are refused at construction with a ``ValueError``
    naming the principal: a last-wins dict build would let roster ORDER
    silently decide the tier (#45) — the gate's fail-loud posture, same as an
    unsupported policy.
    """

    def __init__(
        self,
        allowlist: GatewayAllowlist,
        *,
        audit_sink: AuditSink,
        identity: AgentIdentity,
    ) -> None:
        if allowlist.policy not in SUPPORTED_POLICIES:
            # The wiring guard should have rejected this spec long before boot;
            # fail loudly here too rather than default-admit an unknown policy.
            raise ValueError(
                f"gateway allowlist policy '{allowlist.policy}' is not enforced "
                f"(supported: {sorted(SUPPORTED_POLICIES)})"
            )
        self._policy = allowlist.policy
        self._roster: dict[str, str] = {}
        for entry in allowlist.roster:
            principal = _normalize_roster_id(entry.id)
            if principal in self._roster:
                # Last-wins would let roster ORDER silently decide the tier —
                # under owner-only, admission itself (#45). Fail loudly.
                raise ValueError(
                    f"gateway allowlist roster: entries collide on the normalized "
                    f"principal '{principal}' — remove or merge the duplicates"
                )
            self._roster[principal] = entry.tier
        self._audit_sink = audit_sink
        self._identity = identity

    @staticmethod
    def principal(message: InternalMessage) -> str:
        """The roster-id-shaped principal for this message: '<platform>:<platform_id>'."""
        return f"{message.channel.platform}:{message.sender.platform_id}"

    def admit(self, message: InternalMessage) -> InternalMessage | None:
        """Admit (resolving ``internal_user_id``) or drop and audit.

        On admission the sender's ``internal_user_id`` is set to the resolved
        roster principal (identity resolution at the gate). On rejection a
        `gateway_reject` record is written and None is returned — the caller
        MUST NOT enqueue, so nothing runs downstream.
        """
        principal = self.principal(message)
        tier = self._roster.get(principal)
        admitted = tier is not None and (self._policy != "owner-only" or tier == "owner")
        if not admitted:
            self._audit_reject(message, principal, tier)
            return None
        resolved = message.sender.model_copy(update={"internal_user_id": principal})
        return message.model_copy(update={"sender": resolved})

    def _audit_reject(self, message: InternalMessage, principal: str, tier: str | None) -> None:
        reason = (
            "sender not on roster"
            if tier is None
            else f"tier '{tier}' not admitted by policy '{self._policy}'"
        )
        self._audit_sink.append(
            AuditRecord(
                agent=self._identity,
                event="gateway_reject",
                trigger=Trigger(
                    message_id=message.id,
                    purpose=(
                        f"gateway dropped {message.channel.platform} message from an "
                        f"unpermitted sender ({reason})"
                    ),
                ),
                action=Action(
                    name="gateway.allowlist",
                    # Digest-not-payload: the principal id (not message content)
                    # identifies who was turned away; a reviewer wants that.
                    input_digest=_sha256(principal),
                    input_summary=f"sender {principal} on {message.channel.platform}: {reason}",
                ),
                outcome=Outcome(status="denied"),
            )
        )

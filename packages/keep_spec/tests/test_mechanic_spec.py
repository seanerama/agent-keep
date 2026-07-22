"""Stage-4 tests for the baked mechanic spec (permanent CI fixture).

`specs/mechanic.yaml` is THE spec the paired-mechanic image bakes in — this
file pins the properties the stage promises (the wiring/buildability gate
lives in agent_runtime's test_worker_analyzer.py):

a. the spec validates strictly against keep_spec (keep/v1);
b. the CI provider is `static` (hermetic — ADR 0001) with a non-empty script
   whose reply carries a citation marker (the smoke-mechanic.sh assertion);
c. the dev-http channel is gated OWNER-ONLY (one human drives the mechanic);
d. the single tool server is the read-only worker-analyzer local grant —
   exactly the three analyzer ops, every scope read-only, all auto-approved
   under the default-deny policy;
e. NO memory section (the mechanic never learns — no memory writes exist),
   no skills, no triggers;
f. the egress allowlist contains ONLY the model provider host — the perimeter
   the live flip runs inside; the bundle arrives by read-only mount, not
   network;
g. jsonl audit at the mechanic's OWN path (never the bundle dir — ADR 0011
   collision fix) + sqlite persistence + single session;
h. no secrets anywhere (contract agent-spec rule 3);
i. the slug extends the locked image identity (ADR 0001:
   ghcr.io/seanerama/agent-keep-mechanic).
"""

from pathlib import Path

from keep_spec import load_spec

REPO_ROOT = Path(__file__).parents[3]
SPEC_PATH = REPO_ROOT / "specs" / "mechanic.yaml"

ANALYZER_OPS = {"read_bundle", "explain_behavior", "propose_fix"}


def test_mechanic_validates_strictly() -> None:
    spec = load_spec(SPEC_PATH)
    assert spec.apiVersion == "keep/v1"
    assert spec.kind == "AgentSpec"
    assert spec.metadata.slug == "mechanic"  # image: ghcr.io/seanerama/agent-keep-mechanic


def test_static_provider_is_the_ci_substrate_with_a_cited_reply() -> None:
    spec = load_spec(SPEC_PATH)
    models = spec.spec.models
    assert models.provider == "static"
    assert models.static is not None and models.static.script
    # No remote provider path is selected anywhere: nothing in this spec can
    # make a network call, so CI is hermetic by construction.
    assert models.anthropic is None
    assert models.tiers == []
    # The script drives the REAL tool loop: a scripted analyzer tool call,
    # then a reply that carries the citation marker smoke-mechanic.sh asserts.
    script = models.static.script
    assert any(
        entry.startswith("TOOL_CALL ") and "analyzer.read_bundle" in entry for entry in script
    )
    reply = script[-1]
    assert "audit_record_ids" in reply  # the citation marker


def test_dev_http_channel_is_owner_only() -> None:
    spec = load_spec(SPEC_PATH)
    (channel,) = spec.spec.channels
    assert channel.type == "dev-http"
    allowlist = spec.spec.gateway.allowlist
    assert allowlist is not None
    assert allowlist.policy == "owner-only"
    (entry,) = allowlist.roster
    assert entry.id == "dev-http:owner"
    assert entry.tier == "owner"


def test_single_read_only_analyzer_grant_all_auto_approved() -> None:
    spec = load_spec(SPEC_PATH)
    (server,) = spec.spec.tools  # the SINGLE tool server: the analyzer
    assert server.name == "analyzer"
    assert server.transport.kind == "local"
    assert server.secretEnvs == []  # a local in-process tool needs no secret
    assert {grant.name for grant in server.allow} == ANALYZER_OPS
    assert all(grant.scope == "read-only" for grant in server.allow)
    approval = spec.spec.approval
    assert approval.policy == "allowlist-confirm-rest"  # default-deny
    assert set(approval.autoApprove) == {f"analyzer.{op}" for op in ANALYZER_OPS}


def test_no_memory_no_skills_no_triggers_absence_posture() -> None:
    """The mechanic never learns: NO memory section means no memory module in
    the image at all (absence semantics, contract agent-spec rule 2) — a
    poisoned bundle has nothing to write into."""
    spec = load_spec(SPEC_PATH)
    assert spec.spec.memory is None
    assert spec.spec.skills == []
    assert spec.spec.triggers is None


def test_egress_allowlist_is_exactly_the_model_provider_host() -> None:
    spec = load_spec(SPEC_PATH)
    assert spec.spec.sandbox.egress == ["api.anthropic.com:443"]
    assert spec.spec.sandbox.profile == "container"


def test_audited_durable_skeleton_posture() -> None:
    spec = load_spec(SPEC_PATH)
    assert spec.spec.gateway.queue == "in-process"
    assert spec.spec.sessions.mode == "single"
    assert spec.spec.persistence.tier == "sqlite"
    audit = spec.spec.observability.audit
    assert audit.sink == "jsonl"
    # The mechanic's OWN audit path — distinct from any bundle-dir mount point
    # (the deploy mounts the worker bundle elsewhere, read-only; ADR 0011).
    assert audit.path == "/var/lib/agent-keep/audit.jsonl"

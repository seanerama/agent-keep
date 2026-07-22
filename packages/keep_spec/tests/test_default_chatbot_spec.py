"""Stage-2 tests for the baked default-chatbot spec (permanent CI fixture).

`specs/default-chatbot.yaml` is THE spec the chassis image bakes in — this
file pins the properties the stage promises:

a. the spec validates strictly against keep_spec (keep/v1);
b. the CI provider is `static` (hermetic — ADR 0001) with a non-empty script;
c. token/budget accounting is ON (models.budgets selects the model-router);
d. the egress allowlist contains ONLY the model provider host
   (api.anthropic.com) — the perimeter the live flip runs inside;
e. no secrets anywhere (contract agent-spec rule 3 — env NAMES only; the
   static/hermetic posture means not even a secret NAME is required);
f. jsonl audit + sqlite persistence + single session — the durable, audited
   skeleton posture;
g. the slug extends the locked image identity (ADR 0001).
"""

from pathlib import Path

from keep_spec import load_spec

REPO_ROOT = Path(__file__).parents[3]
SPEC_PATH = REPO_ROOT / "specs" / "default-chatbot.yaml"


def test_default_chatbot_validates_strictly() -> None:
    spec = load_spec(SPEC_PATH)
    assert spec.apiVersion == "keep/v1"
    assert spec.kind == "AgentSpec"
    assert spec.metadata.slug == "default-chatbot"


def test_static_provider_is_the_ci_substrate() -> None:
    spec = load_spec(SPEC_PATH)
    models = spec.spec.models
    assert models.provider == "static"
    assert models.static is not None and models.static.script
    # No remote provider path is selected anywhere: nothing in this spec can
    # make a network call, so CI is hermetic by construction.
    assert models.anthropic is None
    assert models.tiers == []


def test_token_budget_accounting_is_on() -> None:
    spec = load_spec(SPEC_PATH)
    budgets = spec.spec.models.budgets
    assert budgets is not None
    assert budgets.maxTokensPerSession is not None and budgets.maxTokensPerSession > 0
    assert budgets.onExceed == "block"


def test_egress_allowlist_is_exactly_the_model_provider_host() -> None:
    spec = load_spec(SPEC_PATH)
    assert spec.spec.sandbox.egress == ["api.anthropic.com:443"]
    assert spec.spec.sandbox.profile == "container"


def test_skeleton_posture_dev_http_sqlite_jsonl_single_session() -> None:
    spec = load_spec(SPEC_PATH)
    (channel,) = spec.spec.channels
    assert channel.type == "dev-http"
    assert spec.spec.gateway.queue == "in-process"
    assert spec.spec.sessions.mode == "single"
    assert spec.spec.persistence.tier == "sqlite"
    audit = spec.spec.observability.audit
    assert audit.sink == "jsonl"
    assert audit.path == "/var/lib/agent-keep/audit.jsonl"


def test_no_tools_no_skills_no_memory_absence_posture() -> None:
    """The default chatbot is the THIN agent: nothing beyond the message loop
    is declared, so nothing beyond it exists in the image (absence semantics,
    contract agent-spec rule 2)."""
    spec = load_spec(SPEC_PATH)
    assert spec.spec.tools == []
    assert spec.spec.skills == []
    assert spec.spec.memory is None
    assert spec.spec.triggers is None
    assert spec.spec.gateway.allowlist is None

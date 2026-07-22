"""Stage-3 tests for the reference agent specs (permanent CI fixtures).

Covers the stage's testing requirements against `examples/outage-correlation.yaml`
and `examples/client-tracking.yaml`:

a. both specs validate strictly;
b. escape-hatch audit — every scalar string in both documents is an enum
   member, matches a declared pattern, or sits at an explicitly allowlisted
   free-text path (names and persona prose only);
c. envelope-shape diff — the two specs are identical in shape outside the
   `spec.*` section values (the diff IS the structural-personalization demo);
d. negative assertion — no shell / filesystem / config-push tool anywhere.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from keep_spec import load_spec
from keep_spec.models import EGRESS_HOST

REPO_ROOT = Path(__file__).parents[3]
SPEC_PATHS = {
    "outage-correlation": REPO_ROOT / "examples" / "outage-correlation.yaml",
    "client-tracking": REPO_ROOT / "examples" / "client-tracking.yaml",
}
SCHEMA_PATH = REPO_ROOT / "docs" / "spec-schema.json"

# ---------------------------------------------------------------- (b) escape hatches
#
# The complete set of free-text field paths the reference specs may use.
# Everything here is a NAME or persona/prompt PROSE — never load-bearing
# configuration. Every other string in the documents must be an enum member
# or match a schema-declared pattern. Keep this list short: growing it is a
# schema-review event, not a test fix.
FREE_TEXT_PATHS = frozenset(
    {
        "metadata.name",  # human-readable agent name
        "metadata.description",  # one-line description
        "spec.persona.identity",  # persona prose
        "spec.persona.tone",  # persona prose
        "spec.persona.instructions[]",  # persona prose
        "spec.triggers.activations[].source",  # event-source name
        "spec.triggers.activations[].event",  # event-type name
        "spec.triggers.activations[].prompt",  # trigger prompt prose
        "spec.gateway.allowlist.roster[].id",  # platform principal ids
        "spec.tools[].allow[].name",  # tool names as exposed by the server
        "spec.tools[].transport.command",  # stdio executable name
        # spec.approval.autoApprove[] left the allowlist in stage 5: entries
        # are now pattern-validated ('<server>.<tool>') and cross-checked
        # against declared grants — no longer free text.
        "spec.models.anthropic.model",  # model name
        "spec.models.tiers[].anthropic.model",  # model name
        "spec.observability.audit.path",  # audit sink file path
    }
)

# Fields whose format is enforced by a Pydantic model validator rather than a
# JSON-schema `pattern` (the exported schema shows a plain string).
EXTRA_PATTERN_PATHS = {"spec.sandbox.egress[]": EGRESS_HOST}


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def _load_schema() -> dict[str, Any]:
    return _load_yaml(SCHEMA_PATH)  # JSON is a YAML subset


def _deref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
    return node


_JSON_TYPE = {str: "string", bool: "boolean", int: "integer", dict: "object", list: "array"}


def _resolve(schema: dict[str, Any], node: dict[str, Any], value: Any) -> dict[str, Any]:
    """Deref and pick the union branch that applies to `value`."""
    node = _deref(schema, node)
    if "discriminator" in node and isinstance(value, dict):
        tag = value[node["discriminator"]["propertyName"]]
        return _resolve(schema, {"$ref": node["discriminator"]["mapping"][tag]}, value)
    for key in ("oneOf", "anyOf"):
        if key in node:
            branches = [_deref(schema, branch) for branch in node[key]]
            non_null = [branch for branch in branches if branch.get("type") != "null"]
            if len(non_null) > 1:  # scalar union (e.g. constraint values): pick by JSON type
                non_null = [b for b in non_null if b.get("type") == _JSON_TYPE[type(value)]]
            assert len(non_null) == 1, f"ambiguous non-discriminated union: {node}"
            return _resolve(schema, non_null[0], value)
    return node


def _audit_strings(
    schema: dict[str, Any], node: dict[str, Any], value: Any, path: str, violations: list[str]
) -> None:
    node = _resolve(schema, node, value)
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else key
            if child_node := node.get("properties", {}).get(key):
                _audit_strings(schema, child_node, item, child_path, violations)
                continue
            # Open mapping (e.g. ToolGrant.constraints): the KEY must match a
            # patternProperties pattern — the schema validates it, so it is
            # not free text — and the value is audited under that pattern.
            matching = [sub for pat, sub in node["patternProperties"].items() if re.match(pat, key)]
            assert len(matching) == 1, f"{child_path}: key matches {len(matching)} patterns"
            _audit_strings(schema, matching[0], item, child_path, violations)
    elif isinstance(value, list):
        for item in value:
            _audit_strings(schema, node["items"], item, f"{path}[]", violations)
    elif isinstance(value, str):
        if "const" in node:
            assert value == node["const"], f"{path}: {value!r} != const {node['const']!r}"
        elif "enum" in node:
            assert value in node["enum"], f"{path}: {value!r} not in enum {node['enum']}"
        elif pattern := (node.get("pattern") or EXTRA_PATTERN_PATHS.get(path)):
            assert re.match(pattern, value), f"{path}: {value!r} !~ {pattern!r}"
        elif path not in FREE_TEXT_PATHS:
            violations.append(f"{path}: free-text string {value!r} outside the allowlist")
    # ints / floats / bools / None are typed by construction — nothing to audit.


# ------------------------------------------------------------------------- (a) valid


@pytest.mark.parametrize("slug", sorted(SPEC_PATHS))
def test_reference_spec_validates_strictly(slug: str) -> None:
    spec = load_spec(SPEC_PATHS[slug])
    assert spec.metadata.slug == slug
    assert spec.spec.gateway.allowlist is not None  # both agents have an identity layer
    assert spec.spec.sandbox.egress, "egress must name its required hosts explicitly"


def test_worked_example_selections() -> None:
    """The outage spec expresses VISION.md's worked example, selection by selection."""
    spec = load_spec(SPEC_PATHS["outage-correlation"])
    webex = spec.spec.channels[0]
    assert webex.type == "webex"
    assert webex.transport == "webhook"
    assert webex.verification.method == "signature"
    assert spec.spec.gateway.queue == "redis"
    assert spec.spec.gateway.allowlist is not None
    assert spec.spec.gateway.allowlist.policy == "tiered"
    tiers = {entry.tier for entry in spec.spec.gateway.allowlist.roster}
    assert tiers == {"owner", "trusted"}  # owner plus the NOC roster
    assert spec.spec.triggers is not None
    assert {a.kind for a in spec.spec.triggers.activations} == {"message", "event-subscription"}
    assert spec.spec.memory is not None
    structure = spec.spec.memory.structure
    assert structure.kind == "vectors"  # retrieval memory ...
    assert structure.corpus == "agent-summaries"  # ... over summaries only (typed corpus)
    paging = next(server for server in spec.spec.tools if server.name == "noc-paging")
    page_room = next(grant for grant in paging.allow if grant.name == "page_room")
    assert page_room.constraints == {"room": "noc-outages"}  # typed room pin on the grant
    assert [tier.name for tier in spec.spec.models.tiers] == ["triage", "correlation"]
    assert all(tier.provider == "anthropic" for tier in spec.spec.models.tiers)
    assert spec.spec.observability.audit.sink == "jsonl"


# ---------------------------------------------------------------- (b) escape hatches


@pytest.mark.parametrize("slug", sorted(SPEC_PATHS))
def test_no_escape_hatches(slug: str) -> None:
    """Every scalar string is enum / pattern / allowlisted-name-or-prose.

    This is the "no configuration prose smuggled in strings" check: a string
    that is neither an enum member nor pattern-validated nor on the short
    FREE_TEXT_PATHS allowlist fails the stage's no-escape-hatch objective.
    """
    schema = _load_schema()
    data = _load_yaml(SPEC_PATHS[slug])
    violations: list[str] = []
    _audit_strings(schema, schema, data, "", violations)
    assert not violations, "escape hatches found:\n" + "\n".join(violations)


def test_escape_hatch_auditor_has_teeth() -> None:
    """The auditor flags a schema-valid free-text string at a non-allowlisted path."""
    schema = _load_schema()
    data = _load_yaml(SPEC_PATHS["outage-correlation"])
    # observability.health.path is a plain string in the schema and is NOT on
    # the free-text allowlist — the auditor must flag it.
    data["spec"]["observability"]["health"] = {"path": "/give-me-a-shell"}
    violations: list[str] = []
    _audit_strings(schema, schema, data, "", violations)
    assert violations == [
        "spec.observability.health.path: free-text string '/give-me-a-shell' outside the allowlist"
    ]


# ---------------------------------------------------------------- (c) envelope shape


def test_envelope_shape_identical_diff_confined_to_spec_sections() -> None:
    """`foundry diff` does not exist yet — assert its precondition instead.

    The two documents are identical in SHAPE everywhere: same top-level keys,
    same metadata keys, same set of spec.* sections. All differences live in
    the VALUES inside spec.* sections (plus metadata identity values). That
    diff is the structural-personalization demo.
    """
    outage = _load_yaml(SPEC_PATHS["outage-correlation"])
    client = _load_yaml(SPEC_PATHS["client-tracking"])

    assert list(outage) == list(client) == ["apiVersion", "kind", "metadata", "spec"]
    assert outage["apiVersion"] == client["apiVersion"]
    assert outage["kind"] == client["kind"]
    assert set(outage["metadata"]) == set(client["metadata"])
    assert outage["metadata"]["specVersion"] == client["metadata"]["specVersion"]
    assert set(outage["spec"]) == set(client["spec"]), (
        "both specs must declare the same spec.* sections; only section CONTENT may differ"
    )

    differing = {name for name in outage["spec"] if outage["spec"][name] != client["spec"][name]}
    # The personalization is structural: access, memory, tools, approval,
    # channels, and routing all differ between the two agents.
    assert {"channels", "gateway", "memory", "tools", "approval", "models"} <= differing


# ------------------------------------------------------- (d) forbidden capabilities

FORBIDDEN_TOOL_TERMS = ("shell", "bash", "terminal", "exec", "file", "config", "ssh")


def _strings_under(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings_under(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings_under(child)]
    return []


@pytest.mark.parametrize("slug", sorted(SPEC_PATHS))
def test_no_shell_filesystem_or_config_tools(slug: str) -> None:
    """Grep-level negative assertion over the parsed tools + approval sections.

    Neither agent's image may contain anything named or scoped for shell,
    filesystem, or config push — the capability must be ABSENT (contract
    agent-spec, binding rule 2), and the audit is over every string in the
    tool declarations: server names, transports, commands, args, tool grants.
    """
    data = _load_yaml(SPEC_PATHS[slug])
    surface = _strings_under(data["spec"]["tools"]) + data["spec"]["approval"]["autoApprove"]
    hits = [
        f"{term!r} in {text!r}"
        for text in surface
        for term in FORBIDDEN_TOOL_TERMS
        if term in text.lower()
    ]
    assert not hits, "forbidden capability vocabulary in tools:\n" + "\n".join(hits)


def test_single_write_grant_per_agent() -> None:
    """Each agent has exactly one read-write grant, and it is the declared one."""
    writes = {
        slug: [
            f"{server.name}.{grant.name}"
            for server in load_spec(path).spec.tools
            for grant in server.allow
            if grant.scope == "read-write"
        ]
        for slug, path in SPEC_PATHS.items()
    }
    assert writes == {
        "outage-correlation": ["noc-paging.page_room"],  # paging, room-pinned
        "client-tracking": ["crm.update_account_note"],  # note update, owner-confirmed
    }

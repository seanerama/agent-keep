"""Unit tests for keep/v1 spec validation (stage-1 testing requirements)."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from keep_spec import load_spec, validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
SKELETON_SPEC = REPO_ROOT / "examples" / "skeleton.yaml"


@pytest.fixture
def skeleton_data() -> dict[str, Any]:
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def test_skeleton_spec_is_valid() -> None:
    spec = load_spec(SKELETON_SPEC)
    assert spec.apiVersion == "keep/v1"
    assert spec.kind == "AgentSpec"
    assert spec.metadata.slug == "skeleton"
    assert spec.spec.channels[0].type == "dev-http"
    assert spec.spec.gateway.queue == "in-process"
    assert spec.spec.models.provider == "static"
    assert spec.spec.sandbox.egress == []


def test_unknown_top_level_field_fails(skeleton_data: dict[str, Any]) -> None:
    data = copy.deepcopy(skeleton_data)
    data["surprise"] = True
    with pytest.raises(ValidationError):
        validate_spec_data(data)


def test_unknown_nested_field_fails(skeleton_data: dict[str, Any]) -> None:
    data = copy.deepcopy(skeleton_data)
    data["spec"]["sandbox"]["network_mode"] = "host"
    with pytest.raises(ValidationError):
        validate_spec_data(data)


def test_unknown_metadata_field_fails(skeleton_data: dict[str, Any]) -> None:
    data = copy.deepcopy(skeleton_data)
    data["metadata"]["owner"] = "someone"
    with pytest.raises(ValidationError):
        validate_spec_data(data)


@pytest.mark.parametrize(
    "section",
    [
        "persona",
        "channels",
        "gateway",
        "sessions",
        "approval",
        "sandbox",
        "models",
        "observability",
        "persistence",
    ],
)
def test_missing_required_section_fails(skeleton_data: dict[str, Any], section: str) -> None:
    data = copy.deepcopy(skeleton_data)
    del data["spec"][section]
    with pytest.raises(ValidationError):
        validate_spec_data(data)


def test_wrong_api_version_fails(skeleton_data: dict[str, Any]) -> None:
    data = copy.deepcopy(skeleton_data)
    data["apiVersion"] = "foundry/v2"
    with pytest.raises(ValidationError):
        validate_spec_data(data)


def test_bad_slug_fails(skeleton_data: dict[str, Any]) -> None:
    data = copy.deepcopy(skeleton_data)
    data["metadata"]["slug"] = "Not A Slug"
    with pytest.raises(ValidationError):
        validate_spec_data(data)


def test_empty_static_script_fails(skeleton_data: dict[str, Any]) -> None:
    data = copy.deepcopy(skeleton_data)
    data["spec"]["models"]["static"]["script"] = []
    with pytest.raises(ValidationError):
        validate_spec_data(data)


def test_duplicate_tool_server_names_fail(skeleton_data: dict[str, Any]) -> None:
    """#34: two spec.tools entries sharing one server name would make grants,
    autoApprove entries, and the executor registry ambiguous — rejected at
    validation (additive check; specs with unique names are untouched)."""
    data = copy.deepcopy(skeleton_data)
    server: dict[str, Any] = {
        "name": "crm",
        "transport": {"kind": "http", "url": "https://crm.internal:8443/mcp"},
        "allow": [{"name": "get_account"}],
    }
    data["spec"]["tools"] = [server, {**copy.deepcopy(server), "allow": [{"name": "list_notes"}]}]
    with pytest.raises(ValidationError, match="duplicate server name"):
        validate_spec_data(data)


def test_duplicate_grant_names_on_one_server_fail(skeleton_data: dict[str, Any]) -> None:
    """#46: `allow: [{name: x, scope: read-only}, {name: x}]` on ONE server is
    an ambiguous grant — it would spawn MCP children and then die at executor
    construction. Rejected at load instead (additive check, stage-12 theme;
    specs with unique grant names per server are untouched)."""
    data = copy.deepcopy(skeleton_data)
    data["spec"]["tools"] = [
        {
            "name": "crm",
            "transport": {"kind": "http", "url": "https://crm.internal:8443/mcp"},
            "allow": [{"name": "get_account", "scope": "read-only"}, {"name": "get_account"}],
        }
    ]
    with pytest.raises(ValidationError, match="duplicate grant name"):
        validate_spec_data(data)


def test_same_grant_name_on_two_servers_is_fine(skeleton_data: dict[str, Any]) -> None:
    """The uniqueness check is PER SERVER: two servers may both grant a tool
    of the same name — the server name namespaces them ('<server>.<tool>')."""
    data = copy.deepcopy(skeleton_data)
    data["spec"]["tools"] = [
        {
            "name": "crm",
            "transport": {"kind": "http", "url": "https://crm.internal:8443/mcp"},
            "allow": [{"name": "get_account"}],
        },
        {
            "name": "billing",
            "transport": {"kind": "http", "url": "https://billing.internal:8443/mcp"},
            "allow": [{"name": "get_account"}],
        },
    ]
    validate_spec_data(data)


def test_additive_only_no_new_required_sections() -> None:
    """Additive proof (stage 2): the required sections are exactly stage 1's.

    Every section added since the walking skeleton (triggers, memory, skills,
    tools) is optional or defaulted, so examples/skeleton.yaml needs zero edits.
    """
    from keep_spec import SpecSections

    required = {name for name, field in SpecSections.model_fields.items() if field.is_required()}
    assert required == {
        "persona",
        "channels",
        "gateway",
        "sessions",
        "approval",
        "sandbox",
        "models",
        "observability",
        "persistence",
    }

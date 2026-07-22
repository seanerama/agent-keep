"""The spine test for keep_spec.diff — the semantic differ/applier.

Contract: contracts/spec-diff.md (frozen `foundry/spec-diff@1`). Exercised over
the three KNOWN repo specs (skeleton / outage-correlation / client-tracking).
The headline is ROUND-TRIP EXACT: for every ordered pair,
``dump_spec_data(apply_diff(a, diff_specs(a, b))) == dump_spec_data(b)`` and the
result re-validates as strict `keep/v1`.
"""

import ast
import itertools
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from keep_spec import AgentSpec, dump_spec_data, load_spec, validate_spec_data
from keep_spec.diff import (
    SPEC_DIFF_SCHEMA,
    SpecChange,
    SpecDiff,
    apply_diff,
    diff_specs,
)

REPO_ROOT = Path(__file__).parents[3]
DIFF_MODULE = Path(__file__).parents[1] / "src" / "keep_spec" / "diff.py"

SPEC_FILES = {
    "skeleton": REPO_ROOT / "examples" / "skeleton.yaml",
    "outage": REPO_ROOT / "examples" / "outage-correlation.yaml",
    "client": REPO_ROOT / "examples" / "client-tracking.yaml",
}

VALID_OPS = {"add", "remove", "change"}

#: Known credential-value shapes that must NEVER appear in a serialized diff —
#: the spec (and therefore the diff) carries env var NAMES, never values.
_SECRET_VALUE_PREFIXES = ("sk-", "xoxb-", "xoxp-", "ghp_", "glpat-", "AKIA")


@pytest.fixture(scope="module")
def specs() -> dict[str, AgentSpec]:
    return {name: load_spec(path) for name, path in SPEC_FILES.items()}


def _named_pairs() -> list[tuple[str, str]]:
    """Every ordered pair of DISTINCT known specs."""
    return list(itertools.permutations(SPEC_FILES, 2))


def _resolve(dump: dict[str, Any], spec_path: str) -> Any:
    """Walk a dotted `spec_path` into a canonical dump (KeyError if broken)."""
    node: Any = dump
    for segment in spec_path.split("."):
        node = node[segment]
    return node


def _path_present(dump: dict[str, Any], spec_path: str) -> bool:
    try:
        _resolve(dump, spec_path)
    except (KeyError, TypeError):
        return False
    return True


# --------------------------------------------------------------- 1. conformance


@pytest.mark.parametrize(("a_name", "b_name"), _named_pairs())
def test_conformance(specs: dict[str, AgentSpec], a_name: str, b_name: str) -> None:
    diff = diff_specs(specs[a_name], specs[b_name])
    wire = diff.wire_dict()

    # Self-describing: schema tag + identity + addressability.
    assert wire["schema"] == SPEC_DIFF_SCHEMA == "foundry/spec-diff@1"
    assert wire["diff_id"]
    assert wire["created_at"]
    assert wire["from"] == {
        "slug": specs[a_name].metadata.slug,
        "spec_version": specs[a_name].metadata.specVersion,
    }
    assert wire["to"] == {
        "slug": specs[b_name].metadata.slug,
        "spec_version": specs[b_name].metadata.specVersion,
    }

    paths = [c["spec_path"] for c in wire["changes"]]
    assert paths, "distinct known specs must differ"
    # Every op valid; every add/remove/change obeys its from/to null rule.
    for change in wire["changes"]:
        assert change["op"] in VALID_OPS
        if change["op"] == "add":
            assert change["from"] is None
        elif change["op"] == "remove":
            assert change["to"] is None
    # Unique + ordered by spec_path (deterministic). NOTE: the address space is
    # the WHOLE dump, so metadata.* paths appear too — do NOT assert startswith
    # 'spec.'.
    assert len(paths) == len(set(paths)), "spec_paths must be unique"
    assert paths == sorted(paths), "changes must be ordered by spec_path"

    # No secret VALUES anywhere in the serialized diff.
    blob = repr(wire)
    for prefix in _SECRET_VALUE_PREFIXES:
        assert prefix not in blob, f"serialized diff leaks a secret-shaped token ({prefix})"


def test_diff_is_deterministic(specs: dict[str, AgentSpec]) -> None:
    """Same pair -> byte-identical `changes` (ignoring the fresh id/timestamp)."""
    a, b = specs["skeleton"], specs["outage"]
    first = [c.model_dump(by_alias=True) for c in diff_specs(a, b).changes]
    second = [c.model_dump(by_alias=True) for c in diff_specs(a, b).changes]
    assert first == second


# --------------------------------------------------------------- 2. correctness


def _change_at(diff: SpecDiff, spec_path: str) -> SpecChange:
    matches = [c for c in diff.changes if c.spec_path == spec_path]
    assert len(matches) == 1, f"expected exactly one change at {spec_path}, got {len(matches)}"
    return matches[0]


def test_correctness_skeleton_to_outage(specs: dict[str, AgentSpec]) -> None:
    diff = diff_specs(specs["skeleton"], specs["outage"])

    queue = _change_at(diff, "spec.gateway.queue")
    assert (queue.op, queue.from_, queue.to) == ("change", "in-process", "redis")

    tier = _change_at(diff, "spec.persistence.tier")
    assert (tier.op, tier.from_, tier.to) == ("change", "sqlite", "postgres")

    provider = _change_at(diff, "spec.models.provider")
    assert (provider.op, provider.from_, provider.to) == ("change", "static", "anthropic")

    memory = _change_at(diff, "spec.memory")
    assert memory.op == "add"
    assert memory.from_ is None
    assert memory.to["structure"]["kind"] == "vectors"


def test_dict_recursion_whole_subtree_no_orphan_empty_dict(
    specs: dict[str, AgentSpec],
) -> None:
    """skeleton.spec.models.static (a dict) is absent in outage -> a WHOLE-SUBTREE
    remove at `spec.models.static`, never a descent that leaves an orphan
    `spec.models.static: {}` (which fails validate_spec_data)."""
    diff = diff_specs(specs["skeleton"], specs["outage"])

    static = _change_at(diff, "spec.models.static")
    assert static.op == "remove"
    assert static.from_ == {"script": ["Hello from the walking skeleton. The spine is connected."]}
    assert static.to is None

    # No change descends BELOW the removed subtree (no orphan-empty-dict path).
    assert not [c for c in diff.changes if c.spec_path.startswith("spec.models.static.")], (
        "dict-recursion rule violated: emitted a path inside the removed subtree"
    )

    # And the outage side gains the anthropic block as its own whole-subtree add.
    anthropic = _change_at(diff, "spec.models.anthropic")
    assert anthropic.op == "add"


def test_metadata_paths_appear(specs: dict[str, AgentSpec]) -> None:
    """The diff address space is the WHOLE dump — metadata.* ops appear too."""
    diff = diff_specs(specs["skeleton"], specs["outage"])
    slug = _change_at(diff, "metadata.slug")
    assert (slug.op, slug.from_, slug.to) == ("change", "skeleton", "outage-correlation")


# --------------------------------------------------------------- 3. round-trip


@pytest.mark.parametrize(("a_name", "b_name"), _named_pairs())
def test_round_trip_exact(specs: dict[str, AgentSpec], a_name: str, b_name: str) -> None:
    a, b = specs[a_name], specs[b_name]
    rebuilt = apply_diff(a, diff_specs(a, b))
    # EXACT: re-dumps equal to b ...
    assert dump_spec_data(rebuilt) == dump_spec_data(b)
    # ... and re-validates strict keep/v1 (apply_diff already validated;
    # assert the invariant a consumer relies on).
    assert validate_spec_data(dump_spec_data(rebuilt)) == b


@pytest.mark.parametrize("name", list(SPEC_FILES))
def test_diff_self_is_empty(specs: dict[str, AgentSpec], name: str) -> None:
    assert diff_specs(specs[name], specs[name]).changes == []


@pytest.mark.parametrize("name", list(SPEC_FILES))
def test_apply_empty_diff_is_identity(specs: dict[str, AgentSpec], name: str) -> None:
    spec = specs[name]
    empty = diff_specs(spec, spec)
    assert dump_spec_data(apply_diff(spec, empty)) == dump_spec_data(spec)


def test_apply_diff_raises_on_invalid_result(specs: dict[str, AgentSpec]) -> None:
    """apply_diff NEVER emits an invalid spec silently — a change that removes a
    required field is caught by validate_spec_data and raised."""
    spec = specs["skeleton"]
    diff = diff_specs(spec, spec)
    diff.changes.append(SpecChange(spec_path="spec.persona", op="remove", from_={}, to=None))
    with pytest.raises(ValidationError):
        apply_diff(spec, diff)


def test_apply_diff_bogus_path_raises_domain_error_naming_the_path(
    specs: dict[str, AgentSpec],
) -> None:
    """#101: a change whose spec_path addresses a non-existent node raises a
    domain-framed SpecDiffApplyError NAMING the path — NOT a bare KeyError from
    the apply walk. (Fails-before-fix: the apply loop raised `KeyError: 'does'`.)
    The Mechanic feeds apply_diff diffs from outside diff_specs, so a malformed
    path must fail legibly."""
    from keep_spec.diff import SpecDiffApplyError

    spec = specs["skeleton"]
    diff = diff_specs(spec, spec)
    diff.changes.append(
        SpecChange(spec_path="spec.does.not.exist", op="remove", from_="x", to=None)
    )
    with pytest.raises(SpecDiffApplyError) as excinfo:
        apply_diff(spec, diff)
    assert "spec.does.not.exist" in str(excinfo.value)
    assert not isinstance(excinfo.value, KeyError)


def test_apply_diff_valid_diffs_still_round_trip(specs: dict[str, AgentSpec]) -> None:
    """#101 is additive: the domain-error wrap does not disturb valid applies —
    a real diff still round-trips exactly."""
    a, b = specs["skeleton"], specs["outage"]
    rebuilt = apply_diff(a, diff_specs(a, b))
    assert dump_spec_data(rebuilt) == dump_spec_data(b)


# --------------------------------------------------------------- 4. citation link


@pytest.mark.parametrize(("a_name", "b_name"), _named_pairs())
def test_citation_link_resolves(specs: dict[str, AgentSpec], a_name: str, b_name: str) -> None:
    """Every change.spec_path resolves against the dump that should contain it,
    and the value there equals the recorded from/to (same property the
    interview-transcript pins)."""
    a, b = specs[a_name], specs[b_name]
    da, db = dump_spec_data(a), dump_spec_data(b)
    for change in diff_specs(a, b).changes:
        if change.op == "add":
            assert _resolve(db, change.spec_path) == change.to
            assert not _path_present(da, change.spec_path)
        elif change.op == "remove":
            assert _resolve(da, change.spec_path) == change.from_
            assert not _path_present(db, change.spec_path)
        else:  # change
            assert _resolve(da, change.spec_path) == change.from_
            assert _resolve(db, change.spec_path) == change.to


# --------------------------------------------------------------- 5. leaf-clean


def test_module_is_leaf_clean() -> None:
    """keep_spec.diff pulls NOTHING from agent_runtime or any interview
    package — the Mechanic + rebuild loop must import it without the runtime
    (ADR 0007)."""
    tree = ast.parse(DIFF_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "agent_runtime" not in imported
    # It imports only keep_spec + the stdlib/pydantic.
    assert "keep_spec" in imported

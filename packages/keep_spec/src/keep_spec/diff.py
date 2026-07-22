"""Semantic spec differ + applier for keep/v1 AgentSpecs.

Contract: contracts/spec-diff.md (frozen v1, `foundry/spec-diff@1`). ADR 0007 —
this lives INSIDE the existing `keep_spec` package and is LEAF-CLEAN: it
imports ONLY `keep_spec` models (`AgentSpec`, `dump_spec_data`,
`validate_spec_data`) plus the stdlib. The Mechanic + rebuild loop (Phase 5)
must import it without pulling in `agent_runtime` or any interview package.

The artifact is a structured, `spec_path`-keyed diff between two specs — the
same dotted address space the interview-transcript cites (ADR 0006), so a diff
entry and a transcript entry for the same field share the key. The round-trip
invariant a consumer may rely on: ``apply_diff(a, diff_specs(a, b))`` re-dumps
equal to ``b`` and re-validates as strict `keep/v1`.

`keep_spec` is a plain library with no sandbox/determinism restriction, so
`created_at`/`diff_id` use the stdlib directly (the same shape the
predecessor's interview package produced: an RFC 3339 UTC timestamp and a
uuid4 string).
"""

import copy
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from keep_spec.models import AgentSpec, dump_spec_data, validate_spec_data

#: The frozen schema tag every diff declares (contract: spec-diff, `@1`).
SPEC_DIFF_SCHEMA = "foundry/spec-diff@1"

#: The three diff operations (additive enum — a new op is a NEW contract).
Op = Literal["add", "remove", "change"]


class SpecDiffApplyError(ValueError):
    """`apply_diff` could not apply a change because its `spec_path` addresses a
    node that does not exist in the target spec's dump (#101).

    A domain-framed replacement for the bare `KeyError` the apply walk would
    otherwise raise. `diff_specs` never produces such a path, but the Mechanic
    (Phase 5) feeds `apply_diff` diffs from outside `diff_specs` (persisted or
    LLM-shaped), so a structurally-malformed path must fail NAMING the offending
    `spec_path`, not leak a stdlib `KeyError`. Additive — no `spec-diff` contract
    change (the wire shape is unchanged; only the error surface improves)."""


def _utc_now() -> str:
    """An RFC 3339 timestamp in UTC (the contract's `created_at`)."""
    return datetime.now(tz=UTC).isoformat()


class SpecRef(BaseModel):
    """Identity of one endpoint of a diff — `from`/`to` in the wire shape."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(description="The endpoint spec's metadata.slug.")
    spec_version: str = Field(description="The endpoint spec's metadata.specVersion.")


class SpecChange(BaseModel):
    """One change entry: a `spec_path`, an `op`, and the before/after values.

    `from`/`to` are Python keywords / carry arbitrary JSON values, so `from` is
    aliased. `extra="allow"` so a future producer (the Mechanic) may annotate a
    change with additive fields (e.g. a `rationale`) without breaking the read
    path — `apply_diff` only ever consults `spec_path`/`op`/`to`.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    spec_path: str = Field(description="Dotted path into the canonical dump.")
    op: Op = Field(description="add | remove | change.")
    from_: Any = Field(
        default=None,
        validation_alias="from",
        serialization_alias="from",
        description="Value in the `from` spec (null for an add).",
    )
    to: Any = Field(default=None, description="Value in the `to` spec (null for a remove).")


class SpecDiff(BaseModel):
    """A structured, semantic diff between two keep/v1 specs.

    Self-describing (`schema` tag, `diff_id`, `from`/`to` identity, `created_at`)
    and addressable (`changes` keyed by `spec_path`, ordered, unique). Serialize
    the wire form with ``model_dump(mode="json", by_alias=True)`` (see
    :func:`wire_dict`).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_: str = Field(
        default=SPEC_DIFF_SCHEMA,
        validation_alias="schema",
        serialization_alias="schema",
        description="Frozen schema tag.",
    )
    diff_id: str = Field(description="Unique id for this diff document.")
    created_at: str = Field(description="RFC 3339, UTC.")
    from_: SpecRef = Field(
        validation_alias="from", serialization_alias="from", description="Identity of the `a` spec."
    )
    to: SpecRef = Field(description="Identity of the `b` spec.")
    changes: list[SpecChange] = Field(
        default_factory=list,
        description="Changes ordered by spec_path (deterministic); paths unique.",
    )

    def wire_dict(self) -> dict[str, Any]:
        """The wire shape (aliased keys: `schema`, `from`) as plain JSON data."""
        return self.model_dump(mode="json", by_alias=True)


def _walk(
    a: dict[str, Any],
    b: dict[str, Any],
    prefix: str,
    changes: list[SpecChange],
) -> None:
    """Diff two dicts into `changes`, recursing per the dict-recursion rule.

    Recurse into a key ONLY where it is a dict on BOTH sides; otherwise emit a
    WHOLE-SUBTREE add/remove/change at that path. A key present-as-dict on one
    side and absent/scalar on the other is a whole-subtree op, never a
    recursion (removing `spec.models.static.script` must emit
    `remove spec.models.static`, never leave `spec.models.static: {}`, which
    fails `validate_spec_data`). Lists are diffed WHOLE-VALUE.
    """
    for key in a.keys() | b.keys():
        path = f"{prefix}.{key}" if prefix else key
        in_a = key in a
        in_b = key in b
        if in_a and not in_b:
            changes.append(SpecChange(spec_path=path, op="remove", from_=a[key], to=None))
        elif in_b and not in_a:
            changes.append(SpecChange(spec_path=path, op="add", from_=None, to=b[key]))
        else:
            va = a[key]
            vb = b[key]
            if va == vb:
                continue
            if isinstance(va, dict) and isinstance(vb, dict):
                _walk(va, vb, path, changes)
            else:
                changes.append(SpecChange(spec_path=path, op="change", from_=va, to=vb))


def diff_specs(a: AgentSpec, b: AgentSpec) -> SpecDiff:
    """Compute the semantic diff of `a` -> `b` over their canonical dumps.

    Diffs `dump_spec_data(a)` vs `dump_spec_data(b)` (the
    `model_dump(mode="json", exclude_unset=True)` form). `spec_path`s are dotted
    over the WHOLE dump — `metadata.*` ops appear alongside `spec.*`. Changes are
    unique by path and ordered by `spec_path`, so the same pair always yields an
    identical diff.
    """
    changes: list[SpecChange] = []
    _walk(dump_spec_data(a), dump_spec_data(b), "", changes)
    changes.sort(key=lambda c: c.spec_path)
    return SpecDiff(
        diff_id=str(uuid4()),
        created_at=_utc_now(),
        from_=SpecRef(slug=a.metadata.slug, spec_version=a.metadata.specVersion),
        to=SpecRef(slug=b.metadata.slug, spec_version=b.metadata.specVersion),
        changes=changes,
    )


def apply_diff(a: AgentSpec, d: SpecDiff) -> AgentSpec:
    """Apply `d`'s changes onto `dump_spec_data(a)` and re-validate the result.

    The result is validated with `validate_spec_data` (strict `keep/v1`) and
    raised on ANY violation — `apply_diff` NEVER emits an invalid spec silently.
    (Validation stops at `keep_spec.validate_spec_data`, NOT
    `ensure_buildable` — that lives in `agent_runtime`; keeping it here is what
    keeps this module leaf-clean.)
    """
    data = dump_spec_data(a)
    for change in d.changes:
        segments = change.spec_path.split(".")
        parent = data
        try:
            for segment in segments[:-1]:
                parent = parent[segment]
            leaf = segments[-1]
            if change.op == "remove":
                del parent[leaf]
            else:  # add | change
                parent[leaf] = copy.deepcopy(change.to)
        except (KeyError, TypeError) as exc:
            # The change targets a path that does not exist in the target dump
            # (a missing intermediate segment, or the leaf of a `remove`).
            # Domain-frame it (#101) naming the offending spec_path instead of
            # leaking a bare KeyError/TypeError from the walk.
            raise SpecDiffApplyError(
                f"cannot apply change: spec_path {change.spec_path!r} targets a "
                f"non-existent node in the spec ({change.op})"
            ) from exc
    return validate_spec_data(data)

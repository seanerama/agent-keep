# Contract: log-egress

- **Status:** frozen v1 (re-frozen under Agent Keep, 2026-07-22)

> **Carried from Agent Foundry** (`~/projects/Agent-Factorio/contracts/log-egress.md`,
> frozen there at v1) per the transplant manifest — proven shape, carried not
> rewritten. The body below is verbatim from the source. Read it under this
> identity mapping: `foundry_spec` → `keep_spec`, `foundry/v1` → `keep/v1`,
> `agent-foundry` → `agent-keep`, `/etc/agent-foundry/` → `/etc/agent-keep/`.
>
> **Successor deltas (normative):** the bundle is `<slug>.yaml` + `<slug>.audit.jsonl` ONLY — `<slug>.interview.json` does not exist in Agent Keep (interview left behind). The mechanic MUST handle transcript-less bundles (the predecessor's ADR 0011 flagged this crash; fixing it is part of the transplant).

---


- **Status:** frozen v1
- **Owner:** the worker-analyzer component (`agent_runtime`) — the read-only
  reader of a worker's artifact bundle (ADR 0009/0010).

How a paired-but-separate **mechanic** READS a worker's ground truth — the
vision v0.2 Phase 5 seam. The mechanic reads; it NEVER writes to the worker.
Its outputs are explanations (citing the audit record + the transcript decision)
and proposed `spec-diff`s — the remedy the human approves and the factory
rebuilds. This is the artifact side of "every agent ships with its mechanic."

## Exposes

- The **worker artifact bundle** (read-only) for one worker at a bundle
  directory located by `MECHANIC_WORKER_DIR` (deploy-config env, never in the
  mechanic's spec):
  - `<slug>.yaml` — the worker's `foundry/v1` spec (contract: `agent-spec`).
  - `<slug>.interview.json` — the worker's transcript (contract:
    `interview-transcript` v1) — the legislative-history citation source.
  - the worker's append-only audit log (jsonl, one `audit-record` v1 per line,
    at the worker's `spec.observability.audit.path`, co-located in the bundle).
- The mechanic's **outputs**, riding existing contracts: an EXPLANATION =
  `{ audit_record_id, decision_id, spec_path, statement }` (cite the audit
  record + the transcript decision that set the field); a REMEDY = a `spec-diff`
  `SpecDiff` produced via `foundry_spec.diff`, annotated with an additive
  `rationale` (spec-diff forward-compat). NEVER a write to the worker.

## Consumes

- `audit-record` v1 (READS the jsonl lines — parses, never edits),
  `interview-transcript` v1 (READS — the spec_path↔decision citation),
  `agent-spec` (READS the worker spec; COMPOSES a diff against it — no schema
  edit), `spec-diff` v1 (PRODUCES the remedy), the shared `mark_untrusted`/defang
  (fences bundle content in prompt assembly).

## Schema / wire

The bundle is a directory; the mechanic reads it read-only:

```
$MECHANIC_WORKER_DIR/
  <slug>.yaml            # agent-spec (foundry/v1)          — read-only
  <slug>.interview.json  # interview-transcript@1            — read-only
  <slug>.audit.jsonl     # one audit-record v1 per line      — read-only, append-only by the worker
```

**Co-location is a deploy convention THIS contract introduces** — it is not
currently emitted by the runtime. The worker writes its audit jsonl to
`spec.observability.audit.path` (an arbitrary path, `jsonl_audit.py`), and the
spec+transcript pair is emitted by the interview/runbook. Assembling them into a
bundle dir with the `<slug>.audit.jsonl` name (audit.path pointed into the
bundle, or copied+renamed) is deploy-time wiring, exercised when the worker/
mechanic pairing is deployed (parked). The walking skeleton uses a fixture
bundle.

Explanation shape (the mechanic's read-only answer):

```json
{
  "question": "<the why-question, e.g. 'why didn't it page the NOC?'>",
  "spec_path": "spec.sessions.history.topK",
  "decision_id": "sessions.history.topK",
  "audit_record_ids": ["<audit record uuid>", "..."],
  "statement": "<demarcated, cited explanation — worker content fenced>"
}
```

Rules (invariants a consumer may rely on):

- **READ-ONLY, three layers** (ADR 0009 — NOT the tool-scope gate, which is
  inert for local tools): (1) **diff-only** — the analyzer has no apply/write
  operation at all, so it structurally cannot mutate the worker; its only
  change-proposal is a `SpecDiff`; (2) the bundle is mounted read-only at
  deploy; (3) the reader opens files in read mode only. The mechanic MUST NOT
  create/modify/delete any bundle file. (The analyzer's `read-only` tool grant
  is descriptive — it declares intent and appears in the one-page review — but
  is not executor-enforced for a local tool.)

  **Amendment (2026-07-14):** the executor now enforces read-only scope for
  local tools too (#109 / stage 34, `agent_runtime/executor.py` ~396–420): a
  `read-only` grant bound to an op that provides no read-only evidence
  (`LocalTool.read_only`) is refused at the local-tools boundary, mirroring
  the MCP `readOnlyHint` path. Scope only *tightens* — a read-write grant
  still runs any op unchanged. The "inert" / "not executor-enforced" framing
  above is **superseded for current code**. The Mechanic's read-only
  guarantee still does **not** depend on this scope gate — it rests on the
  three ADR 0009 layers above (diff-only, read-only mount, read-mode opens);
  the analyzer's ops are already `read_only=True`, so the gate never fires
  for them.
- **UNTRUSTED:** all bundle content (audit `input_summary`, transcript answers,
  any worker-conversation-derived text) is DEMARCATED DATA in the mechanic's
  prompt assembly (`mark_untrusted`), never instructions. Read-only/diff-only is
  the backstop.
- **CITED:** an explanation references at least the `spec_path` (the field the
  question is about) and the `decision_id` (transcript) that set it, plus any
  `audit_record_ids` bearing on it. The `spec_path` is a shared-address-space
  join between the transcript and spec-diff (ADR 0006/0007). The AUDIT side is
  joined heuristically — audit-record carries `id` + `action.name` (tool/op
  name), NOT a `spec_path` — so `audit_record_ids` are found by mapping the
  field to its owning tool/action, not by a `spec_path` match.
- **NO SECRET VALUES:** the bundle holds env-var NAMES only (spec/transcript
  posture); the mechanic never surfaces a credential value.
- **DIFF-ONLY REMEDY:** the mechanic's only change-proposal is a `SpecDiff`
  (never an applied change). Applying it is the human-approved factory rebuild,
  outside this contract.

## Versioning

Frozen at **v1**. Changes are **additive only** — a breaking change is a NEW
contract, not an edit (framework-spec §4.3). Forward-compatible: a future
bundle `manifest` file, a streaming/incremental audit read, or additional
read-only artifact kinds are additive. This contract only READS
`audit-record`/`interview-transcript`/`agent-spec` and PRODUCES `spec-diff` — it
never edits any of them.

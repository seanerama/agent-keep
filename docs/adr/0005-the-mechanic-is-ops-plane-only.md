# 0005. The mechanic is ops-plane only

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

The successor brief records this as operator-confirmed (2026-07-16), but the
predecessor's ADR trail (0009–0011) lives in a repo this one will never clone,
so the successor needs its own record. The chassis pairs every agent with a
mechanic. The question any future stage will ask: how far can the mechanic's
authority reach?

## Decision

Two planes, hard boundary:

- **The mechanic runs the body.** It operates the container — restart, pause,
  budget throttle, health remediation — and explains behavior strictly from
  recorded evidence (the read-only artifact bundle: spec, audit log), citing
  sources. Carried mechanisms: read-only bundle seam (predecessor ADR 0009,
  `log-egress` contract), mechanic-as-stamped-agent from the same runtime
  (predecessor ADR 0010).
- **The mind is human-governed.** The mechanic NEVER edits what the agent is:
  spec changes are proposed as diffs and remain human-approved. It has no write
  path to the worker's spec, prompt, tools, or memory.
- Widening the mechanic's authority in any direction is its own future ADR with
  guardrails, never an incidental scope creep in a stage.

## Alternatives considered

- **Self-healing mechanic (edits specs autonomously):** rejected by the
  operator; collapses the two-plane trust model and makes the audit trail
  circular (the observer editing the observed).
- **No mechanic in v1:** rejected; the paired mechanic is part of the chassis
  identity ("the container that houses it, the mechanic that operates that
  container") and the brief puts it in the walking skeleton envelope.

## Consequences

- Ops-plane actions (restart/pause/throttle) need a narrow, auditable actuator
  seam on the host side — scoped like the predecessor's sudo-helper pattern,
  never general shell access. Its concrete shape is an architect/planner call
  when that stage lands.
- Every mechanic answer is citable evidence or it isn't given — carrying the
  provenance principle forward.
- Spec-change velocity is bounded by human review. That is the point.

# Assessment: backlog triage — hardening/quality stages 16–19

- **Date:** 2026-07-22
- **Input:** open issues #32, #10, #11, #24 (operator confirmed order + approach A for #24)
- **Decision:** ACCEPT #32/#10/#11/#24 as stages 16–19; DEFER #21; Google (issue #15) parked by operator

## Claim / reality verification (against live code, 2026-07-22)

| Item | Claim | Reality | Verdict |
| --- | --- | --- | --- |
| #10 | wiring matcher doesn't lowercase host; keep_spec does | `wiring.py:402 return host == entry_host` (only entry lowercased :399); `egress.py:40-41` lowercases both | ✅ live |
| #11 | proxy binds 0.0.0.0; no head-read timeout | `runner.py:29 DEFAULT_HOST="0.0.0.0"`; `proxy.py:263 readuntil` with no `wait_for` | ✅ live |
| #24 | egress record flushes on close only | `proxy.py:259 _record(...allowed, counter)` returned post-`_tunnel`, appended :212 in finally | ✅ live |
| #32 | no lint hook / local≠CI | no `scripts/lint.sh`/`.githooks`/hook; 2 CI round-trips lost (stages 14,15) | ✅ live |

No false premises.

## Sequencing (operator-confirmed order)

1. **Stage 16 (#32, chore)** — FIRST: the pre-push lint hook + pinned shellcheck
   version stops local-vs-CI lint surprises from costing a round-trip on every
   later stage. No deps.
2. **Stage 17 (#10, bug)** — one-line wiring matcher fix + de-mask the parity
   test. No deps.
3. **Stage 18 (#11, chore)** — proxy ingress hardening (bind scope + head-read
   timeout + conn cap). No deps.
4. **Stage 19 (#24, feature)** — real-time `open` record, approach A. **Depends on
   18** (both touch `keep_egress/proxy.py`; serialized to avoid a rebase).

16/17/18 are independent; 19 gates on 18.

## Contract safety

- #10, #11, #32: no contract touch (correctness/robustness/tooling).
- **#24 (stage 19): an ADDITIVE amendment to `contracts/egress-observation.md`** —
  a new `action: open` value + a dated two-phase-model note. Existing `connect`/
  `denied` records stay byte-for-byte unchanged; a consumer filtering on `connect`
  never sees `open`. This is within the contract's own "additive record kind"
  clause and mirrors the `audit-record` 2026-07-14 additive amendment precedent —
  NOT a new contract, NOT a breaking edit. Flagged so the Reviewer verifies
  additivity. (Operator was consulted; approach A confirmed with the amendment
  understood.)

## Deferred / out

- **#21 (per-cloud provisioners + in-tenant GPU):** architecture-level (which cloud
  first, Terraform vs CLI, in-tenant GPU) and already blessed as on-demand
  conveniences by ADR 0007. Belongs in its own `/verity:architect` session when
  cloud expansion is prioritized — not a plan-session stage.
- **Google provider (#15):** parked by the operator.

# Stage 19: Real-time egress audit: emit an 'open' record at CONNECT establish (approach A)

- **Type:** feature
- **Depends on:** 18

## Objectives

Fix issue #24 (approach A, operator-selected): allowed HTTPS egress records
currently flush only on tunnel CLOSE (httpx pools the connection), so a live
audit doesn't show in-flight cloud model calls. Emit an **`open`** record at
CONNECT establish (the allow decision + matched entry, in real time), keeping the
existing on-close record (which carries the final byte counts). Real-time
observability + preserved byte accounting.

## What to build

- **`packages/keep_egress/src/keep_egress/proxy.py`**: when an allowed CONNECT
  tunnel is ESTABLISHED (before the bidirectional relay begins), append an
  `open` record to the audit sink: same target/verdict(`allowed`)/matched_entry/
  run-correlation as the close record, `action: open`, bytes not-yet-known
  (0/null). The existing on-close record stays exactly as-is (`action: connect`,
  final `bytes_up`/`bytes_down`). Absolute-form HTTP (which already flushes on
  close-per-request via forced `Connection: close`) does NOT need an open record —
  scope this to the CONNECT (tunnel) path where the deferral occurs. Pair the two
  records by the run-correlation key / a shared connection id so a consumer can
  match open↔close.
- **`packages/keep_egress/src/keep_egress/records.py`**: add `open` to the
  `action` enum (additive); keep the frozen field roster otherwise unchanged.
- **`contracts/egress-observation.md` — ADDITIVE amendment** (dated, mirroring the
  audit-record 2026-07-14 amendment precedent): document the two-phase model for
  allowed CONNECT tunnels — an `action: open` record at establish (real-time, no
  byte counts) + the existing `action: connect` record on close (final bytes).
  Existing `connect` records are byte-for-byte unchanged; `open` is a NEW additive
  action a consumer filtering on `connect` never sees. State explicitly that this
  is additive (the "one record per attempt" description now reads: allowed tunnels
  emit open+close; denied/HTTP still emit one). Do NOT alter any existing field or
  the `connect`/`denied` records.

## Interface contracts

- **Amends (additively):** `contracts/egress-observation.md` — new `action: open`
  event; frozen fields + existing records unchanged. Within `audit-record` v1
  (additive record/action kind, exactly what the contract's "additive record kind"
  clause permits). NOT a new contract, NOT a breaking edit. The Reviewer verifies
  additivity (existing `connect`/`denied` records identical).

## Testing requirements

- Unit (keep_egress): an allowed CONNECT emits an `open` record at establish AND a
  `connect` record on close; the two correlate; the `open` record has the allow
  verdict + matched entry + no/zero bytes; the `connect` record has final bytes.
  A DENIED CONNECT emits NO `open` record (denied before establish) — still one
  `denied` record. Absolute-form HTTP still emits exactly one `connect` record
  (no `open`). Records validate against the amended shape.
- Container: an allowed tunnel through the proxy produces the open record in
  real time (before the tunnel closes) — a test that reads the audit while the
  tunnel is still open sees the `open` record (this is the whole point).
- Existing egress deny/allow/no-route tests stay green.

## Acceptance conditions

- [ ] Kill-switch: N/A — this is additive observability, always-on (a chassis has
      no feature flags); recorded here
- [ ] Observably-works: an in-flight allowed cloud call shows an `open` egress
      record in real time (before close), with the `connect`+bytes record still on
      close (ties to the live re-verify of the deferred-record fix)
- [ ] Additive amendment ONLY — existing `connect`/`denied` records byte-for-byte
      unchanged; new `open` action is ignorable by existing consumers
- [ ] Existing suite stays green; CI all-green

## Pipeline test: YES — the container job asserts the real-time open record

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

- **`packages/keep_egress/src/keep_egress/records.py`**: two ADDITIVE changes,
  both following the `audit-record` 2026-07-14 `trace_id` precedent (add a new
  value / a new optional field — existing consumers ignore the new key, nothing
  existing is removed or repurposed):
  - `action: Literal["connect"]` → `Literal["connect", "open"]` (new value).
  - a `connection_id: str` field (default_factory uuid4) that pairs the open+close
    records of one CONNECT (both carry the SAME connection_id); single records
    (denied, allowed-HTTP) get their own unique one. This is the correlation seam.
    Note honestly: this new field appears on ALL egress records going forward —
    that is *additive* (a new key, ignorable by existing consumers), the same kind
    of change as audit-record's additive `trace_id`. It is NOT a removal or a
    field/semantic change to `id/target/verdict/matched_entry/bytes_*/run_id`.
- **`packages/keep_egress/src/keep_egress/proxy.py`**: when an allowed CONNECT
  tunnel is ESTABLISHED (after writing 200 Established, before `_tunnel`), append
  an `open` record: `action: open`, same target/`verdict:allowed`/matched_entry,
  bytes 0, and the connection's shared `connection_id`. The existing on-close
  record stays as-is except it now also carries that same `connection_id`
  (`action: connect`, final bytes). Absolute-form HTTP (already flushes
  per-request via forced `Connection: close`) does NOT get an `open` record —
  scope the open record to the CONNECT/tunnel path where the deferral occurs.
  Make the open-record append resilient the same way the close append is (a
  failing sink append must not break the tunnel or leak a connection slot — reuse
  the stage-18 pattern).
- **`contracts/egress-observation.md` — ADDITIVE amendment** (dated, mirroring the
  audit-record 2026-07-14 precedent): document the two-phase model for allowed
  CONNECT tunnels — an `action: open` record at establish (real-time, no byte
  counts) + the existing `action: connect` record on close (final bytes),
  correlated by `connection_id`. State explicitly this is ADDITIVE: a new action
  value + a new field, existing consumers filtering on `action: connect` and
  reading the known fields are unaffected; no existing field is removed or
  repurposed. The "one record per attempt" line now reads: allowed CONNECT tunnels
  emit open+close (paired by connection_id); denied/HTTP still emit one.

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
- [ ] Additive amendment ONLY (audit-record `trace_id` precedent): a new `open`
      action value + a new `connection_id` field; NO existing field removed or
      repurposed; a consumer filtering `action: connect` and reading the known
      fields is unaffected. Denied/HTTP still emit exactly one record.
- [ ] Existing suite stays green; CI all-green

## Pipeline test: YES — the container job asserts the real-time open record

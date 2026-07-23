# Stage 17: Fix egress matcher case divergence: lowercase host in the wiring matcher

- **Type:** bug
- **Depends on:** none

## Objectives

Fix issue #10: `agent_runtime.wiring._egress_entry_allows` lowercases only the
allowlist ENTRY, not the incoming `host` (`return host == entry_host`,
wiring.py:402), while `keep_spec.egress.egress_entry_allows` lowercases BOTH
(egress.py:40-41). They diverge on an uppercase host — latent (both current
callers pre-lowercase; the proxy uses the keep_spec matcher, verified live), but
the keep_spec docstring claims "identical semantics" and the parity test masks
the divergence by pre-lowercasing when it calls the wiring matcher. Make the
guarantee real.

## What to build

- `packages/agent_runtime/src/agent_runtime/wiring.py`: in
  `_egress_entry_allows`, lowercase the incoming `host` too (so the wiring matcher
  matches `keep_spec.egress`'s case-insensitive behavior exactly). One-line, surgical.
- The parity test (`packages/keep_egress/tests/test_matcher.py` or wherever the
  wiring-vs-keep_spec parity is asserted): DROP the pre-normalization that calls
  the wiring matcher with `host.lower()`, so the two matchers are compared on the
  SAME raw input — a future regression (one lowercasing, the other not) would then
  actually fail. Add an explicit uppercase-host case to the matrix.

## Interface contracts

- **Consumes:** no contract change; this is a correctness fix bringing the two
  matcher implementations into genuine agreement. `contracts/` untouched.

## Testing requirements

- Regression: a test asserting `_egress_entry_allows(entry, "API.ANTHROPIC.COM", 443)`
  matches an `api.anthropic.com:443` entry (fails BEFORE the fix — wiring returns
  False on uppercase; passes after). The parity test, with pre-normalization
  removed, now compares raw inputs including uppercase and stays green only
  because both lowercase.
- No behavior change for the already-lowercased callers (build-time
  cross-validation, the proxy) — existing suites stay green.

## Acceptance conditions

- [ ] Reproduction captured (uppercase host → wiring False, keep_spec True before)
      + regression test (fails before, passes after)
- [ ] Parity test no longer masks the divergence (pre-normalization removed)
- [ ] `contracts/` untouched; no behavior change for existing lowercased callers
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO

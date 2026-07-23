# Stage 18: Egress proxy ingress hardening: bind internal interface, head-read timeout, conn cap

- **Type:** chore
- **Depends on:** none

## Objectives

Close issue #11's ingress-surface items on the egress proxy: it binds `0.0.0.0`
(reachable from both its networks — wider than the contract's "reachable ONLY
from the paired agent"), and `_read_request` awaits `readuntil` with no timeout
and no connection cap (slowloris / half-open exposure, mitigated only by
topology). Defense-in-depth; not a functional bug.

## What to build

- **Bind scope** (`packages/keep_egress/src/keep_egress/runner.py` /
  `proxy.py`): make the proxy listen on the INTERNAL network interface only, not
  all interfaces, so the control port is reachable only from the paired worker —
  matching `contracts/egress-observation.md` §Exposes ("reachable ONLY from the
  paired agent container"). Mechanism: the proxy's default bind should be the
  internal-net alias/interface, not `0.0.0.0`. If the container can't easily know
  its internal-only interface at bind time, an acceptable alternative is to keep
  `0.0.0.0` inside the container BUT document + verify the deploy topology never
  publishes the proxy port on the egress net (it already doesn't — the proxy has
  no `-p`; issue #11's real fix is ensuring the egress-net leg has no listener
  reachable from co-resident containers). Builder picks the cleanest genuinely-
  tightening option and documents why; the goal is: nothing but the worker can
  reach the proxy control port.
- **Head-read timeout**: wrap the request-head `readuntil` in `asyncio.wait_for`
  with a bounded timeout (config via env, sane default e.g. 10s); a slow/half-open
  client is dropped (audited `denied`/`invalid` as a malformed attempt, or closed
  cleanly) rather than holding a task forever.
- **Connection cap**: bound max concurrent client connections (config via env,
  sane default); excess connections are refused/queued, not unbounded.
- Keep enforcement + audit behavior otherwise identical; these are ingress
  robustness only, not a change to the allowlist/audit semantics.

## Interface contracts

- **Consumes:** `contracts/egress-observation.md` — this brings the implementation
  INTO LINE with the frozen §Exposes "reachable ONLY from the paired agent" clause;
  no contract change (a malformed/timed-out attempt still audits per the existing
  record shape). `contracts/` untouched.

## Testing requirements

- Unit (extend the keep_egress proxy tests): a client that sends no complete head
  within the timeout is dropped (no indefinite hang) and does not wedge the proxy;
  the connection cap refuses/queues beyond the limit; a normal request still
  succeeds. Bind-scope: a test/assertion that the listener is not on `0.0.0.0`
  (or that the chosen tightening holds).
- Existing egress container tests (deny/allow/no-route) stay green — enforcement
  unchanged.

## Acceptance conditions

- [ ] Exit-state: proxy control port reachable only from the paired worker
      (bind tightened or topology-verified); head-read timeout + connection cap in
      place with sane env-configurable defaults
- [ ] Allowlist enforcement + audit semantics unchanged (existing egress tests green)
- [ ] `contracts/` untouched; additive/robustness only
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO

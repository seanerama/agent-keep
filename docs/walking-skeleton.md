# Walking skeleton (Stage 0) — thin agent, thick chassis

> Architect handoff to `/verity:plan`. This stage blocks ALL feature stages;
> it proves the spine end-to-end. Per the brief: a simple default chatbot
> inside the FULL envelope from day one.

## Definition

The thinnest end-to-end slice that compiles, runs, passes real tests, goes
green in CI, and deploys to the chosen target:

1. **Transplant the core** from `~/projects/Agent-Factorio` (read-only source;
   copy out, never clone): `agent_runtime` (message loop, gateway, executor,
   audit, sessions, provider seam with `static` + `anthropic`, `dev_http`
   channel, `jsonl_audit`, memory queue, single-session persistence) and
   `foundry_spec` → **`keep_spec`** (renamed; narrowed later, imports fixed
   now). uv workspace, lockfile committed, ruff + mypy-strict + pytest wired.
2. **A default chatbot spec** (`examples/` or `specs/`): dev-http channel,
   static provider for CI / anthropic for live, sqlite-or-single-session
   persistence, jsonl audit, egress allowlist containing only the model
   provider host.
3. **The egress proxy choke point** (NEW — contract `egress-observation` v1,
   ADR 0002): paired proxy container; agent's outbound rides HTTP(S)_PROXY
   through it; runtime allowlist enforcement fail-closed; every attempt
   audited. In CI the static provider makes zero network calls, so CI proves
   the DENY path (an attempted outbound to a non-allowlisted host is refused
   and audited) — the ALLOW path is proven live against the Anthropic API.
4. **Token/cost accounting + audit + paired mechanic** in the envelope:
   budget machinery (`BudgetVerdict`) carried; mechanic (`worker_analyzer`)
   carried, reading the transcript-less bundle (spec + audit only — fix the
   predecessor's known crash), ops-plane only (ADR 0005).
5. **Green CI** mirroring the predecessor's honest shape: structure check,
   gitleaks, ruff, mypy, `pytest -m "not container"`, container job (build →
   run → healthz → message → audit line → non-root → absence-grep → egress-deny
   check). Hermetic: static provider, no secrets, no network.
6. **Deploys** via transplanted `deploy.sh` + systemd template re-namespaced to
   `agent-keep`, target NSAF dev server (ADR 0004, `.verity/deploy-access.md`),
   `/etc/agent-keep/<slug>.env` pattern. Operator smoke: message in over
   dev-http on the tailnet → model reply out THROUGH the observed proxy →
   both directions visible in the audit log.

## Acceptance (the spine is proven when)

- CI green on a PR touching all of the above; container test passes hermetically.
- Live on the NSAF dev server under systemd: `/healthz` OK; a real chat
  round-trip via the Anthropic provider; the audit log shows the inbound
  message, the model call, the egress-observation record for the provider
  connection, and token accounting for the run.
- An outbound attempt to a non-allowlisted host from inside the agent container
  is denied and audited (live check, not just CI).
- The mechanic, pointed at the bundle, answers "what did the agent just do?"
  with citations to audit lines.

## Explicitly OUT of Stage 0 (early post-skeleton stages for intake)

- Real platform channel (WebEx or Slack — one stage each, adapters exist).
- Host-firewall defense-in-depth generated from the allowlist (ADR 0002).
- Mechanic ops actuators (restart/pause/throttle need the scoped host seam —
  ADR 0005); Stage 0 mechanic is read-and-explain only.
- Redis/Postgres tiers, event intake, schedule trigger, MCP tools, embeddings —
  carried code may ride along inert, but nothing composes them in.
- `keep_spec` narrowing (pruning factory-breadth fields) — its own stage before
  `keep/v1` schema export freezes.

## Accepted features from the catalog

None — `helper-bot` declined (requires a web UI surface; the identity forbids
interview/UI surfaces; operator confirmed 2026-07-22).

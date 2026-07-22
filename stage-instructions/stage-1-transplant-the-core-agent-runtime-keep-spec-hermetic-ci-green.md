# Stage 1: Transplant the core: agent_runtime + keep_spec, hermetic CI green

- **Type:** chore
- **Depends on:** none

## Objectives

Carry the proven core out of the read-only Foundry source
(`~/projects/Agent-Factorio` — copy files out, NEVER clone or add as a remote;
clean-tree rule) and stand it up under the Agent Keep identity with the full
hermetic quality gate green in CI. After this stage the repo compiles, type-checks
strict, and passes the transplanted unit/component tests with zero secrets or
network.

## What to build

- uv workspace (`pyproject.toml`, committed `uv.lock`, `.python-version` 3.12),
  hatchling per-package builds — mirror the Foundry root layout.
- `packages/agent_runtime/` — transplant verbatim (src + its tests): core loop,
  gateway, executor, lifecycle, runner, wiring, audit, sessions, messages,
  provider (incl. `BudgetVerdict`), queues, and these components ONLY:
  `dev_http`, `http_receiver`, `channel_lifecycle`, `static_provider`,
  `anthropic_provider`, `model_router`, `prompt_assembler`, `jsonl_audit`,
  `memory_queue`, `single_session`, `sqlite_persistence`, `local_tools`,
  `worker_analyzer`. Leave behind: webex/slack channels, event_intake,
  schedule_trigger, redis/postgres/pgvector/retrieval/facts/embedding,
  mcp_manager (they arrive by later intake, not by default).
  - If leaving a component out breaks imports/wiring minimally, prefer carrying
    the extra file over rewriting wiring — flag it in the PR description.
- `packages/keep_spec/` — transplant `foundry_spec` renamed (package, module,
  and import updates; spec version string `keep/v1`). Do NOT prune schema fields
  in this stage (narrowing is a later stage); drop only `interview`-coupled
  surfaces if any import them.
- CI: extend `.github/workflows/ci.yml` with the predecessor's job shape —
  structure check, gitleaks secret-scan (pinned action SHAs), ruff
  check+format, mypy strict, `pytest -m "not container"` — all via
  `uv sync --locked`. (The container job is Stage 2's.)
- ruff (line-length 100, py312, E/F/W/I/UP/B) and mypy strict config carried.

## Interface contracts

- **Exposes:** the runtime + spec packages every later stage builds on; the
  `static` provider as the CI substrate (ADR 0001).
- **Consumes:** frozen `contracts/agent-spec.md`, `internal-message.md`,
  `audit-record.md`, `run-lifecycle.md` — transplanted code must still conform;
  the Reviewer diffs transplanted files against the Foundry source for
  faithfulness.

## Testing requirements

- Transplanted unit/component tests pass under `pytest -m "not container"`.
- A `keep_spec` test asserts the spec version identifier is `keep/v1` and that
  a minimal chatbot spec (dev-http + static provider + jsonl audit) validates.
- mypy strict passes over both packages' `src` trees.

## Acceptance conditions

- [ ] Exit-state: repo installs with `uv sync --locked`; ruff, mypy strict, and
      the hermetic pytest suite all pass locally and in CI
- [ ] No file imports `foundry_interview`; no `foundry/v1` identifier remains
- [ ] gitleaks job green (no secrets in the transplant)
- [ ] Existing suite stays green; CI all-green

## Pipeline test: NO

# Agent Keep

A repeatable foundation for one agent at a time — the container that houses it, the mechanic that operates it, and every byte and token in or out, observed.

> Scaffolded by [Verity](https://github.com/seanerama/verity-framework) — prompt to production, proven.

## Status

See [`STATUS.md`](STATUS.md) for live runtime state (deployed version, environments).

## Project identity

- **slug:** `agent-keep`
- **images:** `ghcr.io/seanerama/agent-keep`

## Development

### Lint (local == CI)

`scripts/lint.sh` is the single source of truth for the lint gate — it runs the
exact set of checks CI's `lint` job runs, in one place:

```sh
./scripts/lint.sh
```

It runs `uv run ruff check .`, `uv run ruff format --check .`, and
`shellcheck deploy.sh deploy/agent-keep-deploy scripts/*.sh`. All three always
run (it does not stop at the first failure) and it exits non-zero if any check
fails.

### Pre-push hook (opt-in)

A committed pre-push hook runs `scripts/lint.sh` and blocks the push if lint
fails, so a red lint is caught before it reaches CI. It is **not** installed
automatically — enable it once per clone with a single command:

```sh
git config core.hooksPath .githooks
```

(Bypass a single push with `git push --no-verify`.)

### ShellCheck version parity

Local and CI must run the **same** shellcheck version, or a finding can pass
locally and fail in CI. The pinned version is **0.11.0** — CI downloads that
exact release, and `scripts/lint.sh` warns if your local shellcheck differs.
Install 0.11.0 locally (https://github.com/koalaman/shellcheck/releases/tag/v0.11.0)
for identical findings.

# Agent Keep — Status & Handoff

> Runtime/ops truth (framework-spec §4.6). Owned by the **Release/Deploy Operator**,
> updated on every deploy. Records secret **locations** only — never values.

**As of:** not yet deployed

## TL;DR

Scaffolded by Verity. Nothing deployed yet.

## Live deployment

- (none)

## Images

- prefix: `ghcr.io/seanerama/agent-keep`
- CI publishes `-default-chatbot`, `-mechanic`, `-egress-proxy` at `:edge` +
  `:<sha>` on push to main (static/generic; no formal release/`:latest` yet),
  plus the three provider worker variants at `:anthropic-edge`, `:ollama-edge`,
  and `:openai-edge` (each from `specs/default-chatbot.<provider>.yaml`).
- Live-tested providers: `ollama` (real llama3.2 through the proxy, egress ALLOW
  for `host.docker.internal:11434`). `anthropic` + `openai` are deployable and
  await an Operator live test with the respective API key — see the runbook.

## Secrets

- (none configured on any host yet) — when set, list NAMES + on-disk LOCATIONS
  only, never values.
- At go-live the ONE secret is `ANTHROPIC_API_KEY`, VALUE only in
  `/etc/agent-keep/default-chatbot.env` (root:0600) on the deploy host.

## Coordination notes

- **Deploy machinery is READY but not yet run live** (Stage 5). `deploy.sh` +
  `deploy/` (systemd unit `agent-keep@.service`, scoped helper, ingress relay,
  env templates) are in place and CI-green (shellcheck + unit-render +
  local full-topology container test). The live smoke over the tailnet is the
  Operator's gate — exact commands in **`docs/deploy/first-live-chassis.md`**.
- Go-live is two steps: (A) the published static image proves the pipe + the
  egress DENY path; (B) the Operator builds/pushes the `anthropic` worker variant
  (`specs/default-chatbot.anthropic.yaml`) for the real Anthropic reply + egress ALLOW.

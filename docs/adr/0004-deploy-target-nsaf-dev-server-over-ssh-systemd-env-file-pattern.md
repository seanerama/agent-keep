# 0004. Deploy target: NSAF dev server over SSH (systemd + env-file pattern)

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

The operator's deployment catalog (`~/.verity/deployment-methods.md`) lists four
active methods. The transplanted deploy machinery (predecessor ADR 0013) is
host-agnostic: any SSH-reachable Docker+systemd host, target from `DEPLOY_HOST`,
per-agent env file at `/etc/<project>/<slug>.env` (root:0600), scoped sudo
helper. The first live chassis needs a home.

## Decision

The first live chassis deploys to the **NSAF dev server** (operator-selected,
2026-07-22): the Ubuntu box on the operator's tailnet, reached over SSH. It is
exactly the Docker+systemd shape `deploy.sh` expects, the catalog itself says
new services there run under systemd, and it is not publicly reachable — which
suits an agent chassis whose first channel is unverified dev-http (ADR 0003).

Access details (credential locations only) live in the gitignored
`.verity/deploy-access.md`; the committed pointer explains where to get them.
The env-file namespace under the new identity is `/etc/agent-keep/<slug>.env`.

## Alternatives considered

- **EC2 primary:** same SSH+systemd pattern works, but it is publicly exposed,
  arm64-only (forces multi-arch or arm64 images from day one), and RAM-lean
  (~3.7G shared with existing sites). Viable later target; wrong first home.
- **Coolify:** the catalog marks it "a capability target for built apps, not a
  home for core daemons"; its repo→build→domain flow would replace the
  transplanted deploy machinery rather than exercise it.
- **Cloudflare Pages:** static/edge platform; cannot run a long-lived container.

## Consequences

- The deploy stage exercises the transplanted `deploy.sh` + systemd template
  nearly unchanged (re-namespaced to `agent-keep`), keeping transplant risk low.
- The host runs other services (NSAF, per-app Postgres, tunnels); the chassis's
  hardened container profile (read-only fs, cap-drop, memory cap) matters for
  cohabitation.
- Publicly reaching the chassis later means either a cloudflared tunnel route on
  that host or a move to another catalog target — either is a new ADR.

"""Proxy entrypoint — `python -m keep_egress.runner`.

Boot is FAIL-CLOSED: the mounted spec (the SAME spec.yaml the agent image was
baked from — see the package docstring) must load and validate through
`keep_spec.load_spec` or the proxy refuses to start. There is no fallback
allowlist, no permissive mode, and deliberately no kill-switch: a spec with an
empty `sandbox.egress` yields deny-everything, which is the safe default.

Env configuration (defaults in parentheses):
  KEEP_SPEC_PATH          (/etc/agent-keep/spec.yaml)
  KEEP_EGRESS_AUDIT_PATH  (/var/lib/agent-keep/egress-audit.jsonl)
  KEEP_EGRESS_HOST        (0.0.0.0)
  KEEP_EGRESS_PORT        (3128)
"""

import asyncio
import os
import sys

from pydantic import ValidationError

from keep_egress.proxy import EgressProxy
from keep_egress.records import EgressJsonlSink, ObservedAgent
from keep_spec import load_spec

DEFAULT_SPEC_PATH = "/etc/agent-keep/spec.yaml"
DEFAULT_AUDIT_PATH = "/var/lib/agent-keep/egress-audit.jsonl"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 3128


async def _serve(proxy: EgressProxy) -> None:
    await proxy.start()
    await proxy.serve_forever()


def main() -> int:
    spec_path = os.environ.get("KEEP_SPEC_PATH", DEFAULT_SPEC_PATH)
    audit_path = os.environ.get("KEEP_EGRESS_AUDIT_PATH", DEFAULT_AUDIT_PATH)
    host = os.environ.get("KEEP_EGRESS_HOST", DEFAULT_HOST)
    port = int(os.environ.get("KEEP_EGRESS_PORT", str(DEFAULT_PORT)))

    try:
        spec = load_spec(spec_path)
    except FileNotFoundError:
        print(f"error: spec file not found: {spec_path} (KEEP_SPEC_PATH)", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"error: spec failed keep/v1 validation:\n{exc}", file=sys.stderr)
        return 1

    allowlist = spec.spec.sandbox.egress
    proxy = EgressProxy(
        allowlist=allowlist,
        agent=ObservedAgent(slug=spec.metadata.slug, spec_version=spec.metadata.specVersion),
        sink=EgressJsonlSink(audit_path),
        host=host,
        port=port,
    )
    print(
        f"egress proxy: observing agent '{spec.metadata.slug}' "
        f"(specVersion {spec.metadata.specVersion}) on {host}:{port}; "
        f"allowlist entries: {len(allowlist)}"
        + (" (EMPTY — denying everything, fail-closed)" if not allowlist else ""),
        flush=True,
    )
    try:
        asyncio.run(_serve(proxy))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

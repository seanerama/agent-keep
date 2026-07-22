"""Runtime egress allowlist matcher — ONE importable matcher for
`sandbox.egress` entries.

The grammar's source of truth is `keep_spec.models.EGRESS_HOST`
(`host[:port]`, optional `*.` wildcard-subdomain prefix); this module is the
matching SEMANTICS for that grammar, importable by every runtime enforcer —
the egress observation proxy today (`keep_egress`, contract
egress-observation v1), a deploy-side host firewall generator later
(ADR 0002's deferred defense-in-depth stage). The proxy never grows its own
list or its own grammar: the allowlist is always `sandbox.egress` from the
spec, matched here.

Semantics (identical to the build-time cross-validation matcher in
`agent_runtime.wiring._egress_entry_allows`, pinned by a parity test in
`packages/keep_egress/tests/test_matcher.py`):

- an entry without a port covers EVERY port; `host:port` covers that port only;
- `*.example.com` covers subdomains only, never the apex `example.com`;
- host comparison is case-insensitive;
- no entry matched = denied — fail-closed, so an empty allowlist denies
  everything (the safe default; deliberately no kill-switch exists).
"""

import re
from collections.abc import Sequence

_PORT_SUFFIX = re.compile(r":[0-9]{1,5}$")


def egress_entry_allows(entry: str, host: str, port: int) -> bool:
    """Does one `sandbox.egress` `host[:port]` entry (optionally
    `*.`-wildcarded) cover this host+port? Entries are format-validated by the
    schema (`EGRESS_HOST`); hosts compare case-insensitively."""
    entry_host, entry_port = entry, None
    if _PORT_SUFFIX.search(entry):
        entry_host, _, port_text = entry.rpartition(":")
        entry_port = int(port_text)
    if entry_port is not None and entry_port != port:
        return False
    entry_host = entry_host.lower()
    host = host.lower()
    if entry_host.startswith("*."):
        return host.endswith(entry_host[1:])  # subdomains only, per wildcard convention
    return host == entry_host


def match_allowlist(allowlist: Sequence[str], host: str, port: int) -> str | None:
    """First allowlist entry covering `host:port`, or None — None IS the deny
    verdict (fail-closed; an empty allowlist therefore denies everything)."""
    for entry in allowlist:
        if egress_entry_allows(entry, host, port):
            return entry
    return None

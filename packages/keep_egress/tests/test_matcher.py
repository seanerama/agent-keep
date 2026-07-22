"""Allowlist matcher tests (stage 3, unit) — `keep_spec.egress`.

Exact, port-specific, wildcard-subdomain, deny-by-default, case handling —
plus a PARITY guard against the transplanted build-time cross-validation
matcher (`agent_runtime.wiring._egress_entry_allows`): the runtime enforcer
and the build-time validator must agree on every case or the allowlist means
two different things at build vs run time.
"""

import re

import pytest

from agent_runtime.wiring import _egress_entry_allows as wiring_allows
from keep_spec import egress_entry_allows, match_allowlist
from keep_spec.models import EGRESS_HOST

# (entry, host, port, expected)
CASES: list[tuple[str, str, int, bool]] = [
    # exact host, no port -> every port
    ("api.anthropic.com", "api.anthropic.com", 443, True),
    ("api.anthropic.com", "api.anthropic.com", 80, True),
    ("api.anthropic.com", "api.anthropic.com", 8443, True),
    ("api.anthropic.com", "anthropic.com", 443, False),
    ("api.anthropic.com", "evil-api.anthropic.com", 443, False),
    # port-specific entry -> that port only
    ("api.anthropic.com:443", "api.anthropic.com", 443, True),
    ("api.anthropic.com:443", "api.anthropic.com", 80, False),
    ("api.anthropic.com:443", "api.anthropic.com", 8443, False),
    # wildcard subdomains — subdomains only, never the apex
    ("*.example.com", "api.example.com", 443, True),
    ("*.example.com", "a.b.example.com", 443, True),
    ("*.example.com", "example.com", 443, False),
    ("*.example.com", "evilexample.com", 443, False),
    ("*.example.com", "example.com.evil.net", 443, False),
    # wildcard + port
    ("*.example.com:8443", "api.example.com", 8443, True),
    ("*.example.com:8443", "api.example.com", 443, False),
    # case handling: hosts compare case-insensitively
    ("Api.Example.COM", "api.example.com", 443, True),
    ("api.example.com", "API.EXAMPLE.COM", 443, True),
    ("*.Example.com", "api.EXAMPLE.com", 443, True),
    # unrelated host
    ("api.anthropic.com:443", "attacker.invalid", 443, False),
]


@pytest.mark.parametrize(("entry", "host", "port", "expected"), CASES)
def test_egress_entry_allows(entry: str, host: str, port: int, expected: bool) -> None:
    assert egress_entry_allows(entry, host, port) is expected


@pytest.mark.parametrize(("entry", "host", "port", "expected"), CASES)
def test_parity_with_build_time_cross_validation(
    entry: str, host: str, port: int, expected: bool
) -> None:
    """The runtime matcher and the transplanted build-time matcher agree on
    every case (wiring's matcher expects a pre-lowercased host, which is what
    its callers pass — normalize the same way here)."""
    assert wiring_allows(entry, host.lower(), port) is expected
    assert wiring_allows(entry, host.lower(), port) is egress_entry_allows(entry, host, port)


def test_every_case_entry_is_schema_valid() -> None:
    """The cases above stay inside the sandbox.egress grammar (EGRESS_HOST is
    the single source of truth for the value space the matcher covers)."""
    for entry, _host, _port, _expected in CASES:
        assert re.match(EGRESS_HOST, entry), f"case entry {entry!r} is outside EGRESS_HOST"


def test_deny_by_default_empty_allowlist() -> None:
    """An EMPTY allowlist matches nothing — deny-everything is the safe
    default (deliberately no kill-switch exists)."""
    assert match_allowlist([], "api.anthropic.com", 443) is None
    assert match_allowlist([], "localhost", 80) is None


def test_match_allowlist_returns_the_matched_entry() -> None:
    allowlist = ["other.example.net", "*.example.com:443", "api.example.com"]
    # first matching entry wins; the ENTRY string is returned (it lands in the
    # audit record's matched_entry field verbatim)
    assert match_allowlist(allowlist, "api.example.com", 443) == "*.example.com:443"
    assert match_allowlist(allowlist, "api.example.com", 80) == "api.example.com"
    assert match_allowlist(allowlist, "nomatch.invalid", 443) is None

"""Shared fixtures for the container integration harness (marker `container`).

Carried from the Foundry's tests/integration/conftest.py (ADR 0001: proven
shape, carried not rewritten), re-namespaced to agent-keep. The sqlite
persistence tier is REAL: the composed default chatbot requires SQLITE_PATH at
run time and refuses to boot without it — exactly what a deploy must provide.
One fixture centralizes the `docker run` env injection; import-check runs
(`docker run ... python -c "import ..."`) never boot the runner and need
nothing.
"""

import pytest

#: Where the sqlite-tier session store lives INSIDE integration containers:
#: /tmp is writable by the image's non-root `agent` user (uid 10001) without
#: any volume.
SQLITE_PATH_IN_CONTAINER = "/tmp/agent-keep-sessions.sqlite3"


@pytest.fixture(scope="session")
def sqlite_env() -> tuple[str, str]:
    """`docker run` args injecting the required SQLITE_PATH."""
    return ("-e", f"SQLITE_PATH={SQLITE_PATH_IN_CONTAINER}")


#: Container runtime hardening (the Foundry's OPS-01 posture, carried) — the
#: SAME flags the Stage-5 deploy will apply. Splatting these into every run
#: that BOOTS the runner makes the `container` job assert the agent still
#: serves /healthz, handles a message, and writes its audit line UNDER the
#: hardened config:
#:
#:   --security-opt no-new-privileges  no privilege escalation.
#:   --cap-drop ALL                    a Python HTTP server needs no Linux caps.
#:   --read-only                       immutable root fs; writable paths are the
#:                                     explicit mounts below and nothing else.
#:   --pids-limit / --memory           blast-radius ceilings (tunable).
#:   -e PYTHONDONTWRITEBYTECODE=1      /app is read-only — don't attempt to
#:                                     write __pycache__/*.pyc on import.
#:
#: Writable mounts (every path the runtime writes, verified against
#: jsonl_audit.py + sqlite_persistence.py):
#:   --tmpfs /tmp:mode=1777            the sqlite db + WAL/-shm sidecars live
#:                                     here (SQLITE_PATH_IN_CONTAINER is under
#:                                     /tmp), plus any sqlite temp spill.
#:   --tmpfs /var/lib/agent-keep:mode=1777
#:                                     the audit sink appends audit.jsonl here.
#: mode=1777 is LOAD-BEARING, not decorative: a bare --tmpfs mounts root:root
#: drwxr-xr-x, and the uid-10001 runner then fails to append audit.jsonl
#: (PermissionError surfacing as 500 on /message). Do not remove. In production
#: the audit dir is instead a persistent named volume.
HARDENED_RUN_FLAGS: tuple[str, ...] = (
    "--security-opt",
    "no-new-privileges",
    "--cap-drop",
    "ALL",
    "--read-only",
    "--pids-limit",
    "512",
    "--memory",
    "512m",
    "-e",
    "PYTHONDONTWRITEBYTECODE=1",
    "--tmpfs",
    "/tmp:mode=1777",
    "--tmpfs",
    "/var/lib/agent-keep:mode=1777",
)


@pytest.fixture(scope="session")
def hardened_run_flags() -> tuple[str, ...]:
    """`docker run` args applying the runtime hardening (see above)."""
    return HARDENED_RUN_FLAGS

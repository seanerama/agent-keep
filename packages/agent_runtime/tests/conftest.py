"""Shared fixtures for the agent_runtime unit suite.

Stage 20: every buildable persistence tier is durable now, so `build_app` on
any sqlite-tier spec (the skeleton and most test variants) reads SQLITE_PATH
unconditionally and refuses to boot without it. ONE autouse fixture points the
variable at a per-test tmp file so the suite's ~16 pre-existing build_app call
sites (test_anthropic_boot, test_executor, test_event_intake, test_runner_boot,
test_wiring_guard) keep working without hand edits — exactly the deploy-time
injection the runner expects, never a fallback inside the runner itself. The
missing-variable refusal test deletes the variable explicitly on top of this.
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def sqlite_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "sessions.sqlite3"))

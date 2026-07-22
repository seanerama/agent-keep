"""Fail-closed boot tests for the proxy entrypoint (`keep_egress.runner`):
no spec, or an invalid spec, means NO proxy — never a permissive fallback."""

from pathlib import Path

import pytest

from keep_egress import runner


def test_missing_spec_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KEEP_SPEC_PATH", str(tmp_path / "nope.yaml"))
    assert runner.main() == 2
    assert "spec file not found" in capsys.readouterr().err


def test_invalid_spec_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: keep/v1\nkind: AgentSpec\n", encoding="utf-8")
    monkeypatch.setenv("KEEP_SPEC_PATH", str(bad))
    assert runner.main() == 1
    assert "failed keep/v1 validation" in capsys.readouterr().err


# ---- issue #13 Defect 1: present-but-empty env vars must fall back to defaults ----
# `deploy.sh` writes `KEEP_EGRESS_PORT=`/`KEEP_EGRESS_HOST=` (empty) and the unit
# passes them via bare `-e KEEP_EGRESS_PORT`, so docker injects them present-but-
# empty. The old `int(os.environ.get("KEEP_EGRESS_PORT", str(DEFAULT_PORT)))`
# returned `''` for a present-empty key and `int('')` raised ValueError, crashing
# the proxy at boot. These pin the fix: empty resolves to the default, not a crash.


def test_empty_port_resolves_to_default_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crasher: KEEP_EGRESS_PORT='' must yield DEFAULT_PORT, never ValueError."""
    monkeypatch.setenv("KEEP_EGRESS_PORT", "")
    assert runner._resolve_config().port == runner.DEFAULT_PORT == 3128


def test_empty_host_resolves_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The latent twin: KEEP_EGRESS_HOST='' must bind DEFAULT_HOST, not ''."""
    monkeypatch.setenv("KEEP_EGRESS_HOST", "")
    assert runner._resolve_config().host == runner.DEFAULT_HOST


def test_empty_spec_and_audit_paths_resolve_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same env-passthrough hazard on the path vars: empty -> default, not ''."""
    monkeypatch.setenv("KEEP_SPEC_PATH", "")
    monkeypatch.setenv("KEEP_EGRESS_AUDIT_PATH", "")
    config = runner._resolve_config()
    assert config.spec_path == runner.DEFAULT_SPEC_PATH
    assert config.audit_path == runner.DEFAULT_AUDIT_PATH


def test_unset_env_resolves_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The absent (unset) case still resolves to every default."""
    for key in (
        "KEEP_EGRESS_PORT",
        "KEEP_EGRESS_HOST",
        "KEEP_SPEC_PATH",
        "KEEP_EGRESS_AUDIT_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    config = runner._resolve_config()
    assert config == runner._Config(
        spec_path=runner.DEFAULT_SPEC_PATH,
        audit_path=runner.DEFAULT_AUDIT_PATH,
        host=runner.DEFAULT_HOST,
        port=runner.DEFAULT_PORT,
    )


def test_explicit_values_are_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit (non-empty) value wins over the default for every var."""
    monkeypatch.setenv("KEEP_EGRESS_PORT", "9999")
    monkeypatch.setenv("KEEP_EGRESS_HOST", "127.0.0.1")
    monkeypatch.setenv("KEEP_SPEC_PATH", "/custom/spec.yaml")
    monkeypatch.setenv("KEEP_EGRESS_AUDIT_PATH", "/custom/audit.jsonl")
    config = runner._resolve_config()
    assert config == runner._Config(
        spec_path="/custom/spec.yaml",
        audit_path="/custom/audit.jsonl",
        host="127.0.0.1",
        port=9999,
    )

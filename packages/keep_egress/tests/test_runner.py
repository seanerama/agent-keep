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

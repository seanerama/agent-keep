"""keep-build CLI tests — no docker: validate + context-only build paths."""

from pathlib import Path

import pytest

from keep_build.cli import main

REPO_ROOT = Path(__file__).parents[3]
SPEC_PATH = REPO_ROOT / "specs" / "default-chatbot.yaml"


def test_validate_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(SPEC_PATH)]) == 0
    out = capsys.readouterr().out
    assert "valid keep/v1 AgentSpec: default-chatbot" in out


def test_validate_missing_file_exits_2() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["validate", "no-such-spec.yaml"])
    assert excinfo.value.code == 2


def test_validate_invalid_spec_exits_1(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: keep/v1\nkind: AgentSpec\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(["validate", str(bad)])
    assert excinfo.value.code == 1


def test_build_context_only_emits_the_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["build", str(SPEC_PATH), "--context-dir", str(tmp_path), "--context-only"])
    assert rc == 0
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / "spec.yaml").is_file()
    assert (tmp_path / "agent_runtime" / "runner.py").is_file()
    assert f"build context: {tmp_path}" in capsys.readouterr().out

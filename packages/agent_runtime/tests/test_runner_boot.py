"""Boot-time digest guard (issue #8): no 'sha256:unknown' masquerade.

The runner REFUSES to start when AGENT_IMAGE_DIGEST is unset or malformed —
every audit record carries agent.image_digest as provenance, and a fabricated
sentinel would poison the append-only log for the agent's entire lifetime.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_runtime.runner import ImageDigestError, build_app, require_image_digest
from keep_spec import AgentSpec, validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
SKELETON_SPEC = REPO_ROOT / "examples" / "skeleton.yaml"
VALID_DIGEST = "sha256:" + "ab" * 32


def _skeleton_spec(tmp_path: Path) -> AgentSpec:
    """The real skeleton spec, with the audit path redirected into tmp."""
    with open(SKELETON_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    data["spec"]["observability"]["audit"]["path"] = str(tmp_path / "audit.jsonl")
    return validate_spec_data(data)


def test_unset_digest_refuses_to_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_IMAGE_DIGEST", raising=False)
    with pytest.raises(ImageDigestError, match="AGENT_IMAGE_DIGEST is unset"):
        build_app(_skeleton_spec(tmp_path))


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "sha256:unknown",  # the exact masquerade issue #8 forbids
        "latest",
        "sha256:" + "g" * 64,  # not hex
        "sha256:" + "ab" * 31,  # too short
        "SHA256:" + "ab" * 32,  # wrong case
    ],
)
def test_malformed_digest_refuses_to_start(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", bad)
    with pytest.raises(ImageDigestError, match="refusing to start"):
        require_image_digest()


def test_valid_injected_digest_reaches_the_agent_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_IMAGE_DIGEST", VALID_DIGEST)
    assert require_image_digest() == VALID_DIGEST
    core, _adapter = build_app(_skeleton_spec(tmp_path))
    assert core._identity.image_digest == VALID_DIGEST

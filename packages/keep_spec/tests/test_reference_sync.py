"""docs/spec-reference.md must stay in sync with the Pydantic models."""

from pathlib import Path

from keep_spec.reference_export import reference_markdown

REPO_ROOT = Path(__file__).parents[3]
COMMITTED_REFERENCE = REPO_ROOT / "docs" / "spec-reference.md"


def test_committed_reference_matches_models() -> None:
    committed_text = COMMITTED_REFERENCE.read_text(encoding="utf-8")
    assert committed_text == reference_markdown(), (
        "docs/spec-reference.md is stale — regenerate with "
        "`uv run python -m keep_spec.reference_export docs/spec-reference.md`"
    )


def test_reference_documents_every_section() -> None:
    text = reference_markdown()
    assert text.startswith("# keep/v1 AgentSpec — field reference")
    for heading in [
        "## AgentSpec",
        "## Metadata",
        "## SpecSections",
        "## Persona",
        "## Triggers",
        "## ScheduleTrigger",
        "## DevHttpChannel",
        "## DiscordChannel",
        "## SlackChannel",
        "## WebexChannel",
        "## SmsChannel",
        "## Gateway",
        "## Sessions",
        "## Memory",
        "## SkillPack",
        "## McpServer",
        "## Approval",
        "## Sandbox",
        "## Models",
        "## Observability",
        "## Persistence",
    ]:
        assert heading + "\n" in text, f"missing {heading!r} in generated reference"

"""Round-trip: YAML -> models -> YAML is lossless (semantic equality)."""

from pathlib import Path

import pytest
import yaml

from keep_spec import dump_spec_data, dump_spec_yaml, load_spec, validate_spec_data

REPO_ROOT = Path(__file__).parents[3]
FIXTURES = Path(__file__).parent / "fixtures"

SPEC_FILES = [
    REPO_ROOT / "examples" / "skeleton.yaml",
    FIXTURES / "full-featured.yaml",
    FIXTURES / "scheduled-reporter.yaml",
]


@pytest.mark.parametrize("spec_file", SPEC_FILES, ids=lambda p: p.name)
def test_yaml_models_yaml_is_lossless(spec_file: Path) -> None:
    original = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    spec = load_spec(spec_file)
    assert dump_spec_data(spec) == original


@pytest.mark.parametrize("spec_file", SPEC_FILES, ids=lambda p: p.name)
def test_dumped_yaml_revalidates_identically(spec_file: Path) -> None:
    spec = load_spec(spec_file)
    reparsed = yaml.safe_load(dump_spec_yaml(spec))
    assert validate_spec_data(reparsed) == spec

"""Unit tests targeting configuration parsing and structural Dagster transformations."""

from pathlib import Path

import pytest
from galadril_pipeline.parser import PipelineParser


def test_from_yaml_file_errors(tmp_path: Path) -> None:
    """Verifies missing files, empty targets, and validation exceptions."""
    non_existent = tmp_path / "missing.yaml"
    with pytest.raises(FileNotFoundError):
        PipelineParser.from_yaml(non_existent)

    empty_file = tmp_path / "empty.yaml"
    empty_file.write_text("")
    with pytest.raises(ValueError, match="Empty context matching"):
        PipelineParser.from_yaml(empty_file)

    invalid_file = tmp_path / "invalid.yaml"
    invalid_file.write_text("version: 'not-an-int'")
    with pytest.raises(
        ValueError, match="Validation failure across model properties"
    ):
        PipelineParser.from_yaml(invalid_file)


def test_to_dagster_definitions_compilation(tmp_path: Path) -> None:
    """Validates complete object conversion from raw text into operational blocks."""
    valid_yaml = """
    version: 1
    name: pipeline_test
    sources:
      - id: ingress
        topic: raw-topic
        match_pattern: ".*"
        schema_path: "/schema.avsc"
    pipeline:
      - step: compute_layer
        type: sink
        input_from: [ingress]
    """
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(valid_yaml)

    config = PipelineParser.from_yaml(cfg_file)
    defs = PipelineParser.to_dagster_definitions(config)

    assert defs.get_assets_def("ingress") is not None
    assert defs.get_assets_def("compute_layer") is not None

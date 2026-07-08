"""Unit tests for the configuration models and graph validation logic."""

import pytest
from galadril_pipeline.config import (
    PipelineConfig,
    PipelineStep,
    Source,
    StepParams,
    StepType,
    TriggerType,
)
from pydantic import ValidationError


def test_step_params_validation() -> None:
    """Verifies trigger constraints and cron syntax validation."""
    params = StepParams(trigger=TriggerType.MANUAL)
    assert params.cron is None

    params_cron = StepParams(trigger=TriggerType.CRON, cron="0 12 * * *")
    assert params_cron.cron == "0 12 * * *"

    with pytest.raises(ValidationError, match="A cron expression is required"):
        StepParams(trigger=TriggerType.CRON, cron=None)

    with pytest.raises(ValidationError, match="Invalid cron expression format"):
        StepParams(trigger=TriggerType.CRON, cron="invalid-cron")

    with pytest.raises(ValidationError, match="'cron' may only be specified"):
        StepParams(trigger=TriggerType.MANUAL, cron="0 12 * * *")


def test_pipeline_step_inference_validation() -> None:
    """Validates that inference steps mandate a defined AI model path."""
    step = PipelineStep(
        step="inference_step",
        type=StepType.INFERENCE,
        model="some.model.Path",
        input_from=["src"],
    )
    assert step.step == "inference_step"

    with pytest.raises(
        ValidationError, match="Inference steps require a non-null"
    ):
        PipelineStep(
            step="failed_inference", type=StepType.INFERENCE, input_from=["src"]
        )


def test_pipeline_config_graph_integrity() -> None:
    """Validates graph structure including duplicates, unknowns, and cycles."""
    source = Source(id="src", topic="t", match_pattern=".*", schema_path="/p")
    step1 = PipelineStep(step="step1", type=StepType.SINK, input_from=["src"])

    config = PipelineConfig(
        name="valid_pipeline", sources=[source], pipeline=[step1]
    )
    assert config.get_topological_order() == ["src", "step1"]

    with pytest.raises(ValidationError, match="Duplicate source identifiers"):
        PipelineConfig(
            name="dup_src", sources=[source, source], pipeline=[step1]
        )

    with pytest.raises(ValidationError, match="Duplicate step identifiers"):
        PipelineConfig(
            name="dup_step", sources=[source], pipeline=[step1, step1]
        )

    broken_step = PipelineStep(
        step="step2", type=StepType.SINK, input_from=["unknown_node"]
    )
    with pytest.raises(
        ValidationError, match="references unknown dependencies"
    ):
        PipelineConfig(
            name="unknown_dep", sources=[source], pipeline=[broken_step]
        )

    # Cyclic dependency check.
    step_a = PipelineStep(step="A", type=StepType.SINK, input_from=["B"])
    step_b = PipelineStep(step="B", type=StepType.SINK, input_from=["A"])
    with pytest.raises(ValidationError, match="Cyclic dependency detected"):
        PipelineConfig(name="cyclic", sources=[], pipeline=[step_a, step_b])

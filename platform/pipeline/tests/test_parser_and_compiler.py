"""Unit tests for pipeline configuration parsing and Dagster asset compilation."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock
import dagster as dg
import pytest

from galadril_pipeline.config import PipelineConfig
from galadril_pipeline.parser import PipelineParser
from galadril_pipeline.compiler.assets import AssetCompilerFactory
from galadril_pipeline.runtime.schemas import NodeStatus, StepRuntimeOutput
from galadril_pipeline.runtime.batch import BatchHandle


class MockLocalStepExecutor:
    """Lightweight step executor stub for Dagster local materialization testing."""

    def __init__(self) -> None:
        self.executed_steps: list[str] = []

    async def execute_step(
        self, runtime_input: Any
    ) -> StepRuntimeOutput[list[dict[str, Any]]]:
        self.executed_steps.append(runtime_input.step_name)
        return StepRuntimeOutput(
            status=NodeStatus.COMPLETED,
            batch=BatchHandle(
                correlation_id=runtime_input.correlation_id,
                payload=[{"mutated": True}],
                kafka_offsets={},
            ),
            records_processed=1,
            latency_seconds=0.01,
        )


@pytest.fixture
def valid_pipeline_payload() -> dict[str, Any]:
    """Returns a valid operational dictionary structure mapping the pipeline configuration."""
    return {
        "version": 1,
        "name": "production_vision_pipeline",
        "sources": [
            {
                "id": "ingress_images",
                "topic": "raw-images",
                "match_pattern": r"^.*\.jpg$",
                "schema_path": "/schemas/image.avsc",
            }
        ],
        "pipeline": [
            {
                "step": "image_inference",
                "type": "inference",
                "input_from": ["ingress_images"],
                "model": "galadril.vision.models.ResNet50",
                "artifact_path": "/models/resnet50.bin",
                "params": {
                    "trigger": "manual",
                    "retry_policy": {"max_retries": 1, "delay_seconds": 0.1},
                },
            },
            {
                "step": "data_sink",
                "type": "sink",
                "input_from": ["image_inference"],
                "params": {"trigger": "cron", "cron": "0 12 * * *"},
            },
        ],
    }


def test_pipeline_config_parsing_and_toposort(
    valid_pipeline_payload: dict[str, Any],
) -> None:
    """Validates that a correctly formed dictionary parses and extracts exact topological sorting."""
    config = PipelineConfig.model_validate(valid_pipeline_payload)
    assert config.name == "production_vision_pipeline"

    order = config.get_topological_order()
    assert order == ["ingress_images", "image_inference", "data_sink"]


def test_pipeline_config_cyclic_dependency_rejection(
    valid_pipeline_payload: dict[str, Any],
) -> None:
    """Ensures that validation fails with ValueError upon cyclic graph identification."""
    payload = valid_pipeline_payload.copy()
    payload["pipeline"][0]["input_from"].append("data_sink")

    with pytest.raises(ValueError, match="Cyclic dependency detected"):
        PipelineConfig.model_validate(payload)


def test_dagster_definitions_compilation(
    valid_pipeline_payload: dict[str, Any],
) -> None:
    """Verifies that the parser successfully transforms schemas into structural Dagster Definitions."""
    config = PipelineConfig.model_validate(valid_pipeline_payload)
    defs = PipelineParser.to_dagster_definitions(config)

    assert isinstance(defs, dg.Definitions)
    assert defs.get_assets_def("ingress_images") is not None
    assert defs.get_assets_def("image_inference") is not None
    assert defs.get_assets_def("data_sink") is not None


@pytest.mark.asyncio
async def test_dagster_compiled_assets_materialization() -> None:
    """Validates end-to-end execution of compiled assets inside the Dagster runtime context."""
    source_cfg = MagicMock()
    source_cfg.id = "mock_source"
    source_cfg.topic = "test-topic"

    step_cfg = MagicMock()
    step_cfg.step = "mock_compute"
    step_cfg.input_from = ["mock_source"]
    step_cfg.type.value = "transform"
    step_cfg.params.model_dump.return_value = {"param": "value"}

    source_asset = AssetCompilerFactory.build_source_asset(source_cfg)
    compute_asset = AssetCompilerFactory.build_pipeline_asset(
        step_cfg, topological_index=1
    )

    # Replaced obsolete asyncio.coroutine with modern AsyncMock to satisfy Pylance
    mock_kafka = MagicMock()
    mock_kafka.poll_batch = AsyncMock(return_value=[{"data": 1}])
    mock_executor = MockLocalStepExecutor()

    res = dg.materialize(
        assets=[source_asset, compute_asset],
        resources={
            "kafka": mock_kafka,
            "pipeline_executor": mock_executor,
        },
    )
    assert res.success
    assert mock_executor.executed_steps == ["mock_compute"]

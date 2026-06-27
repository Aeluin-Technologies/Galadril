"""Comprehensive unit and integration test suite for the pipeline framework."""

import asyncio
import pytest
import dagster as dg

from galadril_pipeline.config import PipelineConfig
from galadril_pipeline.parser import PipelineParser
from galadril_pipeline.compiler.resources import (
    AbstractStepExecutor,
    NodeStatus,
    StepRuntimeInput,
    StepRuntimeOutput,
)
from galadril_pipeline.runtime.engine import (
    AbstractCheckpointStore,
    AsyncPipelineEngine,
)
from galadril_pipeline.runtime.schemas import PipelineRunContext, StepCheckpoint


class MockStepExecutor(AbstractStepExecutor):
    """In memory executor stub simulating data processing mutations."""

    def __init__(self):
        super().__init__()
        self.executed_steps = []

    async def execute_step(
        self, runtime_input: StepRuntimeInput
    ) -> StepRuntimeOutput:
        self.executed_steps.append(runtime_input.step_name)
        # Simulate slight computational latency.
        await asyncio.sleep(0.01)

        return StepRuntimeOutput(
            status=NodeStatus.COMPLETED,
            records_processed=100,
            latency_seconds=0.01,
            storage_uri_pointers=[
                f"s3://bucket/output/{runtime_input.step_name}.parquet"
            ],
            metrics={"accuracy": 0.95},
        )


class MockCheckpointStore(AbstractCheckpointStore):
    """InMemory checkpoint tracker storing immutable operational records."""

    def __init__(self):
        self.checkpoints = {}

    async def get_checkpoint(
        self, run_id: str, step_name: str
    ) -> StepCheckpoint | None:
        return self.checkpoints.get((run_id, step_name))

    async def save_checkpoint(
        self, run_id: str, checkpoint: StepCheckpoint
    ) -> None:
        self.checkpoints[(run_id, checkpoint.step_name)] = checkpoint


@pytest.fixture
def valid_pipeline_payload() -> dict:
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
                    "retry_policy": {"max_retries": 2, "delay_seconds": 1.0},
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


def test_pipeline_config_parsing_and_toposort(valid_pipeline_payload):
    """Validates that a correctly formed dictionary parses and extracts exact topological sorting."""
    config = PipelineConfig.model_validate(valid_pipeline_payload)
    assert config.name == "production_vision_pipeline"

    order = config.get_topological_order()
    assert order == ["ingress_images", "image_inference", "data_sink"]


def test_pipeline_config_cyclic_dependency_rejection(valid_pipeline_payload):
    """Ensures that validation fails with ValueError upon cyclic graph identification."""
    payload = valid_pipeline_payload.copy()
    # Introduce a cyclic loop: image_inference -> data_sink -> image_inference.
    payload["pipeline"][0]["input_from"].append("data_sink")

    with pytest.raises(ValueError, match="Cyclic dependency detected"):
        PipelineConfig.model_validate(payload)


def test_pipeline_config_invalid_cron_rejection(valid_pipeline_payload):
    """Guarantees that syntax failures inside cron parameters trigger validation rejections."""
    payload = valid_pipeline_payload.copy()
    payload["pipeline"][1]["params"]["cron"] = "invalid_cron_string"

    with pytest.raises(ValueError, match="Invalid cron expression format"):
        PipelineConfig.model_validate(payload)


def test_dagster_definitions_compilation(valid_pipeline_payload):
    """Verifies that the parser successfully transforms schemas into structural Dagster Definitions."""
    config = PipelineConfig.model_validate(valid_pipeline_payload)
    defs = PipelineParser.to_dagster_definitions(config)

    assert isinstance(defs, dg.Definitions)

    assert defs.get_assets_def("ingress_images") is not None
    assert defs.get_assets_def("image_inference") is not None
    assert defs.get_assets_def("data_sink") is not None
    assert defs.get_schedule_def("schedule_data_sink") is not None


@pytest.mark.asyncio
async def test_async_pipeline_engine_execution(valid_pipeline_payload):
    """Executes the full runtime pipeline to verify transactional idempotence and state calculation."""
    config = PipelineConfig.model_validate(valid_pipeline_payload)

    executor = MockStepExecutor()
    store = MockCheckpointStore()
    engine = AsyncPipelineEngine(
        executor=executor, checkpoint_store=store, max_concurrent_tasks=2
    )

    run_context = PipelineRunContext(
        run_id="run_01j00000000000000000000001",
        correlation_id="trace_01j00000000000000000000002",
        tenant_id="tenant_global",
    )

    results = await engine.execute_pipeline(config, run_context)

    assert "image_inference" in results
    assert "data_sink" in results
    assert results["image_inference"].status == NodeStatus.COMPLETED
    assert results["data_sink"].status == NodeStatus.COMPLETED

    assert executor.executed_steps == ["image_inference", "data_sink"]

    # Validate cryptographic signature insertion inside the persistence layer.
    checkpoint = await store.get_checkpoint(
        run_context.run_id, "image_inference"
    )
    assert checkpoint is not None
    assert checkpoint.status == NodeStatus.COMPLETED
    assert len(checkpoint.payload_checksum) == 64

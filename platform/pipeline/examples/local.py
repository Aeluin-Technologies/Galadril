"""Standalone execution script using local YAML configuration and native exception handling."""

import asyncio
import logging
import sys
from pathlib import Path

import yaml
from galadril_pipeline.compiler.resources import (
    AbstractStepExecutor,
    NodeStatus,
    StepRuntimeInput,
    StepRuntimeOutput,
)
from galadril_pipeline.config import PipelineConfig
from galadril_pipeline.runtime.engine import (
    AbstractCheckpointStore,
    AsyncPipelineEngine,
)
from galadril_pipeline.runtime.schemas import PipelineRunContext, StepCheckpoint
from pydantic import ValidationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("galadril_pipeline.example")


class LocalFileSystemExecutor(AbstractStepExecutor):
    """Local executor translating runtime mutations into local URI pointers."""

    async def execute_step(
        self, runtime_input: StepRuntimeInput
    ) -> StepRuntimeOutput:
        logger.info(f"Processing node: '{runtime_input.step_name}'")
        await asyncio.sleep(0.02)

        target_uri = f"file:///tmp/galadril_runtime/{runtime_input.step_name}_output.parquet"

        return StepRuntimeOutput(
            status=NodeStatus.COMPLETED,
            records_processed=500,
            latency_seconds=0.02,
            storage_uri_pointers=[target_uri],
            metrics={"io_engine": "local_fs"},
        )


class LocalCheckpointStore(AbstractCheckpointStore):
    """Volatile transactional registry for runtime checkpoints tracking."""

    def __init__(self) -> None:
        self._store = {}

    async def get_checkpoint(
        self, run_id: str, step_name: str
    ) -> StepCheckpoint | None:
        return self._store.get((run_id, step_name))

    async def save_checkpoint(
        self, run_id: str, checkpoint: StepCheckpoint
    ) -> None:
        self._store[(run_id, checkpoint.step_name)] = checkpoint
        logger.info(
            f"Checkpoint verified for '{checkpoint.step_name}' [SHA-256: {checkpoint.payload_checksum}]"
        )


def main():
    """Validates configuration definitions and executes the asynchronous pipeline core."""
    config_path = (
        Path(__file__).parent.parent.parent.parent
        / "examples"
        / "pipeline.yaml"
    )

    if not config_path.exists():
        logger.error(
            f"Target configuration file absent at destination: {config_path.absolute()}"
        )
        return

    try:
        with open(config_path, encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)

        # Map configuration structural schemas via Pydantic validator layer.
        config_model = PipelineConfig.model_validate(raw_data)

        # Extract list of kafka topics from source components.
        # This is not used for `galadril-vision`--all data come from "raw".
        kafka_topics = [source.topic for source in config_model.sources]
        print(f"Topics from config are: {', '.join(kafka_topics)}")

        # Evaluate topological order to verify execution plan feasibility.
        execution_order = config_model.get_topological_order()

        print("\nExecution plan:")
        steps_by_name = {step.step: step for step in config_model.pipeline}
        for i, node_id in enumerate(execution_order, 1):
            if node_id in steps_by_name:
                step = steps_by_name[node_id]
                print(f"  {i}. Step: {step.step}")
                print(f"     Type: {step.type.value}")
                print(f"     Inputs: {step.input_from}\n")

        executor = LocalFileSystemExecutor()
        store = LocalCheckpointStore()
        engine = AsyncPipelineEngine(
            executor=executor, checkpoint_store=store, max_concurrent_tasks=4
        )

        run_context = PipelineRunContext(
            run_id="run_01j00000000000000000000001",
            correlation_id="trace_01j00000000000000000000002",
            tenant_id="tenant_local_evaluation",
        )

        summary = asyncio.run(
            engine.execute_pipeline(config_model, run_context)
        )

        print("\nFinal Async Execution Summary:")
        for node_id, snapshot in summary.items():
            print(
                f"  Node: '{node_id}' -> Status: {snapshot.status.value} | Mutated Records: {snapshot.records_mutated}"
            )

    except ValidationError as e:
        print(f"Schema validation failure detected by Pydantic: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"Topological graph validation or business rule violation: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected operational breakdown inside the runtime layer: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

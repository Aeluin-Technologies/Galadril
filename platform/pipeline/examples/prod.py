"""Standalone Dagster deployment using local YAML configuration."""

import logging
from pathlib import Path
import dagster as dg
from galadril_pipeline.compiler.resources import (
    AbstractStepExecutor,
    NodeStatus,
    StepRuntimeInput,
    StepRuntimeOutput,
)
from galadril_pipeline import PipelineParser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("galadril_pipeline.dagster_deployment")


class LocalFileSystemExecutor(AbstractStepExecutor):
    """Local executor translating runtime mutations into local URI pointers, integrated as a native Dagster ConfigurableResource."""

    async def execute_step(
        self, runtime_input: StepRuntimeInput
    ) -> StepRuntimeOutput:
        # Dagster automatically captures standard logging or context logs.
        logger.info(f"Processing node via Dagster: '{runtime_input.step_name}'")
        target_uri = f"file:///tmp/galadril_runtime/{runtime_input.step_name}_output.parquet"
        return StepRuntimeOutput(
            status=NodeStatus.COMPLETED,
            records_processed=500,
            latency_seconds=0.02,
            storage_uri_pointers=[target_uri],
            metrics={"io_engine": "local_fs", "orchestrator": "dagster"},
        )


def create_pipeline_definitions() -> dg.Definitions:
    """Loads the declarative configuration and compiles it into high-level Dagster Definitions."""
    config_path = (
        Path(__file__).parent.parent.parent.parent
        / "examples"
        / "pipeline.yaml"
    )
    if not config_path.exists():
        raise FileNotFoundError(
            f"Target configuration file absent at destination: {config_path.absolute()}"
        )

    config_model = PipelineParser.from_yaml(config_path)
    defs = PipelineParser.to_dagster_definitions(config_model)

    local_executor = LocalFileSystemExecutor()

    return dg.Definitions(
        assets=defs.assets,
        schedules=defs.schedules,
        resources={
            "executor": local_executor,
        },
    )


defs = create_pipeline_definitions()

if __name__ == "__main__":
    assets_list = []
    if defs.assets is not None:
        assets_list = [
            asset for asset in defs.assets 
            if isinstance(asset, (dg.AssetsDefinition, dg.AssetSpec, dg.SourceAsset))
        ]

    dg.materialize(
        assets=assets_list,
        resources=defs.resources,
    )

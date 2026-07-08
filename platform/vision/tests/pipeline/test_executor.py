"""Unit tests targeting pure lazy execution topology assembly and UDF step branching."""

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from galadril_pipeline.config import PipelineConfig, StepType
from galadril_pipeline.runtime.batch import PipelineResult
from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.pipeline.executor import ESKGPipelineExecutor

mock_daft = MagicMock()
mock_daft.read_parquet.return_value = MagicMock()
sys.modules["daft"] = mock_daft


@pytest.fixture
def mock_pipeline_environment() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Constructs isolated environment variables required to run functional pipeline executors."""
    pipe_cfg = MagicMock(spec=PipelineConfig)
    pipe_cfg.sources = []
    pipe_cfg.pipeline = []

    vision_cfg = MagicMock(spec=VisionConfig)
    vision_cfg.raw_store.bucket = "raw"
    vision_cfg.models_store.bucket = "models"

    pg_client = MagicMock(spec=PostgresClient)
    pg_client._config = MagicMock()

    return pipe_cfg, vision_cfg, pg_client


@pytest.mark.asyncio
@patch("galadril_vision.pipeline.executor.DownloadDataWorker")
async def test_executor_linear_graph_materialization(
    mock_worker_cls: MagicMock, mock_pipeline_environment: tuple[Any, ...]
) -> None:
    """Assembles lazy evaluation blocks and validates structural pipeline telemetry results."""
    pipe_cfg, vision_cfg, pg_client = mock_pipeline_environment

    mock_df = MagicMock()
    mock_df.with_column.return_value = mock_df
    mock_df.where.return_value = mock_df
    mock_df.collect.return_value = [1, 2, 3]
    sys.modules["daft"].read_parquet.return_value = mock_df

    mock_worker_instance = MagicMock()
    mock_worker_cls.return_value = mock_worker_instance

    executor = ESKGPipelineExecutor(
        config=pipe_cfg, vision_config=vision_cfg, pg_client=pg_client
    )

    result = await executor.execute("s3://parquet/input.parquet")

    assert isinstance(result, PipelineResult)
    assert result.processed_records == 3
    sys.modules["daft"].read_parquet.assert_called_once_with(
        "s3://parquet/input.parquet"
    )


@pytest.mark.asyncio
async def test_executor_validation_failure_on_incomplete_params(
    mock_pipeline_environment: tuple[Any, ...],
) -> None:
    """Ensures a ValueError is thrown when step requirements fail criteria checks."""
    pipe_cfg, vision_cfg, pg_client = mock_pipeline_environment

    invalid_step = MagicMock()
    invalid_step.type = StepType.INFERENCE
    invalid_step.model = None
    invalid_step.params.model_extra = {}
    pipe_cfg.pipeline = [invalid_step]

    mock_df = MagicMock()
    sys.modules["daft"].read_parquet.return_value = mock_df

    executor = ESKGPipelineExecutor(
        config=pipe_cfg, vision_config=vision_cfg, pg_client=pg_client
    )

    with pytest.raises(
        ValueError, match="Incomplete parameters for 'inference' step"
    ):
        await executor.execute("s3://parquet/input.parquet")

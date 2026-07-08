"""Unit tests targeting downstream tracking mechanics and causal model context assertions."""

from unittest.mock import AsyncMock, MagicMock, patch

import dagster as dg
import pytest
from galadril_pipeline.resources.causal import CausalRunnerResource
from galadril_vision.common.config import VisionConfig


@patch("galadril_pipeline.resources.causal.AmarthCausalRunner")
@patch("galadril_pipeline.resources.causal.GraphStore")
def test_causal_runner_resource_lifecycle(
    mock_graph_store: MagicMock, mock_runner_cls: MagicMock
) -> None:
    """Validates configuration passing and setup mechanics for the causal runner resource."""
    mock_context = MagicMock(spec=dg.InitResourceContext)

    mock_config = MagicMock(spec=VisionConfig)
    mock_config.postgres = {"host": "localhost", "database": "test_db"}

    mock_db_provider = MagicMock()
    mock_pg_client = MagicMock()
    mock_db_provider.client = mock_pg_client

    resource = CausalRunnerResource(
        graph_config=mock_config, db_provider=mock_db_provider
    )

    resource.setup_for_execution(mock_context)

    mock_graph_store.assert_called_once_with(
        config=mock_config.postgres, client=mock_pg_client
    )
    mock_runner_cls.assert_called_once_with(
        pg=mock_pg_client, graph=mock_graph_store.return_value
    )


@pytest.mark.asyncio
async def test_causal_runner_uninitialized_execution() -> None:
    """Ensures invocation checks raise validation structural errors before context blocks are verified."""
    mock_config = MagicMock(spec=VisionConfig)
    resource = CausalRunnerResource(
        graph_config=mock_config, db_provider=MagicMock()
    )

    with pytest.raises(
        RuntimeError, match="CausalRunnerResource accessed before setup."
    ):
        await resource.run(MagicMock())


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.causal.AmarthCausalRunner")
@patch("galadril_pipeline.resources.causal.GraphStore")
async def test_causal_runner_successful_execution(
    mock_graph_store: MagicMock, mock_runner_cls: MagicMock
) -> None:
    """Validates asynchronous execution maps arguments downstream to the AmarthCausalRunner engine."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(return_value={"status": "processed_causal"})
    mock_runner_cls.return_value = mock_runner

    mock_config = MagicMock(spec=VisionConfig)
    mock_db_provider = MagicMock()

    resource = CausalRunnerResource(
        graph_config=mock_config, db_provider=mock_db_provider
    )
    resource.setup_for_execution(mock_context)

    batch_arg = MagicMock()
    result = await resource.run(batch=batch_arg)

    assert result == {"status": "processed_causal"}
    mock_runner.run.assert_called_once_with(batch=batch_arg)

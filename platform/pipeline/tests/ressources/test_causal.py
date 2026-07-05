"""Unit tests targeting downstream tracking mechanics and causal model context assertions."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import dagster as dg

from galadril_pipeline.resources.causal import CausalRunnerResource


@patch("galadril_pipeline.resources.causal.AmarthCausalRunner")
@patch("galadril_pipeline.resources.causal.GraphStore")
def test_causal_runner_resource_setup_fallback_attributes(
    mock_graph_store: MagicMock, mock_runner_cls: MagicMock
) -> None:
    """Validates configuration fallback mechanics pass missing topological pointers safely downstream."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_config_provider = MagicMock()
    mock_db_provider = MagicMock()

    base_cfg = mock_config_provider.vision_config
    del base_cfg.graph
    base_cfg.connectors.graph = "nested_graph_config"

    resource = CausalRunnerResource(
        config_provider=mock_config_provider, db_provider=mock_db_provider
    )
    resource.setup_for_execution(mock_context)

    mock_graph_store.assert_called_once_with(
        config="nested_graph_config", client=mock_db_provider.client
    )
    mock_runner_cls.assert_called_once_with(
        pg=mock_db_provider.client, graph=mock_graph_store.return_value
    )


def test_causal_runner_resource_setup_missing_config() -> None:
    """Ensures missing causal structure graphs raise clear parameter verification flags."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_config_provider = MagicMock()
    base_cfg = mock_config_provider.vision_config
    del base_cfg.graph
    del base_cfg.connectors.graph

    resource = CausalRunnerResource(
        config_provider=mock_config_provider, db_provider=MagicMock()
    )
    with pytest.raises(
        ValueError, match="Graph configuration missing from VisionConfig."
    ):
        resource.setup_for_execution(mock_context)


@pytest.mark.asyncio
async def test_causal_runner_uninitialized_execution() -> None:
    """Ensures invocation checks raise validation structural errors before context blocks are verified."""
    resource = CausalRunnerResource(
        config_provider=MagicMock(), db_provider=MagicMock()
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
    """Validates structural processing contexts parse fully into targeted execution assertions."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(return_value={"status": "completed"})
    mock_runner_cls.return_value = mock_runner

    resource = CausalRunnerResource(
        config_provider=MagicMock(), db_provider=MagicMock()
    )
    resource.setup_for_execution(mock_context)

    batch_arg = MagicMock()
    res = await resource.run(batch=batch_arg)
    assert res == {"status": "completed"}
    mock_runner.run.assert_called_once_with(batch=batch_arg)

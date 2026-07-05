"""Causal graph model execution tracking resource."""

from typing import Any, Optional
import dagster as dg
from pydantic import PrivateAttr
from galadril_pipeline.resources.config import VisionConfigResource
from galadril_pipeline.resources.postgres import PostgresResource
from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult
from galadril_vision.causal.runner import AmarthCausalRunner
from galadril_vision.connectors.postgres.graph import GraphStore


class CausalRunnerResource(dg.ConfigurableResource):
    """Configurable context resource establishing references for downstream tracking models."""

    config_provider: dg.ResourceDependency[VisionConfigResource]
    db_provider: dg.ResourceDependency[PostgresResource]
    _runner: Optional[AmarthCausalRunner] = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Initializes the causal runner engine with relational graph configurations."""
        base_cfg = self.config_provider.vision_config
        pg_client = self.db_provider.client
        graph_config = getattr(
            base_cfg, "graph", getattr(base_cfg.connectors, "graph", None)
        )

        if graph_config is None:
            raise ValueError("Graph configuration missing from VisionConfig.")

        self._runner = AmarthCausalRunner(
            pg=pg_client,
            graph=GraphStore(config=graph_config, client=pg_client),
        )

    async def run(self, batch: BatchHandle[PipelineResult]) -> dict[str, Any]:
        """Runs the specialized causal tracking graphs on the completed asset processing context."""
        if self._runner is None:
            raise RuntimeError("CausalRunnerResource accessed before setup.")
        return await self._runner.run(batch=batch)

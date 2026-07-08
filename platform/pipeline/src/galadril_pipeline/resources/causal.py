"""Causal graph model execution tracking resource."""

from typing import Any

import dagster as dg
from galadril_vision.causal.runner import AmarthCausalRunner
from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.postgres.graph import GraphStore
from pydantic import Field, PrivateAttr

from galadril_pipeline.resources.postgres import PostgresResource
from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult


class CausalRunnerResource(dg.ConfigurableResource):
    """Configurable context resource establishing references for downstream tracking models."""

    graph_config: VisionConfig = Field(
        description="Relational graph configurations for the causal runner."
    )

    db_provider: PostgresResource

    _runner: AmarthCausalRunner | None = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Initializes the causal runner engine with relational graph configurations."""
        pg_client = self.db_provider.client

        self._runner = AmarthCausalRunner(
            pg=pg_client,
            graph=GraphStore(
                config=self.graph_config.postgres, client=pg_client
            ),
        )

    async def run(self, batch: BatchHandle[PipelineResult]) -> dict[str, Any]:
        """Runs the specialized causal tracking graphs on the completed asset processing context."""
        if self._runner is None:
            raise RuntimeError("CausalRunnerResource accessed before setup.")
        return await self._runner.run(batch=batch)

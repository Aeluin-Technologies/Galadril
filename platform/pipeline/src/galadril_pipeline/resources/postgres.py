"""PostgreSQL database connectivity resource for Dagster pipeline steps."""

import asyncio
from typing import Optional
import dagster as dg
from pydantic import PrivateAttr

from galadril_pipeline.resources.config import VisionConfigResource
from galadril_vision.connectors.postgres.client import PostgresClient


class PostgresResource(dg.ConfigurableResource):
    """Shared resource managing a single stateful PostgreSQL engine connection client."""

    config_provider: dg.ResourceDependency[VisionConfigResource]
    _client: Optional[PostgresClient] = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Initializes the PostgresClient using infrastructure configuration."""
        base_cfg = self.config_provider.vision_config
        self._client = PostgresClient(base_cfg.postgres)

    def teardown_after_execution(self, context: dg.InitResourceContext) -> None:
        """Safely closes the asynchronous database connection pool."""
        if self._client:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._client.close())
            except RuntimeError:
                asyncio.run(self._client.close())

    @property
    def client(self) -> PostgresClient:
        """Returns the active PostgresClient instance."""
        if self._client is None:
            raise RuntimeError("PostgresResource client accessed before setup.")
        return self._client

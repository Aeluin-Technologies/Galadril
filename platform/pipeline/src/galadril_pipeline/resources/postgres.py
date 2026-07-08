"""PostgreSQL database connectivity resource for Dagster pipeline steps."""

import asyncio

import dagster as dg
from galadril_vision.common.config import PostgresConnectorConfig
from galadril_vision.connectors.postgres.client import PostgresClient
from pydantic import Field, PrivateAttr


class PostgresResource(dg.ConfigurableResource):
    """Shared resource managing a single stateful PostgreSQL engine connection client."""

    host: str = Field(description="PostgreSQL server hostname.")
    port: int = Field(description="PostgreSQL server port.")
    username: str = Field(description="Database username.")
    password: str = Field(description="Database password.", exclude=True)
    database: str = Field(description="Target database name.")

    _client: PostgresClient | None = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Initializes the PostgresClient using infrastructure configuration."""
        config = PostgresConnectorConfig(
            host=f"{self.host}:{self.port}",
            user=self.username,
            password=self.password,
            database=self.database,
        )

        self._client = PostgresClient(config=config)

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

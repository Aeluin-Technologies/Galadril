"""PostgreSQL-backed ontology runtime composition for Vision pipeline blocks."""

from __future__ import annotations

import asyncio

from galadril_ontology import (
    OntologyRuntimeManager,
    OntologySlice,
    OntologySliceRequest,
)
from galadril_ontology.postgres import PostgresOntologyRepository

from galadril_vision.common.config import PostgresConnectorConfig
from galadril_vision.connectors.postgres.client import PostgresClient


class VisionPostgresOntologyStore:
    """Lazily owns one actor-local PostgreSQL ontology connection pool."""

    __slots__ = ("_client", "_config", "_connect_lock", "_repository")

    def __init__(self, config: PostgresConnectorConfig) -> None:
        self._config = config
        self._client: PostgresClient | None = None
        self._repository: PostgresOntologyRepository | None = None
        self._connect_lock = asyncio.Lock()

    async def _get_repository(self) -> PostgresOntologyRepository:
        repository = self._repository
        if repository is not None:
            return repository
        async with self._connect_lock:
            repository = self._repository
            if repository is not None:
                return repository
            client = PostgresClient(self._config)
            await client.connect()
            repository = PostgresOntologyRepository(client)
            self._client = client
            self._repository = repository
            return repository

    async def load_runtime_slice(
        self, request: OntologySliceRequest
    ) -> OntologySlice:
        """Queries current production metadata and resources for every block."""
        repository = await self._get_repository()
        return await repository.load_runtime_slice(request)

    async def close(self) -> None:
        """Releases the actor-local pool when an embedding runtime shuts down."""
        client = self._client
        if client is not None:
            await client.close()
        self._client = None
        self._repository = None


def build_vision_ontology_runtime(
    config: PostgresConnectorConfig,
) -> OntologyRuntimeManager:
    """Builds the production manager without caching mutable publication state."""
    return OntologyRuntimeManager(VisionPostgresOntologyStore(config))

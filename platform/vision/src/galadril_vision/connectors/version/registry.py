"""Composes Vision execution with Registry-derived ontology slices."""

from datetime import UTC, datetime

import orjson
from galadril_ontology import (
    OntologyRuntimeManager,
    OntologySlice,
    OntologySliceRequest,
)
from galadril_registry_api import RegistryClient, RegistryConfig


class VisionRegistryOntologyStore:
    """Defers the gRPC channel until execution inside a Ray actor."""

    __slots__ = ("_client", "_config")

    def __init__(self, config: RegistryConfig) -> None:
        self._config = config
        self._client: RegistryClient | None = None

    async def load_runtime_slice(
        self, request: OntologySliceRequest
    ) -> OntologySlice:
        """Loads a dependency-closed slice pinned to the executing DAG."""
        if self._client is None:
            self._client = RegistryClient(self._config)
        artifact = await self._client.get_ontology_slice(
            request.tenant_id,
            request.pipeline_id,
            request.pipeline_revision_id,
            request.block_id,
        )
        ontology: object = orjson.loads(artifact.ontology_json)
        publication_metadata: object = orjson.loads(
            artifact.publication_metadata_json
        )
        binding_metadata: object = orjson.loads(artifact.binding_metadata_json)
        if (
            not isinstance(ontology, dict)
            or not isinstance(publication_metadata, dict)
            or not isinstance(binding_metadata, dict)
        ):
            raise ValueError("Registry returned an invalid ontology slice")
        return OntologySlice.model_validate(
            {
                "metadata": {
                    "tenant_id": request.tenant_id,
                    "ontology_id": artifact.ontology_id,
                    "publication_id": artifact.publication_id,
                    "revision_id": artifact.revision_id,
                    "base_version": artifact.base_version,
                    "base_hash": artifact.base_hash,
                    "effective_hash": artifact.effective_hash,
                    "publication_metadata": publication_metadata,
                    "binding_metadata": binding_metadata,
                    "published_at": datetime.fromtimestamp(
                        artifact.published_at_ms / 1000, tz=UTC
                    ),
                },
                "ontology": ontology,
            }
        )

    async def close(self) -> None:
        """Closes the actor-local Registry channel."""
        if self._client is not None:
            await self._client.close()
        self._client = None


def build_vision_ontology_runtime(
    config: RegistryConfig,
) -> OntologyRuntimeManager:
    """Builds a runtime whose only persistence boundary is Registry gRPC."""
    return OntologyRuntimeManager(VisionRegistryOntologyStore(config))

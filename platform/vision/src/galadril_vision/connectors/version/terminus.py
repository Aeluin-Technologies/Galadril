"""Composes Vision ontology semantics with native TerminusDB persistence."""

from galadril_ontology import (
    OntologyRuntimeManager,
    OntologySlice,
    OntologySliceRequest,
)
from galadril_ontology.backends.terminus import (
    TerminusClient,
    TerminusConfig,
    TerminusOntologyRepository,
)


class VisionTerminusOntologyStore:
    """Defers HTTP pool creation until execution inside a Ray actor."""

    __slots__ = ("_config", "_client", "_repository")

    def __init__(self, config: TerminusConfig) -> None:
        self._config = config
        self._client: TerminusClient | None = None
        self._repository: TerminusOntologyRepository | None = None

    async def load_runtime_slice(
        self, request: OntologySliceRequest
    ) -> OntologySlice:
        if self._repository is None:
            self._client = TerminusClient(self._config)
            self._repository = TerminusOntologyRepository(self._client)
        return await self._repository.load_runtime_slice(request)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None
        self._repository = None


def build_vision_ontology_runtime(
    config: TerminusConfig,
) -> OntologyRuntimeManager:
    return OntologyRuntimeManager(VisionTerminusOntologyStore(config))

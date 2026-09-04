"""Actor composition must not serialize open HTTP transports."""

import asyncio
from unittest.mock import AsyncMock, patch

import cloudpickle
import pytest
from galadril_ontology.backends.terminus import TerminusConfig
from galadril_vision.connectors.version.terminus import (
    VisionTerminusOntologyStore,
)


def test_runtime_transport_is_created_inside_the_actor() -> None:
    store = VisionTerminusOntologyStore(TerminusConfig())
    restored = cloudpickle.loads(cloudpickle.dumps(store))
    client = AsyncMock()
    repository = AsyncMock()
    with (
        patch(
            "galadril_vision.connectors.version.terminus.TerminusClient",
            return_value=client,
        ) as factory,
        patch(
            "galadril_vision.connectors.version.terminus.TerminusOntologyRepository",
            return_value=repository,
        ),
    ):
        factory.assert_not_called()
        asyncio.run(restored.close())
        factory.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))

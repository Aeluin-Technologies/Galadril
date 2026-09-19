"""Actor composition must not serialize open Registry transports."""

import asyncio
from unittest.mock import patch

import cloudpickle
import pytest
from galadril_registry_api import RegistryConfig
from galadril_vision.connectors.version.registry import (
    VisionRegistryOntologyStore,
)


def test_runtime_transport_is_created_inside_the_actor() -> None:
    config = RegistryConfig(endpoint="http://registry:50052")
    store = VisionRegistryOntologyStore(config)
    restored = cloudpickle.loads(cloudpickle.dumps(store))
    with patch(
        "galadril_vision.connectors.version.registry.RegistryClient"
    ) as factory:
        factory.assert_not_called()
        asyncio.run(restored.close())
        factory.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))

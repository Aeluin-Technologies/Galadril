"""Actor serialization must never retain a live Gateway transport."""

import asyncio
from unittest.mock import patch

import cloudpickle
import pytest
from galadril_ontology import GatewayGolClient


def test_runtime_transport_is_created_inside_the_actor() -> None:
    client = GatewayGolClient("http://api-proxy:8082")
    restored = cloudpickle.loads(cloudpickle.dumps(client))
    with patch("galadril_ontology.client.httpx.AsyncClient") as factory:
        asyncio.run(restored.close())
        factory.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))

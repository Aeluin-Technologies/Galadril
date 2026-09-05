"""Unit tests for actor-local model initialization and cache isolation."""

from __future__ import annotations

import asyncio

import pytest
from galadril_vision.actors import inference


class _Engine:
    """Records model loads for cache-concurrency assertions."""

    loads = 0

    def __init__(self, loader: object) -> None:
        self.loader = loader

    async def load_model(self, model_name: str) -> None:
        del model_name
        type(self).loads += 1
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_concurrent_model_requests_share_one_actor_local_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents duplicate GPU model allocation under concurrent commands."""
    inference._INFERENCE_ENGINES.clear()
    _Engine.loads = 0
    monkeypatch.setattr(inference, "InferenceEngine", _Engine)
    monkeypatch.setattr(inference, "S3Loader", lambda **_: object())

    first, second = await asyncio.gather(
        inference.get_inference_engine(
            model_name="models.Face",
            models_bucket="models",
            models_prefix="face",
            endpoint_url="http://minio:9000",
        ),
        inference.get_inference_engine(
            model_name="models.Face",
            models_bucket="models",
            models_prefix="face",
            endpoint_url="http://minio:9000",
        ),
    )

    assert first is second
    assert _Engine.loads == 1
    assert first.__class__.__name__ == "_Engine"


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))

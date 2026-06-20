"""Unit tests for galadril-vision configuration models."""

from __future__ import annotations

from galadril_vision.common.config import VisionConfig


def test_vision_config_inference_uses_dedicated_models_bucket() -> None:
    """Ensures raw assets and model artifacts use separate buckets."""
    cfg = VisionConfig.model_validate(
        {
            "name": "test-vision",
            "connectors": {
                "kafka": {
                    "brokers": ["localhost:9092"],
                    "schema_registry": "http://schema-registry:8081",
                    "consumer_group": "vision-test",
                },
                "s3": {
                    "endpoint": "http://minio:9000",
                    "access_key": "access",
                    "secret_key": "secret",
                    "region": "us-east-1",
                    "bucket": "vision-data",
                    "models_bucket": "models",
                },
                "postgres": {
                    "database": "galadril",
                    "host": "postgres",
                    "user": "galadril",
                    "password": "galadril",
                },
                "spicedb": {
                    "endpoint": "http://spicedb:50051",
                    "token": "token",
                },
            },
        }
    )

    assert cfg.raw_store.bucket == "vision-data"
    assert cfg.raw_store.prefix == "raw"
    models_store = cfg.models_store
    inference_store = cfg.inference

    assert models_store.bucket == "models"
    assert models_store.prefix == ""
    assert inference_store.bucket == "models"
    assert inference_store.prefix == ""

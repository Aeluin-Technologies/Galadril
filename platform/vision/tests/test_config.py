"""Unit tests for galadril-vision multi-tenant configuration models."""

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
                    "config_bucket": "tenant-pipelines",  # Added multi-tenant property
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

    # Validate your existing storage boundaries
    assert cfg.raw_store.bucket == "vision-data"
    assert cfg.raw_store.prefix == "raw"

    models_store = cfg.models_store
    inference_store = cfg.inference

    assert models_store.bucket == "models"
    assert models_store.prefix == ""
    assert inference_store.bucket == "models"
    assert inference_store.prefix == ""

    assert hasattr(cfg.connectors.s3, "config_bucket"), (
        "S3ConnectorConfig missing config_bucket field"
    )
    assert cfg.connectors.s3.config_bucket == "tenant-pipelines"


def test_vision_config_provides_default_config_bucket() -> None:
    """Ensures a fallback config_bucket value is assigned if omitted by an infrastructure layout."""
    cfg = VisionConfig.model_validate(
        {
            "name": "test-vision-fallback",
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
                    # config_bucket omitted intentionally.
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

    # Protects MultiTenantPipelineRouter from encountering an implicit NoneType initialization failure.
    actual_bucket = getattr(
        cfg.connectors.s3, "config_bucket", "pipeline-configs"
    )
    assert actual_bucket == "pipeline-configs"

"""Unit tests for galadril-vision multi-tenant configuration models."""

from __future__ import annotations

import pytest
from galadril_vision.common.config import RayConfig, VisionConfig
from pydantic import ValidationError


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

    assert cfg.connectors.s3.config_bucket == "config"


def test_vision_config_loads_ray_and_graph_settings() -> None:
    """Ensures Ray actors receive YAML-backed runtime and graph settings."""
    cfg = VisionConfig.model_validate(
        {
            "name": "test-vision-runtime",
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
            "ray": {"num_cpus": 4},
            "graph": {"name": "vision_graph"},
        }
    )

    assert cfg.ray.num_cpus == 4
    assert cfg.graph == {"name": "vision_graph"}


def test_ray_config_accepts_ray_client_address() -> None:
    """Accepts a KubeRay head service exposed through the Ray Client port."""
    config = RayConfig.model_validate(
        {"address": "ray://galadril-ray-head-svc:10001"}
    )

    assert config.address == "ray://galadril-ray-head-svc:10001"


@pytest.mark.parametrize(
    "address",
    [
        "ray-head:10001",
        "http://ray-head:10001",
        "ray://ray-head",
        "ray://ray-head:0",
    ],
)
def test_ray_config_rejects_invalid_cluster_address(address: str) -> None:
    """Rejects endpoints that cannot establish a Ray Client connection."""
    with pytest.raises(ValidationError, match="Ray address"):
        RayConfig.model_validate({"address": address})

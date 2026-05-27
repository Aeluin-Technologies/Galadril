"""Configuration for galadril-vision."""

from __future__ import annotations
from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Union


class S3StorageConfig(BaseSettings):
    bucket: str = "my-bucket"
    prefix: str = ""
    endpoint_url: str | None = None
    region_name: str = "eu-west-1"


class KafkaConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAFKA_")
    bootstrap_servers: str = "redpanda:9092"
    schema_registry: str = "redpanda:8081"
    group_id: str = "galadril-vision"
    topic: str = "galadril.images.metadata"
    auto_offset_reset: str = "earliest"
    enable_auto_commit: bool = False
    max_poll_records: int = 100
    session_timeout_ms: int = 30000


class PostgresConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_")
    dsn: Union[PostgresDsn, str] = Field(
        default="postgresql://postgres:postgres@postgres:5432/galadril_dev"
    )
    min_connections: int = 5
    max_connections: int = 20
    graph_name: str = "galadril_dev"
    vector_dimensions: int = 1024
    similarity_threshold: float = 0.85


class RayConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAY_")
    address: str | None = None
    num_cpus: int | None = None
    num_gpus: int | None = None


class VisionConfig(BaseSettings):
    """Root configuration aggregating all sub-configs."""

    model_config = SettingsConfigDict(
        env_prefix="VISION_",
        env_nested_delimiter="__",
    )

    kafka: KafkaConfig = Field(default_factory=KafkaConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    ray: RayConfig = Field(default_factory=RayConfig)

    image_store: S3StorageConfig = Field(
        default_factory=lambda: S3StorageConfig(prefix="raw")
    )

    inference: S3StorageConfig = Field(
        default_factory=lambda: S3StorageConfig(prefix="models")
    )

    batch_size: int = 32
    unknown_vertex_prefix: str = "UNKNOWN"

"""Configuration structures for galadril-vision."""

from __future__ import annotations

from typing import Any
import yaml
from pydantic import BaseModel, Field

from galadril_pipeline import PipelineConfig


class KafkaConnectorConfig(BaseModel):
    """Configuration parameters for the Kafka message broker."""

    brokers: list[str]
    schema_registry: str
    consumer_group: str

    auto_offset_reset: str = "earliest"
    enable_auto_commit: bool = False
    max_poll_records: int = 100
    session_timeout_ms: int = 30000
    authz_dlq_topic: str | None = None

    @property
    def bootstrap_servers(self) -> str:
        return ",".join(self.brokers)

    @property
    def group_id(self) -> str:
        return self.consumer_group


class S3ConnectorConfig(BaseModel):
    """Storage parameters targeting S3/MinIO infrastructure components.

    The raw ingestion bucket stays on `bucket`, while model artifacts use
    `models_bucket` to keep the storage responsibilities isolated.
    """

    endpoint: str
    access_key: str
    secret_key: str
    region: str
    bucket: str
    models_bucket: str = "models"
    bucket_notifications: str | None = None
    staging_bucket: str | None = None


class PostgresConnectorConfig(BaseModel):
    """PostgreSQL storage parameters."""

    database: str
    host: str
    user: str
    password: str

    min_connections: int = 5
    max_connections: int = 20
    graph_name: str = "galadril_dev"
    vector_dimensions: int = 1024
    similarity_threshold: float = 0.85
    vector_search_timeout_ms: int = 5000

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}/{self.database}"


class SpiceDBConnectorConfig(BaseModel):
    """Authorization connector parameters targeting SpiceDB instances."""

    endpoint: str
    token: str
    schema_name: str | None = None

    max_local_retries: int = 20
    base_retry_ms: int = 250
    max_retry_ms: int = 10000


class ConnectorsConfig(BaseModel):
    """Consolidated connector definitions block."""

    kafka: KafkaConnectorConfig
    s3: S3ConnectorConfig
    postgres: PostgresConnectorConfig
    spicedb: SpiceDBConnectorConfig


class SourceConfig(BaseModel):
    """Metadata configurations referencing processing ingress channels."""

    id: str
    topic: str
    match_pattern: str
    schema_path: str


class PipelineStepConfig(BaseModel):
    """A discrete unit step block sequence layout blueprint inside the pipeline execution map."""

    step: str
    type: str
    input_from: list[str]
    model: str | None = None
    artifact_path: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class S3StorageConfig(BaseModel):
    """Downstream compatibility container model matching client specifications."""

    bucket: str
    prefix: str
    endpoint_url: str | None = None
    region_name: str = "us-east-1"
    access_key: str | None = None
    secret_key: str | None = None


class RayConfig(BaseModel):
    """Resource configuration tuning bounds targeting Ray cluster engines."""

    address: str | None = None
    num_cpus: int | None = None
    num_gpus: int | None = None


class VisionConfig(BaseModel):
    """Root configuration aggregator mapping directly to the unified pipeline YAML layout."""

    name: str
    connectors: ConnectorsConfig
    sources: list[SourceConfig] = Field(default_factory=list)
    pipeline: list[PipelineStepConfig] = Field(default_factory=list)

    batch_size: int = 32
    batch_timeout_s: float | None = 300.0
    unknown_vertex_prefix: str = "UNKNOWN"

    @property
    def kafka(self) -> KafkaConnectorConfig:
        return self.connectors.kafka

    @property
    def postgres(self) -> PostgresConnectorConfig:
        return self.connectors.postgres

    @property
    def spicedb(self) -> SpiceDBConnectorConfig:
        return self.connectors.spicedb

    @property
    def ray(self) -> RayConfig:
        return RayConfig()

    @property
    def raw_store(self) -> S3StorageConfig:
        """Returns storage settings for raw multimodal assets."""
        return S3StorageConfig(
            bucket=self.connectors.s3.bucket,
            prefix="raw",
            endpoint_url=self.connectors.s3.endpoint,
            region_name=self.connectors.s3.region,
            access_key=self.connectors.s3.access_key,
            secret_key=self.connectors.s3.secret_key,
        )

    @property
    def image_store(self) -> S3StorageConfig:
        """Backward-compatible alias for raw multimodal storage."""
        return self.raw_store

    @property
    def models_store(self) -> S3StorageConfig:
        """Returns the dedicated bucket used to store model artifacts at the root."""
        return S3StorageConfig(
            bucket=self.connectors.s3.models_bucket,
            prefix="",
            endpoint_url=self.connectors.s3.endpoint,
            region_name=self.connectors.s3.region,
            access_key=self.connectors.s3.access_key,
            secret_key=self.connectors.s3.secret_key,
        )

    @property
    def inference(self) -> S3StorageConfig:
        """Backward-compatible alias for the dedicated model artifact store."""
        return self.models_store

    def get_kafka_topics(self) -> list[str]:
        """Calculates a unique deduplicated list of target routing streams from configuration sources."""
        return list(set(source.topic for source in self.sources))

    def to_pipeline_config(self) -> PipelineConfig:
        """Transforms fields into a structurally isolated PipelineConfig model class instance."""
        data = self.model_dump(
            include={"name", "connectors", "sources", "pipeline"}
        )
        return PipelineConfig.model_validate(data)

    @classmethod
    def from_yaml(cls, path: str) -> VisionConfig:
        """Loads and binds file stream configurations into verified class properties."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)

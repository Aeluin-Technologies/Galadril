"""Configuration structures for galadril-vision."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from galadril_pipeline.config import PipelineConfig
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class KafkaConnectorConfig(BaseModel):
    """Configuration parameters for the Kafka message broker."""

    brokers: list[str]
    schema_registry: str
    consumer_group: str

    auto_offset_reset: Literal["latest", "earliest", "none"] = "earliest"
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
    """Storage parameters targeting S3/MinIO infrastructure components."""

    endpoint: str
    access_key: str
    secret_key: str
    region: str
    bucket: str
    models_bucket: str = "models"
    config_bucket: str = "config"
    bucket_notifications: str = "s3-notification"
    staging_bucket: str = "staging"


class PostgresConnectorConfig(BaseModel):
    """PostgreSQL storage parameters."""

    database: str
    host: str
    user: str
    password: str
    maintenance_user: str | None = None
    maintenance_password: str | None = None

    min_connections: int = 5
    max_connections: int = 20
    graph_name: str = "galadril_dev"
    vector_dimensions: int = 1024
    similarity_threshold: float = 0.85
    vector_search_timeout_ms: int = 5000

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}/{self.database}"

    @property
    def maintenance_dsn(self) -> str | None:
        """Returns the separately credentialed maintenance DSN."""
        if self.maintenance_user is None or self.maintenance_password is None:
            return None
        return (
            f"postgresql://{self.maintenance_user}:{self.maintenance_password}"
            f"@{self.host}/{self.database}"
        )

    @model_validator(mode="after")
    def validate_maintenance_credentials(self) -> PostgresConnectorConfig:
        """Rejects partially configured privileged database identities."""
        if (self.maintenance_user is None) != (
            self.maintenance_password is None
        ):
            raise ValueError(
                "maintenance_user and maintenance_password must be configured together"
            )
        return self


class IdentityResolutionConfig(BaseModel):
    """LI-ESKG runtime, calibration, and candidate-gating settings."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = True
    ledger_root: str | None = None
    candidate_top_k: int = Field(default=8, ge=1, le=256)
    provider_id: int = Field(default=1, ge=1)
    schema_id: int = Field(default=1, ge=1)
    model_version: int = Field(default=1, ge=1)
    calibration_id: int = Field(default=1, ge=1)
    postgres_backend_id: int = Field(default=1, ge=1)
    host_snapshot: int = Field(default=0, ge=0)
    candidate_snapshot: int = Field(default=0, ge=0)
    h3_resolution: int = Field(default=9, ge=0, le=15)
    h3_ring_size: int = Field(default=1, ge=0, le=512)
    probability_epsilon: float = Field(default=1.0e-6, gt=0.0, lt=0.5)
    vector_similarity_midpoint: float = Field(default=0.85, ge=-1.0, le=1.0)
    vector_similarity_scale: float = Field(default=12.0, gt=0.0)
    vector_weight: float = Field(default=1.0, ge=0.0)
    pipeline_probability_weight: float = Field(default=1.0, ge=0.0)
    max_abs_log_likelihood_ratio: float = Field(default=20.0, gt=0.0)
    new_evidence_log_potential: float = 0.0
    noise_evidence_log_potential: float = -4.0
    candidate_log_prior: float = 0.0
    new_log_prior: float = 0.0
    noise_log_prior: float = -4.0
    queue_capacity: int = Field(default=1024, ge=1)
    result_capacity: int = Field(default=128, ge=1)
    max_batch_size: int = Field(default=256, ge=1)
    max_batch_latency_ms: int = Field(default=5, ge=1)
    worker_threads: int = Field(default=2, ge=1)
    pool_max_idle: int = Field(default=4, ge=1)
    result_timeout_seconds: float = Field(default=10.0, gt=0.0)


class SpiceDBConnectorConfig(BaseModel):
    """Authorization connector parameters targeting SpiceDB instances."""

    endpoint: str
    token: str
    schema_name: str | None = None

    max_local_retries: int = 20
    base_retry_ms: int = 250
    max_retry_ms: int = 10000


class TelemetryConfig(BaseModel):
    """Configuration parameters for the OpenTelemetry infrastructure."""

    enabled: bool = True
    otlp_endpoint: str | None = None
    otlp_insecure: bool = False
    environment: str = "production"
    version: str = "1.0.0"


class ConnectorsConfig(BaseModel):
    """Consolidated connector definitions block."""

    kafka: KafkaConnectorConfig
    s3: S3ConnectorConfig
    postgres: PostgresConnectorConfig
    spicedb: SpiceDBConnectorConfig
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)


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
    """Connection and resource settings for local or shared Ray runtimes."""

    model_config = ConfigDict(extra="forbid")

    address: str | None = None
    num_cpus: int | None = None
    num_gpus: int | None = None
    gpu_actor_num_gpus: float | None = Field(default=None, ge=0.0, le=1.0)
    namespace: str = "galadril"
    actor_replicas: int = Field(default=1, ge=1)

    @field_validator("address", mode="before")
    @classmethod
    def validate_ray_client_address(cls, value: object) -> object:
        """Accepts an empty local address or a complete Ray Client endpoint."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Ray address must be a string")
        address = value.strip()
        if not address:
            return None
        parsed = urlsplit(address)
        if parsed.scheme != "ray" or parsed.hostname is None:
            raise ValueError(
                "Ray address must use the form ray://<head-host>:<port>"
            )
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("Ray address contains an invalid port") from error
        if port is None or port == 0:
            raise ValueError("Ray address must include a valid Ray Client port")
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Ray address must not contain a path or query")
        return address


class VisionConfig(BaseModel):
    """Root configuration aggregator mapping directly to the unified pipeline YAML layout."""

    name: str
    connectors: ConnectorsConfig
    sources: list[SourceConfig] = Field(default_factory=list)
    pipeline: list[PipelineStepConfig] = Field(default_factory=list)
    ray: RayConfig = Field(default_factory=RayConfig)
    identity_resolution: IdentityResolutionConfig = Field(
        default_factory=IdentityResolutionConfig
    )
    graph: dict[str, Any] = Field(default_factory=dict)

    unknown_vertex_prefix: str = "UNKNOWN"

    @model_validator(mode="after")
    def validate_identity_actor_ownership(self) -> VisionConfig:
        """Prevents multiple actors from allocating one tenant identity sequence."""
        if self.identity_resolution.enabled and self.ray.actor_replicas != 1:
            raise ValueError(
                "identity resolution requires ray.actor_replicas=1 until "
                "tenant-sharded LI-ESKG actor ownership is configured"
            )
        return self

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
    def telemetry(self) -> TelemetryConfig:
        return self.connectors.telemetry

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
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)

//! Domain structures for Galadril data ingest routing.

use chrono::{DateTime, Utc};
use serde::Deserialize;

/// Configuration credentials for Kafka clusters.
#[derive(Debug, Clone, Deserialize)]
pub struct KafkaConnectorConfig {
    /// List of seed brokers.
    pub brokers: Vec<String>,
    /// Schema registry endpoint.
    pub schema_registry: String,
    /// Unique consumer group id.
    pub consumer_group: String,
}

/// Connectivity block for S3 compliant storage.
#[derive(Debug, Clone, Deserialize)]
pub struct S3ConnectorConfig {
    /// Endpoint target URL.
    pub endpoint: String,
    /// IAM access key.
    pub access_key: String,
    /// IAM secret key.
    pub secret_key: String,
    /// S3 regional constraints.
    pub region: String,
    /// Target bronze bucket.
    pub bucket: String,
    /// Optional notification bridge.
    pub bucket_notifications: Option<String>,
}

/// Individual source criteria used for fast runtime checking.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct SourceConfig {
    pub id: String,
    /// Event destination.
    pub topic: String,
    /// Local or remote location of target Avro layout.
    pub schema_path: Option<String>,
    /// Route regex matching rule.
    pub match_pattern: Option<String>,
    /// Payload extraction mechanism.
    #[serde(default = "default_parser")]
    pub parser: String,
}

/// Notification wrapper received from object store records.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileEvent {
    /// Origin bucket.
    pub bucket: String,
    /// Key path inside bucket.
    pub key: String,
    /// Payload byte metric.
    pub size: i64,
    /// Timestamp of receipt.
    pub received_at: DateTime<Utc>,
}

/// Dynamic payload describing execution targets for tenants.
#[derive(Debug, Clone, Deserialize)]
pub struct PipelineConfig {
    /// Active inputs.
    pub sources: Vec<Source>,
}

/// Validated dynamic source description.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct Source {
    /// Target id.
    pub id: String,
    /// Ingestion topic destination.
    pub topic: String,
    /// Filter rule regex.
    pub match_pattern: String,
    /// Attached validation layout path.
    pub schema_path: Option<String>,
    /// Target execution parser strategy.
    #[serde(default = "default_parser")]
    pub parser: String,
}

#[inline]
fn default_parser() -> String {
    "metadata".to_string()
}

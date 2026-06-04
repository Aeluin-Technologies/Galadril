//! Galadril domain models.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize)]
pub struct PipelineConfig {
    pub name: String,
    pub connectors: ConnectorsConfig,
    #[serde(default)]
    pub sources: Vec<SourceConfig>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ConnectorsConfig {
    pub kafka: Option<KafkaConnectorConfig>,
    pub s3: Option<S3ConnectorConfig>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct KafkaConnectorConfig {
    pub brokers: Vec<String>,
    pub schema_registry: String,
    pub consumer_group: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct S3ConnectorConfig {
    pub endpoint: String,
    pub access_key: String,
    pub secret_key: String,
    pub region: String,
    pub bucket: String,
    pub bucket_notifications: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SourceConfig {
    pub id: String,
    pub topic: String,
    pub schema_path: Option<String>,
    pub match_pattern: Option<String>,
    #[serde(default = "default_parser")]
    pub parser: String,
}

fn default_parser() -> String {
    "metadata".to_string()
}

/// Abstraction of S3 notification.
#[derive(Debug, Clone)]
pub struct FileEvent {
    pub bucket: String,
    pub key: String,
    pub size: i64,
    pub received_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BoundingBox {
    pub top_left_lat: f64,
    pub top_left_lon: f64,
    pub bottom_right_lat: f64,
    pub bottom_right_lon: f64,
}

/// Generic image payload.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImagePayload {
    pub id: String,
    pub timestamp: i64,
    pub ingested_at: i64,
    pub storage_path: Option<String>,
    pub source: String,
    pub mime_type: Option<String>,
    pub geometry: Option<BoundingBox>,
}

/// Generic audio payload.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioPayload {
    pub id: String,
    pub timestamp: i64,
    pub ingested_at: i64,
    pub storage_path: Option<String>,
    pub source: String,
    pub duration_seconds: Option<i32>,
    pub language: Option<String>,
}

/// Generic document payload.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DocumentPayload {
    pub id: String,
    pub timestamp: i64,
    pub ingested_at: i64,
    pub storage_path: Option<String>,
    pub source: String,
    pub original_filename: Option<String>,
    pub mime_type: Option<String>,
    pub file_hash: Option<String>,
}

/// Generic text payload.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TextPayload {
    pub id: String,
    pub timestamp: i64,
    pub ingested_at: i64,
    pub storage_path: Option<String>,
    pub source: String,
    pub content: String,
    pub url: Option<String>,
    pub author: Option<String>,
}

/// Generic transaction/state-transition payload.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TransactionPayload {
    pub id: String,
    pub timestamp: i64,
    pub ingested_at: i64,
    pub storage_path: Option<String>,
    pub source: String,
    pub sender_account: Option<String>,
    pub receiver_account: Option<String>,
    pub amount: Option<f64>,
    pub currency: Option<String>,
    pub transaction_type: Option<String>,
}

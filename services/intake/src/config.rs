//! Dynamic layer assembling explicit environment metrics and system boots.

use std::env;
use std::path::PathBuf;

use anyhow::{Context, Result};
use config::{Config, Environment, File, FileFormat};
use serde::Deserialize;

/// Consolidated engine runtime configurations.
#[derive(Debug, Clone)]
pub struct AppConfig {
    /// Local/remote pub-sub links.
    pub kafka: KafkaConfig,
    /// Ingestion object paths.
    pub s3: S3Config,
}

/// Pub-sub network targets.
#[derive(Debug, Clone)]
pub struct KafkaConfig {
    /// Brokers seed URLs.
    pub brokers: String,
    /// Group identity tracking offsets.
    pub consumer_group: String,
    /// Global data scheme registry endpoint.
    pub schema_registry: String,
}

/// Shared persistence targets.
#[derive(Debug, Clone)]
pub struct S3Config {
    /// target network URL.
    pub endpoint: String,
    /// Physical location block.
    pub region: String,
    /// Shared payload location.
    pub bucket: String,
    /// Associated asynchronous topic link.
    pub bucket_notifications: String,
    /// Multi-tenant deployment configurations storage.
    pub config_bucket: String,
    /// User identification token.
    pub access_key: String,
    /// Proof of access key token.
    pub secret_key: String,
}

#[derive(Debug, Clone, Deserialize)]
struct RawBootstrapConfig {
    connectors: RawConnectors,
}

#[derive(Debug, Clone, Deserialize)]
struct RawConnectors {
    kafka: RawKafkaConnector,
    s3: RawS3Connector,
}

#[derive(Debug, Clone, Deserialize)]
struct RawKafkaConnector {
    brokers: Vec<String>,
    schema_registry: String,
    consumer_group: String,
}

#[derive(Debug, Clone, Deserialize)]
struct RawS3Connector {
    endpoint: String,
    access_key: String,
    secret_key: String,
    region: String,
    bucket: String,
    bucket_notifications: String,
    config_bucket: String,
}

impl AppConfig {
    /// Assembles system bounds using file profiles and system variables.
    pub fn load() -> Result<Self> {
        let bootstrap_path = env::var("INTAKE_BOOTSTRAP_PATH")
            .unwrap_or_else(|_| "bootstrap.yaml".to_string());

        let builder = Config::builder()
            .set_default("connectors.kafka.consumer_group", "intake-service")?
            .add_source(
                File::from(PathBuf::from(&bootstrap_path))
                    .format(FileFormat::Yaml),
            )
            .add_source(
                Environment::with_prefix("INTAKE")
                    .separator("__")
                    .try_parsing(true),
            );

        let built_config =
            builder.build().context("Failed to build config-rs layer")?;
        let raw: RawBootstrapConfig = built_config
            .try_deserialize()
            .context("Failed to deserialize bootstrap configuration")?;

        Self::from_raw(raw)
    }

    fn from_raw(r: RawBootstrapConfig) -> Result<Self> {
        Ok(Self {
            kafka: KafkaConfig {
                brokers: r.connectors.kafka.brokers.join(","),
                consumer_group: r.connectors.kafka.consumer_group,
                schema_registry: r.connectors.kafka.schema_registry,
            },
            s3: S3Config {
                endpoint: r.connectors.s3.endpoint,
                region: r.connectors.s3.region,
                bucket: r.connectors.s3.bucket,
                bucket_notifications: r.connectors.s3.bucket_notifications,
                config_bucket: r.connectors.s3.config_bucket,
                access_key: r.connectors.s3.access_key,
                secret_key: r.connectors.s3.secret_key,
            },
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw_test_config() -> RawBootstrapConfig {
        RawBootstrapConfig {
            connectors: RawConnectors {
                kafka: RawKafkaConnector {
                    brokers: vec![
                        "redpanda:9092".to_string(),
                        "redpanda:9093".to_string(),
                    ],
                    schema_registry: "http://redpanda:8081".to_string(),
                    consumer_group: "intake-test".to_string(),
                },
                s3: RawS3Connector {
                    endpoint: "http://minio:9000".to_string(),
                    access_key: "minioadmin".to_string(),
                    secret_key: "minioadmin".to_string(),
                    region: "us-east-1".to_string(),
                    bucket: "lake".to_string(),
                    bucket_notifications: "s3-notification".to_string(),
                    config_bucket: "config".to_string(),
                },
            },
        }
    }

    #[test]
    fn from_raw_builds_without_http_or_auth_runtime_config() {
        let cfg = AppConfig::from_raw(raw_test_config());

        assert!(cfg.is_ok());
        if let Ok(cfg) = cfg {
            assert_eq!(cfg.kafka.brokers, "redpanda:9092,redpanda:9093");
            assert_eq!(cfg.s3.bucket, "lake");
        }
    }
}

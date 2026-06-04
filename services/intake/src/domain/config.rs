//! Custom configuration merging Env and YAML.

use std::{env, fs};

use anyhow::{Context, Result};

use crate::domain::models::PipelineConfig;

pub struct AppConfig {
    pub kafka_brokers: String,
    pub kafka_consumer_group: String,
    pub kafka_notification_topic: String,
    pub schema_registry: String,
    pub s3_endpoint: String,
    pub s3_bucket: String,
    pub pipeline: PipelineConfig,

    /// Optional HTTP API host. If None/empty, HTTP is disabled.
    pub http_host: Option<String>,
    pub http_port: u16,

    /// Optional shared secret protection (in addition to JWT).
    pub http_secret_key: Option<String>,

    pub jwt_es256_public_key_pem: Option<String>,
    pub jwt_issuer: Option<String>,
    pub jwt_audience: Option<String>,

    pub spicedb_endpoint: Option<String>,
    pub spicedb_token: Option<String>,
    pub cedar_policy_dsl: Option<String>,
}

impl AppConfig {
    /// Load configuration from YAML and fallback to env vars.
    pub fn from_env() -> Result<Self> {
        tracing::debug!("reading configuration");

        let config_path = env::var("PIPELINE_PATH")
            .unwrap_or_else(|_| "pipeline.yaml".to_string());

        let file_content = fs::read_to_string(&config_path)?;
        let pipeline: PipelineConfig = serde_yaml::from_str(&file_content)?;

        let kafka_brokers = pipeline
            .connectors
            .kafka
            .as_ref()
            .map(|k| k.brokers.join(","))
            .or_else(|| env::var("KAFKA_BROKERS").ok())
            .context("Missing KAFKA_BROKERS")?;

        let schema_registry = pipeline
            .connectors
            .kafka
            .as_ref()
            .map(|k| k.schema_registry.clone())
            .or_else(|| env::var("SCHEMA_REGISTRY").ok())
            .context("Missing SCHEMA_REGISTRY")?;

        let kafka_consumer_group = pipeline
            .connectors
            .kafka
            .as_ref()
            .map(|k| k.consumer_group.clone())
            .unwrap_or_else(|| {
                env::var("KAFKA_CONSUMER_GROUP")
                    .unwrap_or_else(|_| "intake-service".to_string())
            });

        let s3_endpoint = pipeline
            .connectors
            .s3
            .as_ref()
            .map(|s| s.endpoint.clone())
            .or_else(|| env::var("S3_ENDPOINT").ok())
            .context("Missing S3_ENDPOINT")?;

        let kafka_notification_topic = pipeline
            .connectors
            .s3
            .as_ref()
            .and_then(|s| s.bucket_notifications.clone())
            .unwrap_or_else(|| {
                env::var("KAFKA_TOPIC_NOTIFICATIONS")
                    .unwrap_or_else(|_| "s3-bucket-notifications".to_string())
            });

        let s3_bucket =
            env::var("S3_BUCKET").unwrap_or_else(|_| "my-bucket".to_string());

        let http_host = env::var("INTAKE_HTTP_HOST")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        let http_port = env::var("INTAKE_HTTP_PORT")
            .ok()
            .and_then(|s| s.parse::<u16>().ok())
            .unwrap_or(8080);

        let http_secret_key = env::var("INTAKE_HTTP_SECRET_KEY").ok();

        let jwt_es256_public_key_pem =
            env::var("JWT_ES256_PUBLIC_KEY_PEM").ok();
        let jwt_issuer = env::var("JWT_ISSUER").ok();
        let jwt_audience = env::var("JWT_AUDIENCE").ok();

        let spicedb_endpoint = env::var("SPICEDB_ENDPOINT").ok();
        let spicedb_token = env::var("SPICEDB_TOKEN").ok();
        let cedar_policy_dsl = env::var("CEDAR_POLICY_DSL").ok();

        // If HTTP is enabled, enforce required auth config.
        if http_host.is_some() {
            jwt_es256_public_key_pem
                .as_deref()
                .filter(|s| !s.trim().is_empty())
                .context("Missing JWT_ES256_PUBLIC_KEY_PEM (required when INTAKE_HTTP_HOST is set)")?;

            spicedb_endpoint
                .as_deref()
                .filter(|s| !s.trim().is_empty())
                .context("Missing SPICEDB_ENDPOINT (required when INTAKE_HTTP_HOST is set)")?;

            spicedb_token
                .as_deref()
                .filter(|s| !s.trim().is_empty())
                .context("Missing SPICEDB_TOKEN (required when INTAKE_HTTP_HOST is set)")?;
        }

        Ok(Self {
            kafka_brokers,
            kafka_consumer_group,
            kafka_notification_topic,
            schema_registry,
            s3_endpoint,
            s3_bucket,
            pipeline,
            http_host,
            http_port,
            http_secret_key,
            jwt_es256_public_key_pem,
            jwt_issuer,
            jwt_audience,
            spicedb_endpoint,
            spicedb_token,
            cedar_policy_dsl,
        })
    }
}

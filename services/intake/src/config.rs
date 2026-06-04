//! Application configuration loading merging YAML and Environment variables.

use std::env;
use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;

use anyhow::{Context, Result};
use config::{Config, Environment, File, FileFormat};
use secrecy::SecretString;
use serde::Deserialize;

use crate::domain::models::PipelineConfig;

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub server: ServerConfig,
    pub kafka: KafkaConfig,
    pub s3: S3Config,
    pub jwt: JwtConfig,
    pub auth: AuthConfig,
    pub pipeline: PipelineConfig,
}

#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub host: Option<IpAddr>,
    pub port: u16,
}

impl ServerConfig {
    pub fn bind_addr(&self) -> Option<SocketAddr> {
        self.host.map(|h| SocketAddr::new(h, self.port))
    }
}

#[derive(Debug, Clone)]
pub struct KafkaConfig {
    pub brokers: String,
    pub consumer_group: String,
    pub schema_registry: String,
}

#[derive(Debug, Clone)]
pub struct S3Config {
    pub endpoint: String,
    pub bucket: String,
    pub bucket_notifications: String,
}

#[derive(Debug, Clone)]
pub struct JwtConfig {
    pub issuer: Option<String>,
    pub audience: Option<String>,
    pub es256_public_key_pem: Option<String>,
}

#[derive(Debug, Clone)]
pub struct AuthConfig {
    pub spicedb_endpoint: Option<String>,
    pub spicedb_token: Option<SecretString>,
}

#[derive(Debug, Clone, Deserialize, Default)]
struct RawConfig {
    #[serde(default)]
    intake: Option<RawIntake>,
    #[serde(default)]
    jwt: Option<RawJwt>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawIntake {
    host: Option<String>,
    port: Option<u16>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawJwt {
    issuer: Option<String>,
    audience: Option<String>,
}

impl AppConfig {
    pub fn load() -> Result<Self> {
        let pipeline_path = pipeline_path_from_env_or_default()?;

        let builder = Config::builder()
            .add_source(
                File::from(pipeline_path.as_path()).format(FileFormat::Yaml),
            )
            .add_source(
                Environment::with_prefix("INTAKE")
                    .separator("_")
                    .try_parsing(true),
            )
            .add_source(
                Environment::with_prefix("KAFKA")
                    .separator("_")
                    .try_parsing(true),
            )
            .add_source(
                Environment::with_prefix("S3")
                    .separator("_")
                    .try_parsing(true),
            )
            .add_source(
                Environment::with_prefix("SPICEDB")
                    .separator("_")
                    .try_parsing(true),
            )
            .add_source(
                Environment::with_prefix("JWT")
                    .separator("_")
                    .try_parsing(true),
            )
            .add_source(Environment::default().try_parsing(true));

        let built_config =
            builder.build().context("Failed to build config-rs layer")?;

        let pipeline: PipelineConfig =
            built_config.clone().try_deserialize().context(
                "Failed to deserialize PipelineConfig from structure layout",
            )?;

        let raw: RawConfig = built_config
            .try_deserialize()
            .context("Failed to deserialize runtime config options")?;

        Self::from_raw(raw, pipeline)
    }

    fn from_raw(r: RawConfig, pipeline: PipelineConfig) -> Result<Self> {
        // --- 1. Intake Server Configuration ---
        let env_host = env::var("INTAKE_HOST").ok();
        let is_intake_enabled = r.intake.is_some() || env_host.is_some();

        let (server_host, server_port) = if is_intake_enabled {
            let host_str = r
                .intake
                .as_ref()
                .and_then(|i| i.host.clone())
                .or(env_host)
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "0.0.0.0".to_string());

            let parsed_host =
                host_str.parse::<IpAddr>().with_context(|| {
                    format!("Invalid Intake host IP address: {host_str}")
                })?;

            let port = r
                .intake
                .as_ref()
                .and_then(|i| i.port)
                .or_else(|| {
                    env::var("INTAKE_PORT")
                        .ok()
                        .and_then(|p| p.parse::<u16>().ok())
                })
                .unwrap_or(8080);

            (Some(parsed_host), port)
        } else {
            (None, 8080)
        };

        // --- 2. Kafka Configuration ---
        let kafka_ctx = pipeline
            .connectors
            .kafka
            .as_ref()
            .context("Missing connectors.kafka specification block")?;

        let kafka_brokers = if !kafka_ctx.brokers.is_empty() {
            kafka_ctx.brokers.join(",")
        } else {
            env::var("KAFKA_BROKERS")
                .context("Missing KAFKA_BROKERS environment value")?
        };

        let schema_registry = if !kafka_ctx.schema_registry.is_empty() {
            kafka_ctx.schema_registry.clone()
        } else {
            env::var("SCHEMA_REGISTRY")
                .context("Missing SCHEMA_REGISTRY environment value")?
        };

        let kafka_consumer_group = if !kafka_ctx.consumer_group.is_empty() {
            kafka_ctx.consumer_group.clone()
        } else {
            env::var("KAFKA_CONSUMER_GROUP")
                .unwrap_or_else(|_| "intake-service".to_string())
        };

        let s3_ctx = pipeline
            .connectors
            .s3
            .as_ref()
            .context("Missing connectors.s3 specification block")?;

        let s3_endpoint = if !s3_ctx.endpoint.is_empty() {
            s3_ctx.endpoint.clone()
        } else {
            env::var("S3_ENDPOINT")
                .context("Missing S3_ENDPOINT environment value")?
        };

        let s3_bucket = if !s3_ctx.bucket.is_empty() {
            s3_ctx.bucket.clone()
        } else {
            env::var("S3_BUCKET")
                .context("Missing S3_BUCKET environment value")?
        };

        let bucket_notifications = s3_ctx
            .bucket_notifications
            .clone()
            .or_else(|| env::var("KAFKA_TOPIC_NOTIFICATIONS").ok())
            .context(
                "Missing bucket_notifications inside connectors.s3 workspace",
            )?;

        let mut spicedb_endpoint = env::var("SPICEDB_ENDPOINT").ok();
        let mut spicedb_token = env::var("SPICEDB_TOKEN")
            .ok()
            .map(|t| SecretString::new(t.into()));

        let raw_yaml_val: serde_json::Value = Config::builder()
            .add_source(
                File::from(pipeline_path_from_env_or_default()?.as_path())
                    .format(FileFormat::Yaml),
            )
            .build()?
            .try_deserialize()?;

        if let Some(spicedb_block) = raw_yaml_val
            .get("connectors")
            .and_then(|c| c.get("spicedb"))
        {
            if spicedb_endpoint.is_none() {
                spicedb_endpoint = spicedb_block
                    .get("endpoint")
                    .and_then(|e| e.as_str())
                    .map(|s| s.to_string());
            }
            if spicedb_token.is_none() {
                spicedb_token = spicedb_block
                    .get("token")
                    .and_then(|t| t.as_str())
                    .map(|s| SecretString::new(s.to_string().into()));
            }
        }
        let spicedb_endpoint =
            spicedb_endpoint.map(|ep| normalize_spicedb_endpoint(&ep));

        let mut jwt_issuer = env::var("JWT_ISSUER").ok();
        let mut jwt_audience = env::var("JWT_AUDIENCE").ok();
        if let Some(jwt_env) = r.jwt {
            if jwt_issuer.is_none() {
                jwt_issuer = jwt_env.issuer;
            }
            if jwt_audience.is_none() {
                jwt_audience = jwt_env.audience;
            }
        }

        let jwt_es256_public_key_pem =
            env::var("JWT_ES256_PUBLIC_KEY_PEM").ok();

        if server_host.is_some() {
            jwt_es256_public_key_pem
                .as_deref()
                .filter(|s| !s.trim().is_empty())
                .context("Missing JWT_ES256_PUBLIC_KEY_PEM in environment (required when intake is active)")?;

            spicedb_endpoint
                .as_deref()
                .filter(|s| !s.trim().is_empty())
                .context("Missing SPICEDB_ENDPOINT (required when intake is active)")?;

            spicedb_token.as_ref().context(
                "Missing SPICEDB_TOKEN (required when intake is active)",
            )?;
        }

        Ok(Self {
            server: ServerConfig {
                host: server_host,
                port: server_port,
            },
            kafka: KafkaConfig {
                brokers: kafka_brokers,
                consumer_group: kafka_consumer_group,
                schema_registry,
            },
            s3: S3Config {
                endpoint: s3_endpoint,
                bucket: s3_bucket,
                bucket_notifications,
            },
            jwt: JwtConfig {
                issuer: jwt_issuer,
                audience: jwt_audience,
                es256_public_key_pem: jwt_es256_public_key_pem,
            },
            auth: AuthConfig {
                spicedb_endpoint,
                spicedb_token,
            },
            pipeline,
        })
    }
}

fn pipeline_path_from_env_or_default() -> Result<PathBuf> {
    match env::var("INTAKE_PIPELINE_PATH")
        .or_else(|_| env::var("PIPELINE_PATH"))
    {
        Ok(v) if !v.trim().is_empty() => Ok(PathBuf::from(v)),
        _ => Ok(PathBuf::from("pipeline.yaml")),
    }
}

fn normalize_spicedb_endpoint(endpoint: &str) -> String {
    let e = endpoint.trim();
    if e.starts_with("http://") || e.starts_with("https://") {
        e.to_string()
    } else {
        format!("http://{e}")
    }
}

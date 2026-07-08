//! Dynamic layer assembling explicit environment metrics and system boots.

use std::env;
use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;

use anyhow::{Context, Result};
use config::{Config, Environment, File, FileFormat};
use secrecy::SecretString;
use serde::Deserialize;

/// Consolidated engine runtime configurations.
#[derive(Debug, Clone)]
pub struct AppConfig {
    /// Inbound HTTP bindings.
    pub server: ServerConfig,
    /// Local/remote pub-sub links.
    pub kafka: KafkaConfig,
    /// Ingestion object paths.
    pub s3: S3Config,
    /// Token cryptographic parameters.
    pub jwt: JwtConfig,
    /// Permission engines links.
    pub auth: AuthConfig,
}

/// HTTP listener metrics.
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// Multi-interface bind target.
    pub host: Option<IpAddr>,
    /// Socket identification port.
    pub port: u16,
}

impl ServerConfig {
    /// Computes full endpoint mapping targets if available.
    pub fn bind_addr(&self) -> Option<SocketAddr> {
        self.host.map(|h| SocketAddr::new(h, self.port))
    }
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

/// Authorization token layout specifications.
#[derive(Debug, Clone)]
pub struct JwtConfig {
    /// Trusted signer token.
    pub issuer: Option<String>,
    /// Target entity recipient constraint.
    pub audience: Option<String>,
    /// Cryptographic verify target key.
    pub es256_public_key_pem: Option<String>,
}

/// External authorization engine endpoints.
#[derive(Debug, Clone)]
pub struct AuthConfig {
    /// Authentication validation target url.
    pub spicedb_endpoint: Option<String>,
    /// Access tokens for verification steps.
    pub spicedb_token: Option<SecretString>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawBootstrapConfig {
    gateway: Option<RawGateway>,
    jwt: Option<RawJwt>,
    connectors: RawConnectors,
}

#[derive(Debug, Clone, Deserialize)]
struct RawGateway {
    host: Option<String>,
    port: u16,
}

#[derive(Debug, Clone, Deserialize)]
struct RawJwt {
    issuer: Option<String>,
    audience: Option<String>,
    es256_public_key_pem: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawConnectors {
    kafka: RawKafkaConnector,
    s3: RawS3Connector,
    spicedb: Option<RawSpiceDbConnector>,
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

#[derive(Debug, Clone, Deserialize)]
struct RawSpiceDbConnector {
    endpoint: String,
    token: String,
}

impl AppConfig {
    /// Assembles system bounds using file profiles and system variables.
    pub fn load() -> Result<Self> {
        let bootstrap_path = env::var("INTAKE_BOOTSTRAP_PATH")
            .unwrap_or_else(|_| "bootstrap.yaml".to_string());

        let builder = Config::builder()
            .set_default("gateway.port", 8080)?
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
        let server_host = match r.gateway.as_ref().and_then(|g| g.host.clone())
        {
            Some(h) if !h.trim().is_empty() => {
                let parsed_host =
                    h.trim().parse::<IpAddr>().with_context(|| {
                        format!("Invalid Intake host IP address: {h}")
                    })?;
                Some(parsed_host)
            },
            _ => None,
        };

        let server_port = r.gateway.as_ref().map(|g| g.port).unwrap_or(8080);

        let (spicedb_endpoint, spicedb_token) = match r.connectors.spicedb {
            Some(spicedb_block) => (
                Some(normalize_spicedb_endpoint(&spicedb_block.endpoint)),
                Some(SecretString::new(spicedb_block.token.into())),
            ),
            None => (None, None),
        };

        Ok(Self {
            server: ServerConfig {
                host: server_host,
                port: server_port,
            },
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
            jwt: JwtConfig {
                issuer: r.jwt.as_ref().and_then(|j| j.issuer.clone()),
                audience: r.jwt.as_ref().and_then(|j| j.audience.clone()),
                es256_public_key_pem: r
                    .jwt
                    .as_ref()
                    .and_then(|j| j.es256_public_key_pem.clone()),
            },
            auth: AuthConfig {
                spicedb_endpoint,
                spicedb_token,
            },
        })
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_spicedb_endpoint() {
        assert_eq!(
            normalize_spicedb_endpoint("127.0.0.1:50051"),
            "http://127.0.0.1:50051"
        );
        assert_eq!(
            normalize_spicedb_endpoint("http://localhost:50051"),
            "http://localhost:50051"
        );
    }
}

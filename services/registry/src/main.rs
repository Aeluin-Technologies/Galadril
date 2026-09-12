//! Registry gRPC service backed exclusively by lakeFS-managed S3 storage.

use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::{Context, Result};
use galadril_registry::domain::Registry;
use galadril_registry::grpc::RegistryGrpc;
use galadril_registry::proto::registry_server::RegistryServer;
use galadril_registry::storage::{LakeFsConfig, LakeFsStore, S3Config};
use galadril_telemetry::{ConfigureTelemetry, TelemetryConfig};
use secrecy::SecretString;
use tonic::transport::Server;

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[derive(Debug)]
struct ServiceConfig {
    bind: SocketAddr,
    lakefs: LakeFsConfig,
    s3: S3Config,
}

impl ServiceConfig {
    /// Loads service-only credentials without exposing storage configuration
    /// to callers.
    fn from_environment() -> Result<Self> {
        Self::from_lookup(|name| std::env::var(name).ok())
    }

    /// Parses an injected lookup so configuration failures remain
    /// deterministic in tests.
    fn from_lookup(
        mut lookup: impl FnMut(&str) -> Option<String>,
    ) -> Result<Self> {
        let bind = lookup("REGISTRY_BIND_ADDR")
            .unwrap_or_else(|| "0.0.0.0:50052".to_owned())
            .parse()
            .context("REGISTRY_BIND_ADDR must be a socket address")?;
        let endpoint = lookup("LAKEFS_ENDPOINT")
            .unwrap_or_else(|| "http://lakefs:8000".to_owned());
        let access_key = lookup("LAKEFS_ACCESS_KEY_ID")
            .context("LAKEFS_ACCESS_KEY_ID is required")?;
        let secret_key = lookup("LAKEFS_SECRET_ACCESS_KEY")
            .context("LAKEFS_SECRET_ACCESS_KEY is required")?;
        let storage_namespace = lookup("REGISTRY_STORAGE_NAMESPACE")
            .context("REGISTRY_STORAGE_NAMESPACE is required")?;
        let repository_prefix = lookup("REGISTRY_REPOSITORY_PREFIX")
            .unwrap_or_else(|| "galadril".to_owned());
        let s3_endpoint = lookup("REGISTRY_S3_ENDPOINT")
            .context("REGISTRY_S3_ENDPOINT is required")?;
        let s3_access_key = lookup("REGISTRY_S3_ACCESS_KEY_ID")
            .context("REGISTRY_S3_ACCESS_KEY_ID is required")?;
        let s3_secret_key = lookup("REGISTRY_S3_SECRET_ACCESS_KEY")
            .context("REGISTRY_S3_SECRET_ACCESS_KEY is required")?;
        let s3_region = lookup("REGISTRY_S3_REGION")
            .unwrap_or_else(|| "us-east-1".to_owned());
        Ok(Self {
            bind,
            lakefs: LakeFsConfig {
                endpoint,
                access_key,
                secret_key: SecretString::from(secret_key),
                repository_prefix,
                storage_namespace,
                branch: "main".to_owned(),
            },
            s3: S3Config {
                endpoint: s3_endpoint,
                region: s3_region,
                access_key: s3_access_key,
                secret_key: SecretString::from(s3_secret_key),
            },
        })
    }
}

/// Starts the sole ontology and pipeline persistence service.
#[tokio::main]
async fn main() -> Result<()> {
    let telemetry = TelemetryConfig::Binary {
        name: "galadril-registry",
        version: env!("CARGO_PKG_VERSION"),
    }
    .configure()?;
    let result = async {
        let config = ServiceConfig::from_environment()?;
        let store = LakeFsStore::new(config.lakefs, config.s3)?;
        let registry = Arc::new(Registry::new(store));
        let service = RegistryGrpc::new(registry);
        let (health_reporter, health_service) =
            tonic_health::server::health_reporter();
        health_reporter
            .set_serving::<RegistryServer<RegistryGrpc<LakeFsStore>>>()
            .await;
        tracing::info!(bind = %config.bind, "registry_grpc_listening");
        Server::builder()
            .add_service(health_service)
            .add_service(RegistryServer::new(service))
            .serve(config.bind)
            .await
            .context("Registry gRPC server failed")
    }
    .await;
    telemetry.shutdown()?;
    result
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    #[test]
    fn configuration_requires_service_owned_lakefs_credentials() -> Result<()>
    {
        let values = HashMap::from([
            ("LAKEFS_ACCESS_KEY_ID", "access"),
            ("REGISTRY_STORAGE_NAMESPACE", "s3://lake/registry/"),
            ("REGISTRY_S3_ENDPOINT", "http://minio:9000"),
            ("REGISTRY_S3_ACCESS_KEY_ID", "minio"),
            ("REGISTRY_S3_SECRET_ACCESS_KEY", "secret"),
        ]);
        let Err(error) = ServiceConfig::from_lookup(|name| {
            values.get(name).map(|value| (*value).to_owned())
        }) else {
            anyhow::bail!("missing secret accepted");
        };
        assert!(error.to_string().contains("LAKEFS_SECRET_ACCESS_KEY"));
        Ok(())
    }

    #[test]
    fn configuration_requires_service_owned_s3_purge_credentials() -> Result<()>
    {
        let values = HashMap::from([
            ("LAKEFS_ACCESS_KEY_ID", "access"),
            ("LAKEFS_SECRET_ACCESS_KEY", "secret"),
            ("REGISTRY_STORAGE_NAMESPACE", "s3://lake/registry/"),
            ("REGISTRY_S3_ENDPOINT", "http://minio:9000"),
            ("REGISTRY_S3_ACCESS_KEY_ID", "minio"),
        ]);
        let Err(error) = ServiceConfig::from_lookup(|name| {
            values.get(name).map(|value| (*value).to_owned())
        }) else {
            anyhow::bail!("missing S3 secret accepted");
        };
        assert!(error.to_string().contains("REGISTRY_S3_SECRET_ACCESS_KEY"));
        Ok(())
    }

    #[test]
    fn configuration_parses_a_complete_service_boundary() -> Result<()> {
        let values = HashMap::from([
            ("REGISTRY_BIND_ADDR", "127.0.0.1:51000"),
            ("LAKEFS_ENDPOINT", "http://storage-control:8000"),
            ("LAKEFS_ACCESS_KEY_ID", "access"),
            ("LAKEFS_SECRET_ACCESS_KEY", "secret"),
            ("REGISTRY_STORAGE_NAMESPACE", "s3://lake/registry/"),
            ("REGISTRY_REPOSITORY_PREFIX", "tenant-registry"),
            ("REGISTRY_S3_ENDPOINT", "http://minio:9000"),
            ("REGISTRY_S3_ACCESS_KEY_ID", "minio"),
            ("REGISTRY_S3_SECRET_ACCESS_KEY", "secret"),
            ("REGISTRY_S3_REGION", "us-east-1"),
        ]);
        let config = ServiceConfig::from_lookup(|name| {
            values.get(name).map(|value| (*value).to_owned())
        })?;
        assert_eq!(config.bind, "127.0.0.1:51000".parse()?);
        assert_eq!(config.lakefs.endpoint, "http://storage-control:8000");
        assert_eq!(config.lakefs.repository_prefix, "tenant-registry");
        assert_eq!(config.s3.endpoint, "http://minio:9000");
        Ok(())
    }
}

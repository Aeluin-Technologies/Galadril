//! Registry gRPC service backed exclusively by lakeFS-managed S3 storage.

use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use config::{Config, File, FileFormat};
use galadril_registry::domain::Registry;
use galadril_registry::grpc::RegistryGrpc;
use galadril_registry::proto::registry_server::RegistryServer;
use galadril_registry::storage::{LakeFsConfig, LakeFsStore, S3Config};
use galadril_telemetry::{ConfigureTelemetry, TelemetryConfig};
use secrecy::SecretString;
use serde::Deserialize;
use tonic::transport::Server;

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[derive(Debug)]
struct ServiceConfig {
    bind: SocketAddr,
    lakefs: LakeFsConfig,
    s3: S3Config,
}

#[derive(Deserialize)]
struct BootstrapConfig {
    connectors: BootstrapConnectors,
}

#[derive(Deserialize)]
struct BootstrapConnectors {
    s3: BootstrapS3,
}

#[derive(Deserialize)]
struct BootstrapS3 {
    endpoint: String,
    access_key: String,
    secret_key: String,
    region: String,
    bucket: String,
}

impl ServiceConfig {
    /// Resolves only connector paths provisioned by supported deployments.
    fn config_path(configured: Option<&str>) -> Result<&'static str> {
        match configured {
            None | Some("examples/connectors.yaml") => {
                Ok("examples/connectors.yaml")
            },
            Some("/connectors.yaml") => Ok("/connectors.yaml"),
            Some("/etc/galadril/connectors.yaml") => {
                Ok("/etc/galadril/connectors.yaml")
            },
            Some(_) => anyhow::bail!(
                "REGISTRY_CONFIG_PATH must select a supported connector file"
            ),
        }
    }

    /// Loads the shared S3 connector and service-owned lakeFS settings.
    fn from_environment() -> Result<Self> {
        let configured = std::env::var("REGISTRY_CONFIG_PATH").ok();
        let path = Self::config_path(configured.as_deref())?;
        let yaml = std::fs::read_to_string(path)
            .with_context(|| format!("Registry config read failed: {path}"))?;
        Self::from_yaml_and_lookup(&yaml, |name| std::env::var(name).ok())
    }

    /// Parses shared S3 settings and injected service-only overrides.
    fn from_yaml_and_lookup(
        yaml: &str,
        mut lookup: impl FnMut(&str) -> Option<String>,
    ) -> Result<Self> {
        let bootstrap: BootstrapConfig = Config::builder()
            .add_source(File::from_str(yaml, FileFormat::Yaml))
            .build()?
            .try_deserialize()
            .context("connectors.s3 is required")?;
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
        let s3 = bootstrap.connectors.s3;
        ensure!(
            storage_namespace == format!("s3://{}/", s3.bucket),
            "Registry storage namespace must be the connectors.s3 bucket root"
        );
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
                endpoint: s3.endpoint,
                region: s3.region,
                access_key: s3.access_key,
                secret_key: SecretString::from(s3.secret_key),
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

    use secrecy::ExposeSecret;

    use super::*;

    const YAML: &str = r#"connectors:
  s3:
    endpoint: http://minio:9000
    access_key: minio
    secret_key: secret
    region: us-east-1
    bucket: lake
"#;

    #[test]
    fn configuration_path_accepts_only_deployment_contracts() -> Result<()> {
        assert_eq!(
            ServiceConfig::config_path(None)?,
            "examples/connectors.yaml"
        );
        assert_eq!(
            ServiceConfig::config_path(Some("/connectors.yaml"))?,
            "/connectors.yaml"
        );
        assert_eq!(
            ServiceConfig::config_path(Some("/etc/galadril/connectors.yaml"))?,
            "/etc/galadril/connectors.yaml"
        );
        assert!(ServiceConfig::config_path(Some("../../secret")).is_err());
        assert!(ServiceConfig::config_path(Some("/tmp/injected")).is_err());
        Ok(())
    }

    #[test]
    fn configuration_requires_service_owned_lakefs_credentials() -> Result<()>
    {
        let values = HashMap::from([
            ("LAKEFS_ACCESS_KEY_ID", "access"),
            ("REGISTRY_STORAGE_NAMESPACE", "s3://lake/"),
        ]);
        let Err(error) = ServiceConfig::from_yaml_and_lookup(YAML, |name| {
            values.get(name).map(|value| (*value).to_owned())
        }) else {
            anyhow::bail!("missing secret accepted");
        };
        assert!(error.to_string().contains("LAKEFS_SECRET_ACCESS_KEY"));
        Ok(())
    }

    #[test]
    fn configuration_requires_shared_s3_credentials() -> Result<()> {
        let values = HashMap::from([
            ("LAKEFS_ACCESS_KEY_ID", "access"),
            ("LAKEFS_SECRET_ACCESS_KEY", "secret"),
            ("REGISTRY_STORAGE_NAMESPACE", "s3://lake/"),
        ]);
        let Err(error) =
            ServiceConfig::from_yaml_and_lookup("connectors: {}", |name| {
                values.get(name).map(|value| (*value).to_owned())
            })
        else {
            anyhow::bail!("missing S3 connector accepted");
        };
        assert!(error.to_string().contains("connectors.s3"));
        Ok(())
    }

    #[test]
    fn configuration_parses_a_complete_service_boundary() -> Result<()> {
        let values = HashMap::from([
            ("REGISTRY_BIND_ADDR", "127.0.0.1:51000"),
            ("LAKEFS_ENDPOINT", "http://storage-control:8000"),
            ("LAKEFS_ACCESS_KEY_ID", "access"),
            ("LAKEFS_SECRET_ACCESS_KEY", "secret"),
            ("REGISTRY_STORAGE_NAMESPACE", "s3://lake/"),
            ("REGISTRY_REPOSITORY_PREFIX", "tenant-registry"),
        ]);
        let config = ServiceConfig::from_yaml_and_lookup(YAML, |name| {
            values.get(name).map(|value| (*value).to_owned())
        })?;
        assert_eq!(config.bind, "127.0.0.1:51000".parse()?);
        assert_eq!(config.lakefs.endpoint, "http://storage-control:8000");
        assert_eq!(config.lakefs.repository_prefix, "tenant-registry");
        assert_eq!(config.s3.endpoint, "http://minio:9000");
        Ok(())
    }

    #[test]
    fn configuration_reads_shared_s3_credentials_from_connectors_yaml()
    -> Result<()> {
        let yaml = r#"
registry:
  endpoint: http://registry:50052
connectors:
  s3:
    endpoint: http://minio:9000
    access_key: shared-access
    secret_key: shared-secret
    region: eu-west-1
    bucket: lake
"#;
        let config =
            ServiceConfig::from_yaml_and_lookup(yaml, |name| match name {
                "LAKEFS_ACCESS_KEY_ID" => Some("lakefs-access".to_owned()),
                "LAKEFS_SECRET_ACCESS_KEY" => Some("lakefs-secret".to_owned()),
                "REGISTRY_STORAGE_NAMESPACE" => Some("s3://lake/".to_owned()),
                _ => None,
            })?;
        assert_eq!(config.s3.endpoint, "http://minio:9000");
        assert_eq!(config.s3.region, "eu-west-1");
        assert_eq!(config.s3.access_key, "shared-access");
        assert_eq!(config.s3.secret_key.expose_secret(), "shared-secret");
        Ok(())
    }

    #[test]
    fn configuration_requires_tenant_partitions_at_the_bucket_root()
    -> Result<()> {
        let values = HashMap::from([
            ("LAKEFS_ACCESS_KEY_ID", "access"),
            ("LAKEFS_SECRET_ACCESS_KEY", "secret"),
            ("REGISTRY_STORAGE_NAMESPACE", "s3://lake/registry/"),
        ]);
        let Err(error) = ServiceConfig::from_yaml_and_lookup(YAML, |name| {
            values.get(name).map(|value| (*value).to_owned())
        }) else {
            anyhow::bail!("nested Registry namespace accepted");
        };
        assert!(error.to_string().contains("bucket root"));
        Ok(())
    }
}

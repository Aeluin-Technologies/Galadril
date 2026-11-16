//! Registry gRPC service backed exclusively by lakeFS-managed S3 storage.

use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use config::{Config, File, FileFormat};
use galadril_registry::domain::Registry;
use galadril_registry::gol::{GolAuthorityConfig, registered_external_types};
use galadril_registry::gol_service::GolPublicationConfig;
use galadril_registry::grpc::RegistryGrpc;
use galadril_registry::proto::registry_server::RegistryServer;
use galadril_registry::storage::{LakeFsConfig, LakeFsStore, S3Config};
use galadril_telemetry::{ConfigureTelemetry, TelemetryConfig};
use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;
use tonic::transport::Server;

mod metadata;

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[derive(Debug)]
struct ServiceConfig {
    bind: SocketAddr,
    lakefs: LakeFsConfig,
    s3: S3Config,
    gol: Option<GolServiceConfig>,
}

#[derive(Debug)]
struct GolServiceConfig {
    artifact_bucket: String,
    authority_path: String,
    spicedb_endpoint: String,
    spicedb_token: SecretString,
}

#[derive(Deserialize)]
struct BootstrapConfig {
    connectors: BootstrapConnectors,
}

#[derive(Deserialize)]
struct BootstrapConnectors {
    s3: BootstrapS3,
    spicedb: Option<BootstrapSpiceDb>,
}

#[derive(Deserialize)]
struct BootstrapSpiceDb {
    endpoint: String,
    token: SecretString,
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
            None => Ok("/etc/galadril/connectors.yaml"),
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
        let bind: SocketAddr = lookup("REGISTRY_BIND_ADDR")
            .unwrap_or_else(|| "127.0.0.1:50053".to_owned())
            .parse()
            .context("REGISTRY_BIND_ADDR must be a socket address")?;
        let ambient = match lookup("REGISTRY_PROXY_MODE").as_deref() {
            None | Some("sidecar") => false,
            Some("ambient") => true,
            Some(_) => {
                anyhow::bail!("REGISTRY_PROXY_MODE must be sidecar or ambient")
            },
        };
        ensure!(
            bind.ip().is_loopback() || ambient,
            "REGISTRY_BIND_ADDR must be loopback behind Envoy"
        );
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
        let spicedb = bootstrap.connectors.spicedb;
        let s3 = bootstrap.connectors.s3;
        ensure!(
            storage_namespace == format!("s3://{}/", s3.bucket),
            "Registry storage namespace must be the connectors.s3 bucket root"
        );
        let gol = match (
            lookup("GOL_ARTIFACT_BUCKET"),
            lookup("GOL_AUTHORITY_CONFIG_PATH"),
        ) {
            (None, None) => None,
            (Some(bucket), Some(path)) => {
                ensure!(
                    bucket != s3.bucket,
                    "GOL artifact bucket must differ from the lakeFS source bucket"
                );
                ensure!(
                    (3..=63).contains(&bucket.len()) &&
                        bucket
                            .bytes()
                            .all(|byte| byte.is_ascii_lowercase() ||
                                byte.is_ascii_digit() ||
                                matches!(byte, b'-' | b'.')),
                    "invalid GOL artifact bucket"
                );
                ensure!(
                    matches!(
                        path.as_str(),
                        "/etc/galadril/gol-authorities.json" |
                            "/etc/gol/gol-authorities.json" |
                            "/gol-authorities.json"
                    ),
                    "GOL authority path must select a supported backend configuration"
                );
                Some(GolServiceConfig {
                    artifact_bucket: bucket,
                    authority_path: path,
                    spicedb_endpoint:lookup("SPICEDB_ENDPOINT").or_else(||spicedb.as_ref().map(|config|config.endpoint.clone())).context("approved metadata authority endpoint is required")?,
                    spicedb_token:lookup("SPICEDB_TOKEN").map(SecretString::from).or_else(||spicedb.map(|config|config.token)).context("approved metadata authority credentials are required")?,
                })
            },
            _ => anyhow::bail!(
                "GOL_ARTIFACT_BUCKET and GOL_AUTHORITY_CONFIG_PATH must be configured together"
            ),
        };
        Ok(Self {
            bind,
            gol,
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
        let artifact_client = store.artifact_client();
        let mut registry = Registry::new(store);
        if let Some(gol) = config.gol {
            let bytes = std::fs::read(&gol.authority_path)
                .context("approved GOL authority configuration unavailable")?;
            ensure!(
                bytes.len() <= 1024 * 1024,
                "approved GOL authority configuration exceeds byte budget"
            );
            let authority: GolAuthorityConfig = serde_json::from_slice(&bytes)
                .context("approved GOL authority configuration invalid")?;
            let metadata = metadata::SpiceDbMetadata::connect(
                &gol.spicedb_endpoint,
                gol.spicedb_token.expose_secret(),
            )
            .await?;
            registry = registry.with_gol(GolPublicationConfig::new(
                artifact_client,
                gol.artifact_bucket,
                authority,
                registered_external_types()?,
                Arc::new(metadata),
            )?);
        } else {
            tracing::warn!(
                event.name = "registry.gol.publication_disabled",
                reason = "approved_authority_configuration_absent"
            );
        }
        let registry = Arc::new(registry);
        let service = RegistryGrpc::new(registry);
        let service = if let Some(path) =
            std::env::var_os("REGISTRY_GATEWAY_KEY_PATH")
        {
            service.with_gateway_key(
                &galadril_registry::grpc::read_gateway_key(
                    std::path::PathBuf::from(path),
                )?,
            )?
        } else {
            tracing::warn!(
                event.name = "registry.gol.source_channel_disabled",
                reason = "gateway_credential_absent"
            );
            service
        };
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

    #[test]
    fn gol_publication_requires_a_separate_bucket_and_explicit_authority() {
        let configured =
            ServiceConfig::from_yaml_and_lookup(YAML, |name| match name {
                "LAKEFS_ACCESS_KEY_ID" => Some("access".into()),
                "LAKEFS_SECRET_ACCESS_KEY" => Some("secret".into()),
                "REGISTRY_STORAGE_NAMESPACE" => Some("s3://lake/".into()),
                "GOL_ARTIFACT_BUCKET" => Some("lake".into()),
                "GOL_AUTHORITY_CONFIG_PATH" => {
                    Some("/etc/galadril/gol-authorities.json".into())
                },
                _ => None,
            });
        assert!(configured.is_err());
    }

    #[test]
    fn ambient_registry_binding_is_explicit_and_fail_closed() {
        for (mode, accepted) in
            [("ambient", true), ("sidecar", false), ("shared", false)]
        {
            let configured =
                ServiceConfig::from_yaml_and_lookup(YAML, |name| match name {
                    "REGISTRY_BIND_ADDR" => Some("0.0.0.0:50053".to_owned()),
                    "REGISTRY_PROXY_MODE" => Some(mode.to_owned()),
                    "LAKEFS_ACCESS_KEY_ID" => Some("access".to_owned()),
                    "LAKEFS_SECRET_ACCESS_KEY" => Some("secret".to_owned()),
                    "REGISTRY_STORAGE_NAMESPACE" => {
                        Some("s3://lake/".to_owned())
                    },
                    _ => None,
                });
            assert_eq!(configured.is_ok(), accepted);
        }
    }

    #[test]
    fn registry_listener_cannot_bypass_the_proxy() -> Result<()> {
        for bind in ["127.0.0.1:50053", "0.0.0.0:50053"] {
            let configured =
                ServiceConfig::from_yaml_and_lookup(YAML, |name| match name {
                    "REGISTRY_BIND_ADDR" => Some(bind.to_owned()),
                    "LAKEFS_ACCESS_KEY_ID" => Some("access".to_owned()),
                    "LAKEFS_SECRET_ACCESS_KEY" => Some("secret".to_owned()),
                    "REGISTRY_STORAGE_NAMESPACE" => {
                        Some("s3://lake/".to_owned())
                    },
                    _ => None,
                });
            assert_eq!(configured.is_ok(), bind.starts_with("127."));
        }
        Ok(())
    }

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
            "/etc/galadril/connectors.yaml"
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

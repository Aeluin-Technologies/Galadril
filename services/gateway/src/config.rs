//! Application configuration loading.

use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;

use anyhow::{Context, Result};
use config::{Config, Environment, File, FileFormat};
use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub registry: Option<galadril_registry::grpc::RegistryClientConfig>,
    pub server: ServerConfig,
    pub database: DatabaseConfig,
    pub jwt: JwtConfig,
    pub auth: AuthConfig,
    pub s3: Option<S3Config>,
    pub scribe: ScribeRuntimeConfig,
}

#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// Bind host for the HTTP server.
    pub host: IpAddr,
    /// Bind port for the HTTP server.
    pub port: u16,
    /// Maximum accepted GraphQL HTTP body size.
    pub max_body_bytes: usize,
    /// Maximum expanded GraphQL selection depth.
    pub max_graphql_depth: usize,
}

impl ServerConfig {
    /// Returns the socket address used for binding.
    pub fn bind_addr(&self) -> SocketAddr {
        SocketAddr::new(self.host, self.port)
    }
}

#[derive(Debug, Clone)]
pub struct AuthConfig {
    /// SpiceDB/Authzed endpoint, e.g. "http://127.0.0.1:50051".
    pub spicedb_endpoint: Option<String>,
    /// SpiceDB/Authzed token (secret).
    pub spicedb_token: Option<SecretString>,
    /// Optional Cedar policies path (stringly-typed because loth expects a
    /// path-like string).
    #[expect(dead_code, reason = "retained for Loth policy compatibility")]
    pub cedar_policy_dsl: String,
}

#[derive(Debug, Clone)]
pub struct DatabaseConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    pub username: String,
    pub password: Option<SecretString>,
    /// Optional full DSN. If set, it wins over
    /// host/port/name/username/password.
    pub url: Option<String>,
    /// Apache AGE graph shared with pipeline workers.
    pub graph_name: String,
}

#[derive(Debug, Clone)]
pub struct JwtConfig {
    pub issuer: Option<String>,
    pub audience: Option<String>,
    pub es256_public_key_pem: Option<String>,
    pub es256_private_key_pem: Option<SecretString>,
}

#[derive(Debug, Clone)]
pub struct S3Config {
    pub endpoint: String,
    pub region: String,
    pub bucket: String,
    #[expect(dead_code, reason = "consumed by the notification deployment")]
    pub bucket_notifications: String,
    pub staging_bucket: String,
    pub access_key: String,
    pub secret_key: String,
}

#[derive(Debug, Clone)]
pub struct ScribeRuntimeConfig {
    pub enabled: bool,
}

/// The unified internal structural layout that matches both `connectors.yaml`
/// shapes AND incoming flat or nested environment overrides.
#[derive(Debug, Clone, Deserialize, Default)]
struct RawConfig {
    #[serde(default)]
    registry: Option<galadril_registry::grpc::RegistryClientConfig>,
    #[serde(default)]
    gateway: Option<RawGateway>,
    #[serde(default)]
    connectors: RawConnectors,
    #[serde(default)]
    database: Option<RawDatabaseOverrides>,
    #[serde(default)]
    spicedb: Option<RawSpiceDbOverrides>,
    #[serde(default)]
    jwt: Option<RawJwt>,
    #[serde(default)]
    public_key_pem: Option<String>,
    #[serde(default)]
    private_key_pem: Option<SecretString>,
    #[serde(default)]
    scribe: Option<RawScribe>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawGateway {
    #[serde(default)]
    host: Option<String>,
    #[serde(default)]
    port: Option<u16>,
    #[serde(default)]
    max_body_bytes: Option<usize>,
    #[serde(default)]
    max_graphql_depth: Option<usize>,
}

#[derive(Debug, Clone, Deserialize, Default)]
struct RawConnectors {
    #[serde(default)]
    postgres: Option<RawPostgres>,
    #[serde(default)]
    spicedb: Option<RawSpiceDb>,
    #[serde(default)]
    s3: Option<RawS3>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawPostgres {
    host: String,
    #[serde(default)]
    database: Option<String>,
    #[serde(default)]
    user: Option<String>,
    #[serde(default)]
    password: Option<String>,
    #[serde(default)]
    graph_name: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawSpiceDb {
    endpoint: String,
    #[serde(default)]
    token: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawS3 {
    endpoint: String,
    region: String,
    bucket: String,
    bucket_notifications: String,
    staging_bucket: String,
    access_key: String,
    secret_key: String,
}

// Structs dedicated to capturing flat ecosystem env overrides cleanly
#[derive(Debug, Clone, Deserialize)]
struct RawDatabaseOverrides {
    username: Option<String>,
    password: Option<SecretString>,
    url: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawSpiceDbOverrides {
    endpoint: Option<String>,
    token: Option<SecretString>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawJwt {
    issuer: Option<String>,
    audience: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct RawScribe {
    #[serde(default)]
    enabled: Option<bool>,
}

impl AppConfig {
    /// Loads configuration from `connectors.yaml` (with environment
    /// overrides).
    pub fn load() -> Result<Self> {
        let pipeline_path = pipeline_path_from_env_or_default()?;

        let builder = Config::builder()
            .add_source(
                File::from(pipeline_path.as_path()).format(FileFormat::Yaml),
            )
            .add_source(
                Environment::with_prefix("GATEWAY")
                    .separator("_")
                    .try_parsing(true),
            )
            .add_source(
                Environment::with_prefix("DATABASE")
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
            .add_source(
                Environment::with_prefix("S3")
                    .separator("_")
                    .try_parsing(true),
            )
            .add_source(Environment::default().try_parsing(true));

        let raw: RawConfig = builder
            .build()
            .context("Failed to build config-rs manager layer")?
            .try_deserialize()
            .context(
                "Failed to deserialize merged configurations into schema",
            )?;

        Self::from_raw(raw)
            .context("Failed to sanitize and build final AppConfig")
    }

    /// Builds a SQLx-compatible Postgres connection string.
    pub fn database_url(&self) -> Result<String> {
        if let Some(url) = &self.database.url {
            return Ok(url.clone());
        }

        let user = &self.database.username;
        let host = &self.database.host;
        let port = self.database.port;
        let db = &self.database.name;

        let url = if let Some(pw) = &self.database.password {
            format!(
                "postgres://{}:{}@{}:{}/{}",
                urlencoding::encode(user),
                urlencoding::encode(pw.expose_secret()),
                host,
                port,
                db
            )
        } else {
            format!(
                "postgres://{}@{}:{}/{}",
                urlencoding::encode(user),
                host,
                port,
                db
            )
        };

        Ok(url)
    }

    /// Validates raw environment values and builds runtime configuration.
    fn from_raw(r: RawConfig) -> Result<Self> {
        let (server_host, server_port, max_body_bytes, max_graphql_depth) = {
            let host_str = r
                .gateway
                .as_ref()
                .and_then(|g| g.host.as_deref())
                .unwrap_or("0.0.0.0");
            let port = r.gateway.as_ref().and_then(|g| g.port).unwrap_or(8080);
            let max_body_bytes = r
                .gateway
                .as_ref()
                .and_then(|g| g.max_body_bytes)
                .unwrap_or(1_048_576);
            let max_graphql_depth = r
                .gateway
                .as_ref()
                .and_then(|g| g.max_graphql_depth)
                .unwrap_or(16);

            let host = host_str.parse::<IpAddr>().with_context(|| {
                format!("Invalid gateway.host IP address: {host_str}")
            })?;

            anyhow::ensure!(
                max_body_bytes > 0,
                "gateway.max_body_bytes must be positive"
            );
            anyhow::ensure!(
                max_graphql_depth > 0,
                "gateway.max_graphql_depth must be positive"
            );

            (host, port, max_body_bytes, max_graphql_depth)
        };

        let (
            db_host,
            db_port,
            db_name,
            mut db_user,
            mut db_password,
            graph_name,
        ) = match r.connectors.postgres {
            Some(pg) => {
                let (h, port) =
                    split_host_port(&pg.host, 5432).with_context(|| {
                        format!(
                            "Invalid connectors.postgres.host: {}",
                            pg.host
                        )
                    })?;

                let name =
                    pg.database.unwrap_or_else(|| "galadril_dev".to_string());
                let user = pg.user.unwrap_or_else(|| "postgres".to_string());
                let password =
                    pg.password.map(|v| SecretString::new(v.into()));
                let graph_name =
                    pg.graph_name.unwrap_or_else(|| "galadril_dev".to_owned());

                (h, port, name, user, password, graph_name)
            },
            None => (
                "localhost".to_string(),
                5432,
                "galadril_dev".to_string(),
                "postgres".to_string(),
                None,
                "galadril_dev".to_owned(),
            ),
        };

        let mut db_url = None;
        if let Some(db_env) = r.database {
            if let Some(u) = db_env.username {
                db_user = u;
            }
            if db_env.password.is_some() {
                db_password = db_env.password;
            }
            if db_env.url.is_some() {
                db_url = db_env.url;
            }
        }

        let (mut spicedb_endpoint, mut spicedb_token) =
            match r.connectors.spicedb {
                Some(s) => (
                    Some(normalize_spicedb_endpoint(&s.endpoint)),
                    s.token.map(|v| SecretString::new(v.into())),
                ),
                None => (None, None),
            };

        if let Some(spicedb_env) = r.spicedb {
            if let Some(ep) = spicedb_env.endpoint {
                spicedb_endpoint = Some(normalize_spicedb_endpoint(&ep));
            }
            if spicedb_env.token.is_some() {
                spicedb_token = spicedb_env.token;
            }
        }

        let (mut jwt_issuer, mut jwt_audience) = (None, None);
        if let Some(jwt_env) = r.jwt {
            jwt_issuer = jwt_env.issuer;
            jwt_audience = jwt_env.audience;
        }

        let s3 = r.connectors.s3.map(|s| S3Config {
            endpoint: s.endpoint,
            bucket: s.bucket,
            region: s.region,
            bucket_notifications: s.bucket_notifications,
            staging_bucket: s.staging_bucket,
            access_key: s.access_key,
            secret_key: s.secret_key,
        });

        Ok(Self {
            registry: r.registry,
            server: ServerConfig {
                host: server_host,
                port: server_port,
                max_body_bytes,
                max_graphql_depth,
            },
            database: DatabaseConfig {
                host: db_host,
                port: db_port,
                name: db_name,
                username: db_user,
                password: db_password,
                url: db_url,
                graph_name,
            },
            jwt: JwtConfig {
                issuer: jwt_issuer,
                audience: jwt_audience,
                es256_public_key_pem: r.public_key_pem,
                es256_private_key_pem: r.private_key_pem,
            },
            auth: AuthConfig {
                spicedb_endpoint,
                spicedb_token,
                cedar_policy_dsl: "".to_string(),
            },
            s3,
            scribe: ScribeRuntimeConfig {
                enabled: r
                    .scribe
                    .and_then(|scribe| scribe.enabled)
                    .unwrap_or(true),
            },
        })
    }
}

/// Resolves the canonical pipeline file with a non-empty environment override.
fn pipeline_path_from_env_or_default() -> Result<PathBuf> {
    match std::env::var("GALADRIL_PIPELINE_PATH") {
        Ok(v) if !v.trim().is_empty() => Ok(PathBuf::from(v)),
        _ => Ok(PathBuf::from("examples/connectors.yaml")),
    }
}

/// Splits "host:port" or "host" into (String, u16).
fn split_host_port(input: &str, default_port: u16) -> Result<(String, u16)> {
    let s = input.trim();
    if s.is_empty() {
        anyhow::bail!("empty host");
    }

    if let Some((h, p)) = s.rsplit_once(':') &&
        !h.is_empty() &&
        p.chars().all(|c| c.is_ascii_digit())
    {
        let port = p
            .parse::<u16>()
            .with_context(|| format!("invalid port: {p}"))?;
        return Ok((h.to_string(), port));
    }

    Ok((s.to_string(), default_port))
}

/// Normalizes a SpiceDB endpoint without changing an explicit URI scheme.
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
    fn split_host_port_parses_host_and_port() -> anyhow::Result<()> {
        let (h, p) = split_host_port("postgres:5432", 123)?;
        assert_eq!(h, "postgres");
        assert_eq!(p, 5432);
        Ok(())
    }

    #[test]
    fn split_host_port_defaults_port() -> anyhow::Result<()> {
        let (h, p) = split_host_port("postgres", 5432)?;
        assert_eq!(h, "postgres");
        assert_eq!(p, 5432);
        Ok(())
    }

    #[test]
    fn normalize_spicedb_endpoint_adds_http_scheme() {
        assert_eq!(
            normalize_spicedb_endpoint("spicedb:50051"),
            "http://spicedb:50051"
        );
    }

    #[test]
    fn normalize_spicedb_endpoint_keeps_existing_scheme() {
        assert_eq!(
            normalize_spicedb_endpoint("http://127.0.0.1:50051"),
            "http://127.0.0.1:50051"
        );
    }

    #[test]
    fn s3_config_optional() {
        let cfg = AppConfig {
            registry: None,
            server: ServerConfig {
                host: std::net::Ipv4Addr::UNSPECIFIED.into(),
                port: 8080,
                max_body_bytes: 1_048_576,
                max_graphql_depth: 16,
            },
            database: DatabaseConfig {
                host: "localhost".to_string(),
                port: 5432,
                name: "db".to_string(),
                username: "user".to_string(),
                password: None,
                url: None,
                graph_name: "galadril_dev".to_owned(),
            },
            jwt: JwtConfig {
                issuer: None,
                audience: None,
                es256_public_key_pem: None,
                es256_private_key_pem: None,
            },
            auth: AuthConfig {
                spicedb_endpoint: None,
                spicedb_token: None,
                cedar_policy_dsl: "".to_string(),
            },
            s3: None,
            scribe: ScribeRuntimeConfig { enabled: true },
        };

        assert!(cfg.s3.is_none());
    }

    #[test]
    fn scribe_is_enabled_by_default_and_can_be_disabled() -> anyhow::Result<()>
    {
        let default = AppConfig::from_raw(RawConfig::default())?;
        assert!(default.scribe.enabled);

        let disabled = AppConfig::from_raw(RawConfig {
            scribe: Some(RawScribe {
                enabled: Some(false),
            }),
            ..RawConfig::default()
        })?;
        assert!(!disabled.scribe.enabled);
        Ok(())
    }

    #[test]
    fn graphql_limits_are_bounded_and_configurable() -> anyhow::Result<()> {
        let defaults = AppConfig::from_raw(RawConfig::default())?;
        assert_eq!(defaults.server.max_body_bytes, 1_048_576);
        assert_eq!(defaults.server.max_graphql_depth, 16);

        let configured = AppConfig::from_raw(RawConfig {
            gateway: Some(RawGateway {
                host: None,
                port: None,
                max_body_bytes: Some(4096),
                max_graphql_depth: Some(7),
            }),
            ..RawConfig::default()
        })?;
        assert_eq!(configured.server.max_body_bytes, 4096);
        assert_eq!(configured.server.max_graphql_depth, 7);
        Ok(())
    }

    #[test]
    fn postgres_graph_name_is_shared_with_pipeline_workers()
    -> anyhow::Result<()> {
        let defaults = AppConfig::from_raw(RawConfig::default())?;
        assert_eq!(defaults.database.graph_name, "galadril_dev");

        let configured = AppConfig::from_raw(RawConfig {
            connectors: RawConnectors {
                postgres: Some(RawPostgres {
                    host: "postgres:5432".to_owned(),
                    database: Some("galadril".to_owned()),
                    user: Some("gateway".to_owned()),
                    password: Some("secret".to_owned()),
                    graph_name: Some("tenant_graph".to_owned()),
                }),
                ..RawConnectors::default()
            },
            ..RawConfig::default()
        })?;

        assert_eq!(configured.database.graph_name, "tenant_graph");
        Ok(())
    }
}

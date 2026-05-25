//! Application configuration loading.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct AppConfig {
    pub server: ServerConfig,
    pub database: DatabaseConfig,
    pub jwt: JwtConfig,
    /// Authorization engine configuration (Loth / SpiceDB / Cedar).
    #[serde(default)]
    pub auth: AuthConfig,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct AuthConfig {
    /// SpiceDB/Authzed endpoint, e.g. "http://127.0.0.1:50051".
    #[serde(default)]
    pub spicedb_endpoint: Option<String>,
    /// SpiceDB/Authzed token (secret).
    #[serde(default)]
    pub spicedb_token: Option<SecretString>,
    #[serde(default)]
    pub cedar_policy_dsl: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ServerConfig {
    /// Bind port for the HTTP server.
    #[serde(default = "default_server_port")]
    pub port: u16,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DatabaseConfig {
    #[serde(default = "default_db_host")]
    pub host: String,
    #[serde(default = "default_db_port")]
    pub port: u16,
    #[serde(default = "default_db_name")]
    pub name: String,
    #[serde(default = "default_db_username")]
    pub username: String,
    #[serde(default)]
    pub password: Option<SecretString>,
    /// Optional full DSN. If set, it wins over
    /// host/port/name/username/password.
    #[serde(default)]
    pub url: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct JwtConfig {
    #[serde(default)]
    pub issuer: Option<String>,
    #[serde(default)]
    pub audience: Option<String>,
    #[serde(default)]
    pub es256_public_key_pem: Option<String>,
    #[serde(default)]
    pub es256_private_key_pem: Option<SecretString>,
}

impl AppConfig {
    /// Loads configuration.
    pub fn load() -> Result<Self> {
        let mut builder = config::Config::builder();

        if let Some(path) = config_path_from_env()? {
            builder =
                builder.add_source(config::File::from(path).required(true));
        } else if let Some(path) = discover_default_config_file()? {
            builder =
                builder.add_source(config::File::from(path).required(false));
        }

        builder = builder.add_source(
            config::Environment::with_prefix("GALADRIL")
                .separator("__")
                .try_parsing(true),
        );

        let mut cfg: Self = builder
            .build()
            .context("Failed to build configuration sources")?
            .try_deserialize()
            .context("Failed to deserialize AppConfig")?;

        apply_sensitive_env_overrides(&mut cfg);

        Ok(cfg)
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
}

fn config_path_from_env() -> Result<Option<PathBuf>> {
    match std::env::var("GALADRIL_CONFIG_PATH") {
        Ok(v) if !v.trim().is_empty() => Ok(Some(PathBuf::from(v))),
        _ => Ok(None),
    }
}

fn discover_default_config_file() -> Result<Option<PathBuf>> {
    for candidate in ["config.toml", "config.yaml", "config.yml"] {
        let p = Path::new(candidate);
        if p.exists() {
            return Ok(Some(p.to_path_buf()));
        }
    }
    Ok(None)
}

fn apply_sensitive_env_overrides(cfg: &mut AppConfig) {
    if let Ok(v) = std::env::var("DATABASE_PASSWORD") &&
        !v.trim().is_empty()
    {
        cfg.database.password = Some(SecretString::new(v.into()));
    }
    if let Ok(v) = std::env::var("DATABASE_USERNAME") &&
        !v.trim().is_empty()
    {
        cfg.database.username = v;
    }
    if let Ok(v) = std::env::var("DATABASE_URL") &&
        !v.trim().is_empty()
    {
        cfg.database.url = Some(v);
    }

    if let Ok(v) = std::env::var("PUBLIC_KEY_PEM") &&
        !v.trim().is_empty()
    {
        cfg.jwt.es256_public_key_pem = Some(v);
    }
    if let Ok(v) = std::env::var("PRIVATE_KEY_PEM") &&
        !v.trim().is_empty()
    {
        cfg.jwt.es256_private_key_pem = Some(SecretString::new(v.into()));
    }

    if let Ok(v) = std::env::var("SPICEDB_TOKEN") &&
        !v.trim().is_empty()
    {
        cfg.auth.spicedb_token = Some(SecretString::new(v.into()));
    }
}

fn default_server_port() -> u16 {
    8080
}

fn default_db_host() -> String {
    "localhost".to_string()
}

fn default_db_port() -> u16 {
    5432
}

fn default_db_name() -> String {
    "galadril_dev".to_string()
}

fn default_db_username() -> String {
    "postgres".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn db_url_without_password() {
        let cfg = AppConfig {
            server: ServerConfig { port: 8080 },
            database: DatabaseConfig {
                host: "localhost".to_string(),
                port: 5432,
                name: "db".to_string(),
                username: "user".to_string(),
                password: None,
                url: None,
            },
            jwt: JwtConfig {
                issuer: None,
                audience: None,
                es256_public_key_pem: None,
                es256_private_key_pem: None,
            },
            auth: AuthConfig::default(),
        };

        let url = cfg.database_url().expect("url should build");
        assert_eq!(url, "postgres://user@localhost:5432/db");
    }

    #[test]
    fn db_url_prefers_url_field() {
        let cfg = AppConfig {
            server: ServerConfig { port: 8080 },
            database: DatabaseConfig {
                host: "localhost".to_string(),
                port: 5432,
                name: "db".to_string(),
                username: "user".to_string(),
                password: None,
                url: Some("postgres://example".to_string()),
            },
            jwt: JwtConfig {
                issuer: None,
                audience: None,
                es256_public_key_pem: None,
                es256_private_key_pem: None,
            },
            auth: AuthConfig::default(),
        };

        assert_eq!(cfg.database_url().unwrap(), "postgres://example");
    }

    #[test]
    fn auth_config_defaults_are_safe() {
        let a = AuthConfig::default();
        assert!(a.spicedb_endpoint.is_none());
        assert!(a.spicedb_token.is_none());
        assert_eq!(a.cedar_policy_dsl, "");
    }
}

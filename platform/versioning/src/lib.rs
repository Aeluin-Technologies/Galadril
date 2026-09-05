//! Native versioned document transport; tenant paths never come from callers.

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use reqwest::{Client, Method};
use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DatabaseScope {
    pub database: String,
    pub user: String,
    pub password: SecretString,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TerminusConfig {
    pub endpoint: String,
    pub organization: String,
    pub tenants: HashMap<String, DatabaseScope>,
    pub bases: Option<DatabaseScope>,
}

#[derive(Clone)]
pub struct TerminusClient {
    http: Client,
    config: std::sync::Arc<TerminusConfig>,
}

pub struct Snapshot {
    pub revision: String,
    pub documents: Vec<Value>,
}

pub fn valid_segment(value: &str) -> bool {
    !value.is_empty() &&
        value.len() <= 128 &&
        value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

pub fn named<'a>(documents: &'a [Value], id: &str) -> Option<&'a Value> {
    documents.iter().find(|doc| {
        doc.get("@id")
            .and_then(Value::as_str)
            .is_some_and(|actual| {
                actual == id ||
                    actual.strip_prefix("terminusdb:///data/") == Some(id)
            })
    })
}

impl TerminusClient {
    pub fn new(config: TerminusConfig) -> Result<Self> {
        let url = reqwest::Url::parse(&config.endpoint)?;
        ensure!(
            matches!(url.scheme(), "http" | "https") &&
                url.host_str().is_some() &&
                url.username().is_empty() &&
                url.password().is_none() &&
                url.query().is_none() &&
                url.fragment().is_none(),
            "Invalid TerminusDB endpoint"
        );
        ensure!(
            valid_segment(&config.organization),
            "Invalid TerminusDB organization"
        );
        let mut databases = HashSet::new();
        for (tenant, scope) in &config.tenants {
            ensure!(
                valid_segment(tenant) && valid_segment(&scope.database),
                "Invalid TerminusDB tenant scope"
            );
            ensure!(
                scope.user != "admin" && !scope.user.is_empty(),
                "TerminusDB requires scoped tenant credentials"
            );
            ensure!(
                databases.insert(scope.database.as_str()),
                "Tenants must use distinct TerminusDB databases"
            );
        }
        if let Some(bases) = &config.bases {
            ensure!(
                databases.insert(bases.database.as_str()),
                "Shared bases cannot use a tenant database"
            );
        }
        let http = Client::builder()
            .timeout(Duration::from_secs(30))
            .connect_timeout(Duration::from_secs(10))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self {
            http,
            config: std::sync::Arc::new(config),
        })
    }

    pub fn path(
        &self,
        tenant: &str,
        reference: &str,
        commit: bool,
    ) -> Result<String> {
        let scope = self
            .config
            .tenants
            .get(tenant)
            .context("TerminusDB tenant capability is unavailable")?;
        ensure!(valid_segment(reference), "Invalid TerminusDB reference");
        Ok(format!(
            "{}/{}/local/{}/{}",
            self.config.organization,
            scope.database,
            if commit { "commit" } else { "branch" },
            reference
        ))
    }

    pub async fn read(
        &self,
        tenant: &str,
        reference: &str,
        commit: bool,
    ) -> Result<Snapshot> {
        let path = self.path(tenant, reference, commit)?;
        let (revision, payload) = self
            .request(
                tenant,
                Method::GET,
                "document",
                &path,
                &[("as_list", "true")],
                None,
                None,
            )
            .await?;
        Ok(Snapshot {
            revision: revision
                .context("TerminusDB omitted the data version")?,
            documents: serde_json::from_value(payload)?,
        })
    }

    pub async fn write(
        &self,
        tenant: &str,
        document: &Value,
        expected: &str,
        author: &str,
        message: &str,
    ) -> Result<String> {
        let path = self.path(tenant, "main", false)?;
        let (revision, _) = self
            .request(
                tenant,
                Method::PUT,
                "document",
                &path,
                &[
                    ("raw_json", "true"),
                    ("create", "true"),
                    ("author", author),
                    ("message", message),
                ],
                Some(document),
                Some(expected),
            )
            .await?;
        let revision = revision
            .context("TerminusDB omitted the committed data version")?;
        tracing::info!(
            tenant_id = tenant,
            revision_id = revision,
            "terminus_revision_committed"
        );
        Ok(revision)
    }

    pub async fn history(
        &self,
        tenant: &str,
        document_id: &str,
        limit: usize,
    ) -> Result<Vec<String>> {
        let path = self.path(tenant, "main", false)?;
        let count = limit.to_string();
        let (_, payload) = self
            .request(
                tenant,
                Method::GET,
                "history",
                &path,
                &[("id", document_id), ("count", &count)],
                None,
                None,
            )
            .await?;
        let entries: Vec<Value> = serde_json::from_value(payload)?;
        entries
            .iter()
            .map(|entry| {
                Ok(entry
                    .get("identifier")
                    .and_then(Value::as_str)
                    .context("Missing native history commit")?
                    .to_owned())
            })
            .collect()
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "explicit HTTP transport boundary"
    )]
    async fn request(
        &self,
        tenant: &str,
        method: Method,
        operation: &str,
        path: &str,
        params: &[(&str, &str)],
        body: Option<&Value>,
        expected: Option<&str>,
    ) -> Result<(Option<String>, Value)> {
        let scope = self
            .config
            .tenants
            .get(tenant)
            .context("TerminusDB tenant capability is unavailable")?;
        let mut request = self
            .http
            .request(
                method,
                format!(
                    "{}/api/{operation}/{path}",
                    self.config.endpoint.trim_end_matches('/')
                ),
            )
            .basic_auth(&scope.user, Some(scope.password.expose_secret()))
            .query(params);
        if let Some(body) = body {
            request = request.json(body);
        }
        if let Some(expected) = expected {
            ensure!(
                valid_segment(expected),
                "Invalid native commit identifier"
            );
            request = request.header(
                "TerminusDB-Data-Version",
                format!("branch:{expected}"),
            );
        }
        let mut response =
            request.send().await.context("TerminusDB is unavailable")?;
        let status = response.status();
        let revision = response
            .headers()
            .get("TerminusDB-Data-Version")
            .map(|value| -> Result<String> {
                let (_, revision) = value
                    .to_str()?
                    .split_once(':')
                    .context("Invalid TerminusDB data version")?;
                ensure!(
                    valid_segment(revision),
                    "Invalid native commit identifier"
                );
                Ok(revision.to_owned())
            })
            .transpose()?;
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            ensure!(
                bytes.len().saturating_add(chunk.len()) <= 16 * 1024 * 1024,
                "TerminusDB response exceeds safety limit"
            );
            bytes.extend_from_slice(&chunk);
        }
        if status.as_u16() == 409 ||
            status.as_u16() == 412 ||
            (status.as_u16() == 400 &&
                String::from_utf8_lossy(&bytes).contains("DataVersion"))
        {
            bail!("TerminusDB branch changed; reload before retrying");
        }
        ensure!(
            status.is_success(),
            "TerminusDB operation failed (HTTP {})",
            status.as_u16()
        );
        let payload = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes)?
        };
        Ok((revision, payload))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> Result<TerminusConfig> {
        Ok(serde_json::from_value(serde_json::json!({
            "endpoint": "http://localhost:6363", "organization": "admin",
            "tenants": {"tenant_a": {"database": "tenant_a", "user": "tenant_a", "password": "secret"}}
        }))?)
    }

    #[test]
    fn tenant_scope_cannot_be_used_as_a_database_path() -> Result<()> {
        let client = TerminusClient::new(config()?)?;
        assert!(client.path("tenant_b", "main", false).is_err());
        assert!(client.path("tenant_a/../tenant_b", "main", false).is_err());
        assert!(client.path("tenant_a", "../main", false).is_err());
        assert_eq!(
            client.path("tenant_a", "main", false)?,
            "admin/tenant_a/local/branch/main"
        );
        Ok(())
    }

    #[test]
    fn duplicate_database_and_admin_credentials_are_rejected() -> Result<()> {
        let mut cfg = config()?;
        let scope = cfg.tenants.get("tenant_a").context("fixture")?.clone();
        cfg.tenants.insert("tenant_b".to_owned(), scope);
        assert!(TerminusClient::new(cfg).is_err());
        let mut cfg = config()?;
        cfg.tenants.get_mut("tenant_a").context("fixture")?.user =
            "admin".to_owned();
        assert!(TerminusClient::new(cfg).is_err());
        Ok(())
    }

    #[tokio::test]
    async fn native_write_sends_compare_and_swap_and_returns_server_commit()
    -> Result<()> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let mut cfg = config()?;
        cfg.endpoint = format!("http://{}", listener.local_addr()?);
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await?;
            let mut data = Vec::new();
            let mut buffer = [0_u8; 1024];
            loop {
                let count = socket.read(&mut buffer).await?;
                ensure!(count > 0, "Request closed before headers");
                data.extend_from_slice(
                    buffer.get(..count).context("Read length")?,
                );
                if String::from_utf8_lossy(&data).contains("\r\n\r\n") {
                    break;
                }
            }
            let request = String::from_utf8(data)?.to_ascii_lowercase();
            ensure!(
                request.starts_with(
                    "put /api/document/admin/tenant_a/local/branch/main?"
                ),
                "Wrong tenant path"
            );
            ensure!(
                request.contains("terminusdb-data-version: branch:oldcommit"),
                "Missing native optimistic constraint"
            );
            ensure!(
                request.contains("raw_json=true"),
                "Missing document mode"
            );
            socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTerminusDB-Data-Version: branch:nativecommit\r\nConnection: close\r\n\r\n[]").await?;
            Ok::<(), anyhow::Error>(())
        });
        let client = TerminusClient::new(cfg)?;
        let result = client
            .write(
                "tenant_a",
                &serde_json::json!({"@id":"pipeline/daily"}),
                "oldcommit",
                "alice",
                "Edit",
            )
            .await?;
        server.await??;
        assert_eq!(result, "nativecommit");
        Ok(())
    }
}

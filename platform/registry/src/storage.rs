//! lakeFS transport; S3 and repository resolution never cross this boundary.

use std::collections::HashMap;
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use async_trait::async_trait;
use aws_sdk_s3::Client as S3Client;
use aws_sdk_s3::config::{Credentials, Region};
use aws_sdk_s3::primitives::ByteStream;
use aws_sdk_s3::types::{Delete, ObjectIdentifier};
use reqwest::{Client, StatusCode};
use secrecy::{ExposeSecret, SecretString};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use tokio::sync::{Mutex, OwnedMutexGuard};

use crate::state::TenantState;

const STATE_PATH: &str = "registry/state.json";
const TENANT_MARKER_VERSION: u8 = 1;
const MAX_STATE_BYTES: usize = 32 * 1024 * 1024;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
/// Registry-owned lakeFS connection and physical S3 namespace settings.
pub struct LakeFsConfig {
    /// lakeFS HTTP API endpoint.
    pub endpoint: String,
    /// lakeFS service access key.
    pub access_key: String,
    /// lakeFS service secret key.
    pub secret_key: SecretString,
    /// Prefix for opaque per-tenant lakeFS repository names.
    pub repository_prefix: String,
    /// Registry-exclusive S3 namespace prefix used by lakeFS.
    pub storage_namespace: String,
    #[serde(default = "default_branch")]
    /// Registry-owned mutable branch whose commits are public revision IDs.
    pub branch: String,
}

#[derive(Debug)]
/// Registry-owned S3 connection used only for tenant markers and GDPR purge.
pub struct S3Config {
    /// S3-compatible HTTP endpoint.
    pub endpoint: String,
    /// S3 signing region.
    pub region: String,
    /// S3 service access key.
    pub access_key: String,
    /// S3 service secret key.
    pub secret_key: SecretString,
}

fn default_branch() -> String {
    "main".to_owned()
}

#[derive(Debug)]
/// Tenant state reconstructed from one immutable lakeFS revision.
pub struct Snapshot {
    /// Opaque immutable lakeFS commit ID.
    pub revision_id: String,
    /// Deserialized tenant artifact at the commit.
    pub state: TenantState,
}

#[derive(Debug)]
/// Minimal tenant projection that never exposes repositories or object paths.
pub struct TenantSnapshot {
    /// Immutable current branch head.
    pub revision_id: String,
}

#[async_trait]
/// Versioned storage contract kept below all domain semantics.
pub trait RevisionStore: Send + Sync {
    /// Verifies that the tenant marker physically exists in S3.
    async fn tenant_exists(&self, tenant_id: &str) -> Result<bool>;

    /// Idempotently creates the tenant marker and lakeFS repository.
    async fn ensure_tenant(&self, tenant_id: &str) -> Result<TenantSnapshot>;

    /// Irreversibly removes the tenant repository and its physical S3 data.
    async fn delete_tenant(&self, tenant_id: &str) -> Result<()>;

    /// Loads the branch head or an explicitly pinned immutable revision.
    async fn load(
        &self,
        tenant_id: &str,
        revision_id: Option<&str>,
    ) -> Result<Snapshot>;

    /// Atomically commits validated state when the expected head still
    /// matches.
    async fn commit(
        &self,
        tenant_id: &str,
        expected_revision_id: &str,
        state: &TenantState,
        author: &str,
        message: &str,
    ) -> Result<String>;
}

/// lakeFS implementation that keeps repository and S3 resolution server-side.
pub struct LakeFsStore {
    config: LakeFsConfig,
    http: Client,
    directory: S3TenantDirectory,
    locks: Arc<StdMutex<HashMap<String, Arc<Mutex<()>>>>>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TenantMarker {
    version: u8,
    tenant_id: String,
    repository: String,
}

/// Direct S3 boundary used for physical tenant existence and GDPR erasure.
pub struct S3TenantDirectory {
    client: S3Client,
    bucket: String,
    prefix: String,
}

#[derive(Deserialize)]
struct LakeFsRef {
    id: String,
}

impl LakeFsStore {
    /// Validates storage configuration and builds a bounded HTTP client.
    pub fn new(config: LakeFsConfig, s3: S3Config) -> Result<Self> {
        let endpoint = reqwest::Url::parse(&config.endpoint)?;
        ensure!(
            matches!(endpoint.scheme(), "http" | "https") &&
                endpoint.host_str().is_some() &&
                endpoint.username().is_empty() &&
                endpoint.password().is_none() &&
                matches!(endpoint.path(), "" | "/") &&
                endpoint.query().is_none() &&
                endpoint.fragment().is_none(),
            "invalid lakeFS endpoint"
        );
        ensure!(
            valid_segment(&config.repository_prefix),
            "invalid repository prefix"
        );
        ensure!(valid_segment(&config.branch), "invalid lakeFS branch");
        ensure!(
            !config.access_key.is_empty() && config.access_key.len() <= 256,
            "invalid lakeFS access key"
        );
        ensure!(
            !config.secret_key.expose_secret().is_empty(),
            "invalid lakeFS secret key"
        );
        ensure!(
            config.storage_namespace.starts_with("s3://") &&
                config.storage_namespace.ends_with('/'),
            "lakeFS storage namespace must be an S3 prefix ending in slash"
        );
        let http = Client::builder()
            .timeout(Duration::from_secs(30))
            .connect_timeout(Duration::from_secs(5))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        let directory = S3TenantDirectory::new(s3, &config.storage_namespace)?;
        Ok(Self {
            config,
            http,
            directory,
            locks: Arc::new(StdMutex::new(HashMap::new())),
        })
    }

    /// Derives an opaque repository name from a validated tenant identity.
    pub fn repository_for_tenant(&self, tenant_id: &str) -> Result<String> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        let digest = Sha256::digest(tenant_id.as_bytes());
        Ok(format!("{}-{:x}", self.config.repository_prefix, digest)
            .chars()
            .take(63)
            .collect())
    }

    fn namespace_for_tenant(&self, tenant_id: &str) -> Result<String> {
        let repository = self.repository_for_tenant(tenant_id)?;
        Ok(format!("{}{repository}/", self.config.storage_namespace))
    }

    fn marker_key(&self, tenant_id: &str) -> Result<String> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        let digest = Sha256::digest(tenant_id.as_bytes());
        Ok(format!("_tenants/{digest:x}.json"))
    }

    async fn marker(&self, tenant_id: &str) -> Result<Option<TenantMarker>> {
        let Some(payload) =
            self.directory.read(&self.marker_key(tenant_id)?).await?
        else {
            return Ok(None);
        };
        ensure!(payload.len() <= 4096, "tenant marker exceeds safety limit");
        let marker: TenantMarker = serde_json::from_slice(&payload)
            .context("invalid tenant marker")?;
        let repository = self.repository_for_tenant(tenant_id)?;
        ensure!(
            marker.version == TENANT_MARKER_VERSION &&
                marker.tenant_id == tenant_id &&
                marker.repository == repository,
            "tenant marker integrity check failed"
        );
        Ok(Some(marker))
    }

    async fn require_tenant(&self, tenant_id: &str) -> Result<TenantMarker> {
        self.marker(tenant_id).await?.context("tenant unavailable")
    }

    async fn tenant_lock(
        &self,
        tenant_id: &str,
    ) -> Result<OwnedMutexGuard<()>> {
        let lock = {
            let mut locks = self.locks.lock().map_err(|_| {
                anyhow::anyhow!("registry tenant lock poisoned")
            })?;
            Arc::clone(
                locks
                    .entry(tenant_id.to_owned())
                    .or_insert_with(|| Arc::new(Mutex::new(()))),
            )
        };
        Ok(lock.lock_owned().await)
    }

    async fn ensure_repository(&self, tenant_id: &str) -> Result<String> {
        let repository = self.repository_for_tenant(tenant_id)?;
        let branch_url = self.url(&format!(
            "repositories/{repository}/branches/{}",
            self.config.branch
        ));
        let response =
            self.authorize(self.http.get(&branch_url)).send().await?;
        if response.status().is_success() {
            return Ok(repository);
        }
        ensure!(
            response.status() == StatusCode::NOT_FOUND,
            "lakeFS branch lookup failed"
        );
        let create = self
            .authorize(self.http.post(self.url("repositories")))
            .json(&json!({
                "name": repository,
                "storage_namespace": self.namespace_for_tenant(tenant_id)?,
                "default_branch": self.config.branch,
                "sample_data": false,
            }))
            .send()
            .await?;
        ensure!(
            create.status().is_success() ||
                create.status() == StatusCode::CONFLICT,
            "lakeFS repository provisioning failed"
        );
        Ok(repository)
    }

    async fn delete_repository(&self, repository: &str) -> Result<()> {
        let response = self
            .authorize(
                self.http
                    .delete(self.url(&format!("repositories/{repository}"))),
            )
            .send()
            .await?;
        ensure!(
            response.status().is_success() ||
                response.status() == StatusCode::NOT_FOUND,
            "lakeFS repository deletion failed"
        );
        Ok(())
    }

    async fn branch_head(&self, repository: &str) -> Result<String> {
        let response = self
            .authorize(self.http.get(self.url(&format!(
                "repositories/{repository}/branches/{}",
                self.config.branch
            ))))
            .send()
            .await?;
        ensure!(
            response.status().is_success(),
            "lakeFS branch lookup failed"
        );
        Ok(response.json::<LakeFsRef>().await?.id)
    }

    fn authorize(
        &self,
        request: reqwest::RequestBuilder,
    ) -> reqwest::RequestBuilder {
        request.basic_auth(
            &self.config.access_key,
            Some(self.config.secret_key.expose_secret()),
        )
    }

    fn url(&self, path: &str) -> String {
        format!(
            "{}/api/v1/{path}",
            self.config.endpoint.trim_end_matches('/')
        )
    }

    async fn read_state(
        &self,
        repository: &str,
        reference: &str,
    ) -> Result<TenantState> {
        let mut response = self
            .authorize(
                self.http
                    .get(self.url(&format!(
                        "repositories/{repository}/refs/{reference}/objects"
                    )))
                    .query(&[("path", STATE_PATH)]),
            )
            .send()
            .await?;
        if response.status() == StatusCode::NOT_FOUND {
            return Ok(TenantState::default());
        }
        ensure!(
            response.status().is_success(),
            "lakeFS artifact read failed"
        );
        let content_length = response
            .content_length()
            .context("lakeFS artifact response length is required")?;
        ensure!(
            content_length <= MAX_STATE_BYTES as u64,
            "registry state exceeds safety limit"
        );
        let capacity = usize::try_from(content_length)
            .context("registry state response length is unsupported")?;
        let mut payload = Vec::with_capacity(capacity);
        while let Some(chunk) = response.chunk().await? {
            let next_length = payload
                .len()
                .checked_add(chunk.len())
                .context("registry state size overflow")?;
            ensure!(
                next_length <= MAX_STATE_BYTES,
                "registry state exceeds safety limit"
            );
            payload.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&payload)
            .context("invalid Registry state artifact")
    }
}

impl S3TenantDirectory {
    /// Builds a path-style S3 client scoped to the Registry namespace.
    pub fn new(config: S3Config, storage_namespace: &str) -> Result<Self> {
        let S3Config {
            endpoint: endpoint_value,
            region,
            access_key,
            secret_key,
        } = config;
        let endpoint = reqwest::Url::parse(&endpoint_value)?;
        ensure!(
            matches!(endpoint.scheme(), "http" | "https") &&
                endpoint.host_str().is_some() &&
                endpoint.username().is_empty() &&
                endpoint.password().is_none() &&
                matches!(endpoint.path(), "" | "/") &&
                endpoint.query().is_none() &&
                endpoint.fragment().is_none(),
            "invalid S3 endpoint"
        );
        ensure!(
            !region.is_empty() &&
                region.len() <= 64 &&
                region.bytes().all(|byte| {
                    byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_')
                }),
            "invalid S3 region"
        );
        ensure!(
            !access_key.is_empty() && access_key.len() <= 256,
            "invalid S3 access key"
        );
        ensure!(
            !secret_key.expose_secret().is_empty(),
            "invalid S3 secret key"
        );
        let namespace = storage_namespace
            .strip_prefix("s3://")
            .context("invalid S3 storage namespace")?;
        let (bucket, prefix) = namespace
            .split_once('/')
            .context("S3 storage namespace requires a prefix")?;
        ensure!(valid_bucket(bucket), "invalid S3 bucket");
        ensure!(
            !prefix.is_empty() &&
                prefix.ends_with('/') &&
                !prefix.starts_with('/') &&
                !prefix.contains("..") &&
                !prefix.contains("//") &&
                prefix.bytes().all(|byte| {
                    byte.is_ascii_alphanumeric() ||
                        matches!(byte, b'/' | b'_' | b'-' | b'.')
                }),
            "invalid S3 namespace prefix"
        );
        let credentials = Credentials::new(
            access_key,
            secret_key.expose_secret(),
            None,
            None,
            "registry-static",
        );
        let client = S3Client::from_conf(
            aws_sdk_s3::config::Builder::new()
                .behavior_version_latest()
                .endpoint_url(endpoint_value)
                .region(Region::new(region))
                .credentials_provider(credentials)
                .force_path_style(true)
                .build(),
        );
        Ok(Self {
            client,
            bucket: bucket.to_owned(),
            prefix: prefix.to_owned(),
        })
    }

    fn key(&self, relative: &str) -> Result<String> {
        ensure!(
            !relative.is_empty() &&
                !relative.starts_with('/') &&
                !relative.contains(".."),
            "invalid Registry S3 key"
        );
        Ok(format!("{}{relative}", self.prefix))
    }

    async fn read(&self, relative: &str) -> Result<Option<Vec<u8>>> {
        let result = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(self.key(relative)?)
            .send()
            .await;
        let output = match result {
            Ok(output) => output,
            Err(error)
                if error
                    .as_service_error()
                    .is_some_and(|service| service.is_no_such_key()) =>
            {
                return Ok(None);
            },
            Err(error) => {
                return Err(error).context("S3 tenant marker read failed");
            },
        };
        ensure!(
            output
                .content_length()
                .is_some_and(|length| (0..=4096).contains(&length)),
            "S3 tenant marker exceeds safety limit"
        );
        let bytes = output
            .body
            .collect()
            .await
            .context("S3 tenant marker body failed")?
            .into_bytes();
        Ok(Some(bytes.to_vec()))
    }

    async fn write(&self, relative: &str, payload: Vec<u8>) -> Result<()> {
        self.client
            .put_object()
            .bucket(&self.bucket)
            .key(self.key(relative)?)
            .content_type("application/json")
            .body(ByteStream::from(payload))
            .send()
            .await
            .context("S3 tenant marker write failed")?;
        Ok(())
    }

    async fn delete(&self, relative: &str) -> Result<()> {
        self.client
            .delete_object()
            .bucket(&self.bucket)
            .key(self.key(relative)?)
            .send()
            .await
            .context("S3 tenant marker deletion failed")?;
        Ok(())
    }

    async fn purge_prefix(&self, relative_prefix: &str) -> Result<()> {
        let prefix = self.key(relative_prefix)?;
        loop {
            let listed = self
                .client
                .list_objects_v2()
                .bucket(&self.bucket)
                .prefix(&prefix)
                .max_keys(1000)
                .send()
                .await
                .context("S3 tenant object listing failed")?;
            if listed.contents().is_empty() {
                return Ok(());
            }
            let objects = listed
                .contents()
                .iter()
                .map(|object| {
                    let key = object
                        .key()
                        .context("S3 returned an object without a key")?;
                    ObjectIdentifier::builder()
                        .key(key)
                        .build()
                        .context("invalid S3 deletion key")
                })
                .collect::<Result<Vec<_>>>()?;
            let delete = Delete::builder()
                .set_objects(Some(objects))
                .quiet(true)
                .build()
                .context("invalid S3 deletion batch")?;
            let deleted = self
                .client
                .delete_objects()
                .bucket(&self.bucket)
                .delete(delete)
                .send()
                .await
                .context("S3 tenant object purge failed")?;
            ensure!(
                deleted.errors().is_empty(),
                "S3 tenant object purge failed"
            );
        }
    }
}

#[async_trait]
impl RevisionStore for LakeFsStore {
    async fn tenant_exists(&self, tenant_id: &str) -> Result<bool> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        Ok(self.marker(tenant_id).await?.is_some())
    }

    async fn ensure_tenant(&self, tenant_id: &str) -> Result<TenantSnapshot> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        let _guard = self.tenant_lock(tenant_id).await?;
        if let Some(marker) = self.marker(tenant_id).await? {
            return Ok(TenantSnapshot {
                revision_id: self.branch_head(&marker.repository).await?,
            });
        }
        let repository = self.ensure_repository(tenant_id).await?;
        let revision_id = self.branch_head(&repository).await?;
        let marker = TenantMarker {
            version: TENANT_MARKER_VERSION,
            tenant_id: tenant_id.to_owned(),
            repository,
        };
        self.directory
            .write(&self.marker_key(tenant_id)?, serde_json::to_vec(&marker)?)
            .await?;
        tracing::info!(tenant_id, revision_id, "registry_tenant_initialized");
        Ok(TenantSnapshot { revision_id })
    }

    async fn delete_tenant(&self, tenant_id: &str) -> Result<()> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        let _guard = self.tenant_lock(tenant_id).await?;
        let marker = self.require_tenant(tenant_id).await?;
        self.delete_repository(&marker.repository).await?;
        self.directory
            .purge_prefix(&format!("{}/", marker.repository))
            .await?;
        self.directory.delete(&self.marker_key(tenant_id)?).await?;
        tracing::warn!(tenant_id, "registry_tenant_gdpr_purged");
        Ok(())
    }

    async fn load(
        &self,
        tenant_id: &str,
        revision_id: Option<&str>,
    ) -> Result<Snapshot> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        if let Some(value) = revision_id {
            ensure!(valid_revision(value), "invalid revision ID");
        }
        let repository = self.require_tenant(tenant_id).await?.repository;
        let revision = match revision_id {
            Some(value) => value.to_owned(),
            None => self.branch_head(&repository).await?,
        };
        let state = self.read_state(&repository, &revision).await?;
        Ok(Snapshot {
            revision_id: revision,
            state,
        })
    }

    async fn commit(
        &self,
        tenant_id: &str,
        expected_revision_id: &str,
        state: &TenantState,
        author: &str,
        message: &str,
    ) -> Result<String> {
        ensure!(
            valid_revision(expected_revision_id),
            "invalid expected revision ID"
        );
        ensure!(!author.is_empty() && author.len() <= 256, "invalid author");
        ensure!(
            !message.is_empty() && message.len() <= 4096,
            "invalid message"
        );
        let _guard = self.tenant_lock(tenant_id).await?;
        let repository = self.require_tenant(tenant_id).await?.repository;
        let head = self.branch_head(&repository).await?;
        ensure!(head == expected_revision_id, "registry revision changed");
        let payload = serde_json::to_vec(state)?;
        ensure!(
            payload.len() <= MAX_STATE_BYTES,
            "registry state exceeds safety limit"
        );
        let upload = self
            .authorize(self.http.post(self.url(&format!(
                "repositories/{repository}/branches/{}/objects",
                self.config.branch
            ))))
            .query(&[("path", STATE_PATH)])
            .header(reqwest::header::CONTENT_TYPE, "application/octet-stream")
            .body(payload)
            .send()
            .await?;
        ensure!(
            upload.status().is_success(),
            "lakeFS artifact staging failed"
        );
        let response = self
            .authorize(self.http.post(self.url(&format!(
                "repositories/{repository}/branches/{}/commits",
                self.config.branch
            ))))
            .json(&json!({
                "message": message,
                "metadata": {"author": author, "tenant_digest": repository},
            }))
            .send()
            .await?;
        if response.status() == StatusCode::PRECONDITION_FAILED ||
            response.status() == StatusCode::CONFLICT
        {
            bail!("registry revision changed");
        }
        ensure!(response.status().is_success(), "lakeFS commit failed");
        let revision = response.json::<LakeFsRef>().await?.id;
        ensure!(
            valid_revision(&revision),
            "lakeFS returned invalid commit ID"
        );
        tracing::info!(
            tenant_id,
            revision_id = revision,
            "registry_revision_committed"
        );
        Ok(revision)
    }
}

/// Reports whether a tenant identity is safe for server-side resolution.
pub fn valid_tenant(value: &str) -> bool {
    !value.is_empty() &&
        value.len() <= 64 &&
        value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')
        })
}

/// Reports whether a lakeFS-managed branch or repository prefix is valid.
pub fn valid_segment(value: &str) -> bool {
    !value.is_empty() &&
        value.len() <= 63 &&
        value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-'
        })
}

fn valid_bucket(value: &str) -> bool {
    value.len() >= 3 &&
        value.len() <= 63 &&
        value.bytes().all(|byte| {
            byte.is_ascii_lowercase() ||
                byte.is_ascii_digit() ||
                matches!(byte, b'.' | b'-')
        }) &&
        !value.starts_with('.') &&
        !value.starts_with('-') &&
        !value.ends_with('.') &&
        !value.ends_with('-') &&
        !value.contains("..")
}

/// Reports whether a caller-supplied opaque revision ID has a safe shape.
pub fn valid_revision(value: &str) -> bool {
    value.len() >= 20 &&
        value.len() <= 128 &&
        value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')
        })
}

#[cfg(test)]
mod tests {
    use anyhow::Context;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, TcpStream};
    use tokio::task::JoinHandle;

    use super::*;

    const HEAD: &str = "aaaaaaaaaaaaaaaaaaaa";
    const NEXT: &str = "bbbbbbbbbbbbbbbbbbbb";

    struct MockResponse {
        status: &'static str,
        body: String,
    }

    fn response(
        status: &'static str,
        body: impl Into<String>,
    ) -> MockResponse {
        MockResponse {
            status,
            body: body.into(),
        }
    }

    async fn mock_lakefs(
        responses: Vec<MockResponse>,
    ) -> Result<(String, JoinHandle<Result<Vec<String>>>)> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let handle = tokio::spawn(async move {
            let mut requests = Vec::with_capacity(responses.len());
            for response in responses {
                let (mut stream, _) = listener.accept().await?;
                requests.push(read_request(&mut stream).await?);
                let message = format!(
                    "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response.status,
                    response.body.len(),
                    response.body
                );
                stream.write_all(message.as_bytes()).await?;
                stream.shutdown().await?;
            }
            Ok(requests)
        });
        Ok((format!("http://{address}"), handle))
    }

    async fn read_request(stream: &mut TcpStream) -> Result<String> {
        let mut request = Vec::with_capacity(4096);
        let mut chunk = [0_u8; 4096];
        loop {
            let read = stream.read(&mut chunk).await?;
            ensure!(read != 0, "mock request ended before headers");
            request.extend_from_slice(
                chunk.get(..read).context("invalid mock read length")?,
            );
            let Some(header_end) = request
                .windows(4)
                .position(|window| window == b"\r\n\r\n")
                .map(|position| position + 4)
            else {
                continue;
            };
            let headers = std::str::from_utf8(
                request
                    .get(..header_end)
                    .context("invalid mock header length")?,
            )?;
            let content_length = headers
                .lines()
                .find_map(|line| {
                    line.to_ascii_lowercase()
                        .strip_prefix("content-length:")
                        .map(str::trim)
                        .and_then(|value| value.parse::<usize>().ok())
                })
                .unwrap_or_default();
            if request.len() >= header_end + content_length {
                return String::from_utf8(request).map_err(Into::into);
            }
        }
    }

    fn config(endpoint: &str) -> LakeFsConfig {
        LakeFsConfig {
            endpoint: endpoint.to_owned(),
            access_key: "registry".to_owned(),
            secret_key: SecretString::from("secret"),
            repository_prefix: "galadril".to_owned(),
            storage_namespace: "s3://lake/registry/".to_owned(),
            branch: "main".to_owned(),
        }
    }

    fn s3_config(endpoint: &str) -> S3Config {
        S3Config {
            endpoint: endpoint.to_owned(),
            region: "us-east-1".to_owned(),
            access_key: "minio".to_owned(),
            secret_key: SecretString::from("secret"),
        }
    }

    fn tenant_marker(tenant_id: &str) -> String {
        let repository =
            format!("galadril-{:x}", Sha256::digest(tenant_id.as_bytes()))
                .chars()
                .take(63)
                .collect::<String>();
        json!({
            "version": TENANT_MARKER_VERSION,
            "tenant_id": tenant_id,
            "repository": repository,
        })
        .to_string()
    }

    fn s3_missing() -> MockResponse {
        response(
            "404 Not Found",
            "<Error><Code>NoSuchKey</Code><Message>missing</Message></Error>",
        )
    }

    #[test]
    fn repository_resolution_is_server_owned_and_non_reversible() -> Result<()>
    {
        let store = LakeFsStore::new(
            config("http://lakefs:8000"),
            s3_config("http://minio:9000"),
        )?;
        let repository = store.repository_for_tenant("tenant_a")?;
        assert!(repository.starts_with("galadril-"));
        assert!(!repository.contains("tenant_a"));
        assert!(store.repository_for_tenant("../tenant_b").is_err());
        assert!(valid_tenant("tenant-1"));
        assert!(!valid_tenant(""));
        assert!(!valid_tenant(&"a".repeat(65)));
        assert!(valid_segment("registry-1"));
        assert!(!valid_segment("Registry"));
        assert!(valid_revision(HEAD));
        assert!(!valid_revision("short"));
        Ok(())
    }

    #[test]
    fn configuration_rejects_unsafe_storage_coordinates() {
        assert!(
            LakeFsStore::new(
                config("ftp://lakefs:8000"),
                s3_config("http://minio:9000")
            )
            .is_err()
        );
        assert!(
            LakeFsStore::new(
                config("http://user@lakefs:8000"),
                s3_config("http://minio:9000")
            )
            .is_err()
        );

        let mut invalid_prefix = config("http://lakefs:8000");
        invalid_prefix.repository_prefix = "Registry".to_owned();
        assert!(
            LakeFsStore::new(invalid_prefix, s3_config("http://minio:9000"))
                .is_err()
        );

        let mut invalid_branch = config("http://lakefs:8000");
        invalid_branch.branch = "feature/x".to_owned();
        assert!(
            LakeFsStore::new(invalid_branch, s3_config("http://minio:9000"))
                .is_err()
        );

        let mut invalid_namespace = config("http://lakefs:8000");
        invalid_namespace.storage_namespace = "s3://lake/registry".to_owned();
        assert!(
            LakeFsStore::new(
                invalid_namespace,
                s3_config("http://minio:9000")
            )
            .is_err()
        );

        let invalid_s3 = s3_config("ftp://minio:9000");
        assert!(
            LakeFsStore::new(config("http://lakefs:8000"), invalid_s3)
                .is_err()
        );
    }

    #[tokio::test]
    async fn ensure_tenant_provisions_repository_and_physical_s3_marker()
    -> Result<()> {
        let (endpoint, handle) = mock_lakefs(vec![
            response("404 Not Found", "{}"),
            response("201 Created", "{}"),
            response("200 OK", format!(r#"{{"id":"{HEAD}"}}"#)),
        ])
        .await?;
        let (s3_endpoint, s3_handle) =
            mock_lakefs(vec![s3_missing(), response("200 OK", "")]).await?;
        let store =
            LakeFsStore::new(config(&endpoint), s3_config(&s3_endpoint))?;

        let snapshot = store.ensure_tenant("tenant_a").await?;

        assert_eq!(snapshot.revision_id, HEAD);
        let requests = handle.await??;
        let create = requests
            .get(1)
            .context("repository creation request missing")?;
        assert!(create.starts_with("POST /api/v1/repositories "));
        assert!(create.contains("s3://lake/registry/galadril-"));
        assert!(!create.contains("tenant_a"));
        assert!(create.to_ascii_lowercase().contains("authorization: basic"));
        let s3_requests = s3_handle.await??;
        assert!(
            s3_requests
                .first()
                .context("marker lookup missing")?
                .starts_with("GET /lake/registry/_tenants/")
        );
        let marker_write =
            s3_requests.get(1).context("marker write missing")?;
        assert!(marker_write.starts_with("PUT /lake/registry/_tenants/"));
        assert!(marker_write.contains("tenant_a"));
        Ok(())
    }

    #[tokio::test]
    async fn load_reads_a_pinned_immutable_state() -> Result<()> {
        let body = serde_json::to_string(&TenantState::default())?;
        let (endpoint, handle) =
            mock_lakefs(vec![response("200 OK", body)]).await?;
        let (s3_endpoint, s3_handle) =
            mock_lakefs(vec![response("200 OK", tenant_marker("tenant_a"))])
                .await?;
        let store =
            LakeFsStore::new(config(&endpoint), s3_config(&s3_endpoint))?;

        let snapshot = store.load("tenant_a", Some(HEAD)).await?;

        assert_eq!(snapshot.revision_id, HEAD);
        let requests = handle.await??;
        assert_eq!(requests.len(), 1);
        assert!(requests.first().context("state request missing")?.contains(
            &format!("/refs/{HEAD}/objects?path=registry%2Fstate.json")
        ));
        assert_eq!(s3_handle.await??.len(), 1);
        Ok(())
    }

    #[tokio::test]
    async fn read_and_delete_fail_closed_for_unknown_tenants() -> Result<()> {
        let (s3_endpoint, s3_handle) =
            mock_lakefs(vec![s3_missing(), s3_missing()]).await?;
        let store = LakeFsStore::new(
            config("http://127.0.0.1:1"),
            s3_config(&s3_endpoint),
        )?;

        assert!(store.load("tenant_a", None).await.is_err());
        assert!(store.delete_tenant("tenant_a").await.is_err());
        assert_eq!(s3_handle.await??.len(), 2);
        Ok(())
    }

    #[tokio::test]
    async fn tenant_validation_rejects_tampered_s3_markers() -> Result<()> {
        let (s3_endpoint, s3_handle) =
            mock_lakefs(vec![response("200 OK", tenant_marker("tenant_b"))])
                .await?;
        let store = LakeFsStore::new(
            config("http://127.0.0.1:1"),
            s3_config(&s3_endpoint),
        )?;

        assert!(store.tenant_exists("tenant_a").await.is_err());
        assert_eq!(s3_handle.await??.len(), 1);
        Ok(())
    }

    #[tokio::test]
    async fn gdpr_delete_removes_lakefs_repository_and_physical_s3_data()
    -> Result<()> {
        let repository = format!("galadril-{:x}", Sha256::digest(b"tenant_a"))
            .chars()
            .take(63)
            .collect::<String>();
        let (endpoint, handle) =
            mock_lakefs(vec![response("204 No Content", "")]).await?;
        let listed = format!(
            "<ListBucketResult><Name>lake</Name><Prefix>registry/{repository}/</Prefix><KeyCount>1</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated><Contents><Key>registry/{repository}/data/object</Key><Size>1</Size></Contents></ListBucketResult>"
        );
        let empty = format!(
            "<ListBucketResult><Name>lake</Name><Prefix>registry/{repository}/</Prefix><KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated></ListBucketResult>"
        );
        let (s3_endpoint, s3_handle) = mock_lakefs(vec![
            response("200 OK", tenant_marker("tenant_a")),
            response("200 OK", listed),
            response("200 OK", "<DeleteResult/>"),
            response("200 OK", empty),
            response("204 No Content", ""),
        ])
        .await?;
        let store =
            LakeFsStore::new(config(&endpoint), s3_config(&s3_endpoint))?;

        store.delete_tenant("tenant_a").await?;

        let lakefs = handle.await??;
        assert!(
            lakefs
                .first()
                .context("repository purge missing")?
                .starts_with(&format!(
                    "DELETE /api/v1/repositories/{repository} "
                ))
        );
        let s3 = s3_handle.await??;
        assert!(
            s3.get(1)
                .context("object listing missing")?
                .contains(&format!("prefix=registry%2F{repository}%2F"))
        );
        let object_purge = s3.get(2).context("object purge missing")?;
        assert!(object_purge.starts_with("POST /lake"));
        assert!(object_purge.contains("delete"));
        assert!(
            s3.get(4)
                .context("marker purge missing")?
                .starts_with("DELETE /lake/registry/_tenants/")
        );
        Ok(())
    }

    #[tokio::test]
    async fn commit_stages_state_and_returns_the_lakefs_commit_id()
    -> Result<()> {
        let (endpoint, handle) = mock_lakefs(vec![
            response("200 OK", format!(r#"{{"id":"{HEAD}"}}"#)),
            response("201 Created", "{}"),
            response("201 Created", format!(r#"{{"id":"{NEXT}"}}"#)),
        ])
        .await?;
        let (s3_endpoint, s3_handle) =
            mock_lakefs(vec![response("200 OK", tenant_marker("tenant_a"))])
                .await?;
        let store =
            LakeFsStore::new(config(&endpoint), s3_config(&s3_endpoint))?;

        let revision = store
            .commit(
                "tenant_a",
                HEAD,
                &TenantState::default(),
                "author",
                "message",
            )
            .await?;

        assert_eq!(revision, NEXT);
        let requests = handle.await??;
        assert!(
            requests
                .get(1)
                .context("state upload missing")?
                .starts_with("POST /api/v1/repositories/")
        );
        let commit = requests.get(2).context("commit request missing")?;
        assert!(commit.contains("/branches/main/commits "));
        assert!(commit.contains(r#""tenant_digest":"galadril-"#));
        assert_eq!(s3_handle.await??.len(), 1);
        Ok(())
    }

    #[tokio::test]
    async fn commit_rejects_stale_heads_and_lakefs_conflicts() -> Result<()> {
        let (stale_endpoint, stale_handle) = mock_lakefs(vec![response(
            "200 OK",
            format!(r#"{{"id":"{NEXT}"}}"#),
        )])
        .await?;
        let (stale_s3, stale_s3_handle) =
            mock_lakefs(vec![response("200 OK", tenant_marker("tenant_a"))])
                .await?;
        let stale_store =
            LakeFsStore::new(config(&stale_endpoint), s3_config(&stale_s3))?;
        assert!(
            stale_store
                .commit(
                    "tenant_a",
                    HEAD,
                    &TenantState::default(),
                    "author",
                    "message"
                )
                .await
                .is_err()
        );
        assert_eq!(stale_handle.await??.len(), 1);
        assert_eq!(stale_s3_handle.await??.len(), 1);

        let (conflict_endpoint, conflict_handle) = mock_lakefs(vec![
            response("200 OK", format!(r#"{{"id":"{HEAD}"}}"#)),
            response("201 Created", "{}"),
            response("409 Conflict", "{}"),
        ])
        .await?;
        let (conflict_s3, conflict_s3_handle) =
            mock_lakefs(vec![response("200 OK", tenant_marker("tenant_a"))])
                .await?;
        let conflict_store = LakeFsStore::new(
            config(&conflict_endpoint),
            s3_config(&conflict_s3),
        )?;
        assert!(
            conflict_store
                .commit(
                    "tenant_a",
                    HEAD,
                    &TenantState::default(),
                    "author",
                    "message"
                )
                .await
                .is_err()
        );
        assert_eq!(conflict_handle.await??.len(), 3);
        assert_eq!(conflict_s3_handle.await??.len(), 1);
        Ok(())
    }

    #[tokio::test]
    async fn storage_errors_fail_closed() -> Result<()> {
        let store = LakeFsStore::new(
            config("http://127.0.0.1:1"),
            s3_config("http://127.0.0.1:1"),
        )?;
        assert!(store.load("../tenant", None).await.is_err());
        assert!(store.load("tenant", Some("short")).await.is_err());
        assert!(
            store
                .commit(
                    "tenant",
                    "short",
                    &TenantState::default(),
                    "author",
                    "message"
                )
                .await
                .is_err()
        );
        assert!(
            store
                .commit("tenant", HEAD, &TenantState::default(), "", "message")
                .await
                .is_err()
        );
        assert!(
            store
                .commit("tenant", HEAD, &TenantState::default(), "author", "")
                .await
                .is_err()
        );

        let (endpoint, handle) =
            mock_lakefs(vec![response("500 Internal Server Error", "{}")])
                .await?;
        let (s3_endpoint, s3_handle) =
            mock_lakefs(vec![response("200 OK", tenant_marker("tenant"))])
                .await?;
        assert!(
            LakeFsStore::new(config(&endpoint), s3_config(&s3_endpoint))?
                .load("tenant", None)
                .await
                .is_err()
        );
        assert_eq!(handle.await??.len(), 1);
        assert_eq!(s3_handle.await??.len(), 1);
        Ok(())
    }
}

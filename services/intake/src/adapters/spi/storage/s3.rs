//! Amazon S3 cloud storage repository link.

use std::collections::HashMap;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use async_trait::async_trait;
use aws_config::Region;
use aws_config::timeout::TimeoutConfig;
use aws_sdk_s3::Client;
use aws_sdk_s3::config::Credentials;
use aws_sdk_s3::primitives::ByteStream;
use bytes::Bytes;

use crate::domain::ports::{AuthzHints, BlobStorage};
use crate::domain::upload_key::sanitize_component;

const META_TENANT: &str = "tenant";
const META_VIEWER: &str = "viewer";
const META_OWNER: &str = "owner";
const S3_TAG_VALUE_MAX_LEN: usize = 256;
const MAX_ALLOWED_DOWNLOAD_SIZE: i64 = 50 * 1024 * 1024;

/// Object storage accessor handling multi-tenant keys and tag values.
pub struct S3Adapter {
    client: Client,
    bucket: String,
}

impl S3Adapter {
    /// Confirms network links and bucket visibility metrics.
    pub async fn new(
        endpoint: &str,
        bucket: &str,
        region: &str,
        access_key: &str,
        secret_key: &str,
    ) -> Result<Self> {
        let timeout_config = TimeoutConfig::builder()
            .connect_timeout(Duration::from_secs(5))
            .operation_timeout(Duration::from_secs(30))
            .operation_attempt_timeout(Duration::from_secs(10))
            .build();

        let config = aws_config::from_env()
            .endpoint_url(endpoint)
            .region(Region::new(region.to_string()))
            .timeout_config(timeout_config)
            .load()
            .await;

        let credentials =
            Credentials::new(access_key, secret_key, None, None, "static");
        let s3_config = aws_sdk_s3::config::Builder::from(&config)
            .credentials_provider(credentials)
            .force_path_style(true)
            .build();

        let client = Client::from_conf(s3_config);

        tokio::time::timeout(
            Duration::from_secs(5),
            client.list_objects_v2().bucket(bucket).max_keys(1).send(),
        )
        .await
        .map_err(|_| {
            anyhow!("Timeout reached while connecting to S3 bucket {bucket:?}")
        })?
        .context(format!("Bucket {bucket:?} not reachable"))?;

        Ok(Self {
            client,
            bucket: bucket.to_string(),
        })
    }

    fn normalize_kv(map: &HashMap<String, String>) -> HashMap<String, String> {
        map.iter()
            .map(|(k, v)| (k.trim().to_lowercase(), v.trim().to_string()))
            .collect()
    }

    fn s3_tagging_query(authz: &AuthzHints) -> Result<String> {
        let mut parts = Vec::with_capacity(3);
        let enc = |s: &str| urlencoding::encode(s.trim()).into_owned();

        if let Some(t) =
            authz.tenant.as_deref().filter(|x| !x.trim().is_empty())
        {
            let val = enc(t);
            if val.len() > S3_TAG_VALUE_MAX_LEN {
                bail!("S3 tag size for 'tenant' violates AWS constraints");
            }
            parts.push(format!("{}={}", META_TENANT, val));
        }
        if let Some(o) =
            authz.owner.as_deref().filter(|x| !x.trim().is_empty())
        {
            let val = enc(o);
            if val.len() > S3_TAG_VALUE_MAX_LEN {
                bail!("S3 tag size for 'owner' violates AWS constraints");
            }
            parts.push(format!("{}={}", META_OWNER, val));
        }
        if let Some(vcsv) = authz.viewers_csv() {
            let val = enc(&vcsv);
            if val.len() > S3_TAG_VALUE_MAX_LEN {
                bail!("S3 tag size for 'viewer' CSV violates AWS constraints");
            }
            parts.push(format!("{}={}", META_VIEWER, val));
        }

        Ok(parts.join("&"))
    }

    fn resolve_tenant_and_key(
        key: &str,
        authz: &AuthzHints,
    ) -> Result<(String, String)> {
        let key = key.trim().trim_start_matches('/');
        let path_tenant = key.split_once('/').map(|(tenant, _)| tenant);
        let header_tenant = authz
            .tenant
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty());

        match (header_tenant, path_tenant) {
            (Some(header), Some(path))
                if !header.to_lowercase().eq(&path.to_lowercase()) =>
            {
                bail!("Tenant mismatch: header {header:?}, path {path:?}");
            },
            (Some(header), Some(_)) => Ok((header.to_owned(), key.to_owned())),
            (Some(header), None) => {
                Ok((header.to_owned(), format!("{header}/{key}")))
            },
            (None, Some(path)) => Ok((path.to_owned(), key.to_owned())),
            (None, None) => bail!(
                "No tenancy parameters recovered from header or storage paths"
            ),
        }
    }

    fn sanitize_key(key: &str) -> Result<()> {
        let key = key.trim().trim_start_matches('/');
        if key.is_empty() {
            bail!("S3 key is empty");
        }

        if key.contains("../") || key.contains("..\\") || key.contains('\0') {
            bail!("Malicious path segments or null bytes detected in S3 key");
        }

        let segments: Vec<&str> = key.split('/').collect();
        if segments.is_empty() {
            bail!("S3 key has no valid structural segments");
        }

        sanitize_component(segments[0], 64, true)?;

        for segment in segments.iter().skip(1) {
            if segment.is_empty() {
                continue;
            }
            sanitize_component(segment, 256, false)?;
        }

        Ok(())
    }
}

#[async_trait]
impl BlobStorage for S3Adapter {
    async fn upload_file(
        &self,
        file_name: &str,
        data: &[u8],
    ) -> Result<String> {
        Self::sanitize_key(file_name)?;

        let body = ByteStream::from(Bytes::copy_from_slice(data));
        self.client
            .put_object()
            .bucket(&self.bucket)
            .key(file_name)
            .body(body)
            .send()
            .await
            .context(format!("Failed to upload {file_name:?}"))?;

        Ok(format!("s3://{}/{file_name}", self.bucket))
    }

    async fn upload_file_with_authz(
        &self,
        key: &str,
        data: &[u8],
        authz: &AuthzHints,
    ) -> Result<String> {
        Self::sanitize_key(key)?;
        let (tenant, resolved_key) = Self::resolve_tenant_and_key(key, authz)?;
        Self::sanitize_key(&resolved_key)?;

        let mut authz = authz.clone();
        authz.tenant = Some(tenant.clone());

        let body = ByteStream::from(Bytes::copy_from_slice(data));
        let mut request = self
            .client
            .put_object()
            .bucket(&self.bucket)
            .key(&resolved_key)
            .body(body)
            .metadata(META_TENANT, &tenant);

        if let Some(owner) = authz
            .owner
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            request = request.metadata(META_OWNER, owner);
        }
        if let Some(viewers) = authz.viewers_csv() {
            request = request.metadata(META_VIEWER, viewers);
        }

        let tagging = Self::s3_tagging_query(&authz)?;
        if !tagging.is_empty() {
            request = request.tagging(tagging);
        }

        request.send().await.with_context(|| {
            format!("Failed to upload validated object {:?}", resolved_key)
        })?;

        Ok(format!("s3://{}/{}", self.bucket, resolved_key))
    }

    async fn download_file(&self, key: &str) -> Result<Vec<u8>> {
        Self::sanitize_key(key)?;

        let response = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await?;

        if let Some(content_length) = response.content_length() {
            if content_length > MAX_ALLOWED_DOWNLOAD_SIZE {
                bail!(
                    "Object size ({content_length} bytes) exceeds maximum security threshold of {MAX_ALLOWED_DOWNLOAD_SIZE} bytes"
                );
            }
        } else {
            bail!(
                "Missing Content-Length metadata header from S3 response; download rejected for security isolation"
            );
        }

        let bytes = response.body.collect().await?.into_bytes().to_vec();
        Ok(bytes)
    }

    async fn authz_hints(
        &self,
        bucket: &str,
        key: &str,
    ) -> Result<AuthzHints> {
        if bucket != self.bucket {
            bail!("Access denied: bucket execution context mismatch");
        }
        Self::sanitize_key(key)?;

        let head = self
            .client
            .head_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await
            .context("head_object failed")?;

        let empty_map = HashMap::new();
        let meta_raw = head.metadata().unwrap_or(&empty_map);
        let meta = Self::normalize_kv(meta_raw);

        let viewers_from_meta = meta
            .get(META_VIEWER)
            .map(|s| {
                s.split(',')
                    .map(str::trim)
                    .filter(|x| !x.is_empty())
                    .map(String::from)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();

        let tags = match self
            .client
            .get_object_tagging()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await
        {
            Ok(resp) => resp
                .tag_set()
                .iter()
                .map(|t| {
                    (
                        t.key().trim().to_lowercase(),
                        t.value().trim().to_string(),
                    )
                })
                .collect::<HashMap<_, _>>(),
            Err(_) => HashMap::new(),
        };

        let tenant = meta
            .get(META_TENANT)
            .cloned()
            .or_else(|| tags.get(META_TENANT).cloned());
        let owner = meta
            .get(META_OWNER)
            .cloned()
            .or_else(|| tags.get(META_OWNER).cloned());

        let mut viewers = viewers_from_meta;
        if let Some(tag_viewer) = tags.get(META_VIEWER) {
            viewers.extend(
                tag_viewer
                    .split(',')
                    .map(str::trim)
                    .filter(|x| !x.is_empty())
                    .map(String::from),
            );
        }

        Ok(AuthzHints {
            tenant,
            viewers,
            owner,
        })
    }

    async fn list_objects(&self, prefix: &str) -> Result<Vec<String>> {
        Self::sanitize_key(prefix)?;

        let resp = self
            .client
            .list_objects_v2()
            .bucket(&self.bucket)
            .prefix(prefix)
            .send()
            .await
            .context("Failed to list S3 objects")?;

        let mut keys = Vec::new();
        if let Some(contents) = resp.contents {
            for obj in contents {
                if let Some(k) = obj.key {
                    keys.push(k);
                }
            }
        }
        Ok(keys)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn s3_tagging_query_empty_when_no_fields() {
        let h = AuthzHints {
            tenant: None,
            viewers: vec![],
            owner: None,
        };
        assert_eq!(S3Adapter::s3_tagging_query(&h).unwrap(), "");
    }

    #[test]
    fn s3_tagging_query_encodes_and_joins() {
        let h = AuthzHints {
            tenant: Some("tenant:acme".to_string()),
            viewers: vec!["user:alice".to_string(), "user:bob".to_string()],
            owner: Some("user:alice".to_string()),
        };
        let q = S3Adapter::s3_tagging_query(&h).unwrap();
        assert!(q.contains("tenant=tenant%3Aacme"));
        assert!(q.contains("owner=user%3Aalice"));
        assert!(q.contains("viewer=user%3Aalice%2Cuser%3Abob"));
    }

    #[test]
    fn s3_tagging_query_rejects_huge_csv_value() {
        let huge_viewers = (0..40)
            .map(|i| format!("user:long_username_id_{i}"))
            .collect();
        let h = AuthzHints {
            tenant: None,
            viewers: huge_viewers,
            owner: None,
        };
        assert!(S3Adapter::s3_tagging_query(&h).is_err());
    }

    #[test]
    fn test_resolve_tenant_and_key_matching() {
        let hints = AuthzHints {
            tenant: Some("acme".to_string()),
            viewers: vec![],
            owner: None,
        };
        let (tenant, key) =
            S3Adapter::resolve_tenant_and_key("acme/files/doc.pdf", &hints)
                .unwrap();
        assert_eq!(tenant, "acme");
        assert_eq!(key, "acme/files/doc.pdf");
    }

    #[test]
    fn test_resolve_tenant_mismatch_error() {
        let hints = AuthzHints {
            tenant: Some("acme".to_string()),
            viewers: vec![],
            owner: None,
        };
        assert!(
            S3Adapter::resolve_tenant_and_key(
                "alternate/files/doc.pdf",
                &hints
            )
            .is_err()
        );
    }

    #[test]
    fn test_sanitize_key_with_valid_paths() {
        assert!(S3Adapter::sanitize_key("tenant1/group1/file.json").is_ok());
        assert!(S3Adapter::sanitize_key("acme_tenant/config.yaml").is_ok());
    }

    #[test]
    fn test_sanitize_key_rejects_path_traversal() {
        assert!(S3Adapter::sanitize_key("tenant1/../../etc/passwd").is_err());
        assert!(S3Adapter::sanitize_key("..\\..\\secret.txt").is_err());
    }

    #[test]
    fn test_sanitize_key_rejects_null_bytes() {
        assert!(S3Adapter::sanitize_key("tenant1/file\0name.bin").is_err());
    }

    #[test]
    fn test_resolve_tenant_and_key_case_insensitivity_handling() {
        let hints = AuthzHints {
            tenant: Some("AcMe".to_string()),
            viewers: vec![],
            owner: None,
        };
        let (tenant, key) =
            S3Adapter::resolve_tenant_and_key("acme/files/doc.pdf", &hints)
                .unwrap();
        assert_eq!(tenant, "AcMe");
        assert_eq!(key, "acme/files/doc.pdf");
    }

    #[test]
    fn test_sanitize_key_enforces_strict_tenant_boundaries() {
        assert!(
            S3Adapter::sanitize_key("invalid.tenant/group/file.txt").is_err()
        );

        let huge_tenant = "a".repeat(65);
        assert!(
            S3Adapter::sanitize_key(&format!("{huge_tenant}/group/file.txt"))
                .is_err()
        );
    }

    #[test]
    fn test_sanitize_key_rejects_reserved_os_names() {
        assert!(S3Adapter::sanitize_key("CON/group/file.txt").is_err());
        assert!(S3Adapter::sanitize_key("tenant1/NUL/file.txt").is_err());
    }
}

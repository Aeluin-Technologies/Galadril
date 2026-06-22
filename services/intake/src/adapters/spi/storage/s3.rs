//! Amazon S3 (or all S3-like) adapter.

use std::collections::HashMap;

use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use aws_config::Region;
use aws_sdk_s3::Client;
use aws_sdk_s3::config::Credentials;
use aws_sdk_s3::primitives::ByteStream;

use crate::domain::ports::{AuthzHints, BlobStorage};

const META_TENANT: &str = "tenant";
const META_VIEWER: &str = "viewer";
const META_OWNER: &str = "owner";
const S3_TAG_VALUE_MAX_LEN: usize = 256;

pub struct S3Adapter {
    client: Client,
    bucket: String,
}

impl S3Adapter {
    /// Create a new [`S3Adapter`].
    pub async fn new(
        endpoint: &str,
        bucket: &str,
        region: &str,
        access_key: &str,
        secret_key: &str,
    ) -> Result<Self> {
        let config = aws_config::from_env()
            .endpoint_url(endpoint)
            .region(Region::new(region.to_string()))
            .load()
            .await;

        let credentials =
            Credentials::new(access_key, secret_key, None, None, "static");

        let s3_config = aws_sdk_s3::config::Builder::from(&config)
            .credentials_provider(credentials)
            .force_path_style(true)
            .build();

        let client = Client::from_conf(s3_config);

        client
            .list_objects_v2()
            .bucket(bucket)
            .max_keys(1)
            .send()
            .await
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

    /// Builds a URL-encoded query-string tag payload for S3 `.tagging(...)`.
    ///
    /// # Errors
    /// Returns an error if any generated tag value exceeds the 256-byte AWS S3
    /// limit, preventing a potential DoS on the upload.
    fn s3_tagging_query(authz: &AuthzHints) -> Result<String> {
        fn enc(s: &str) -> String {
            urlencoding::encode(s.trim()).into_owned()
        }

        let mut parts: Vec<String> = Vec::with_capacity(3);

        if let Some(t) =
            authz.tenant.as_deref().filter(|x| !x.trim().is_empty())
        {
            let val = enc(t);
            if val.len() > S3_TAG_VALUE_MAX_LEN {
                bail!(
                    "S3 tag value for 'tenant' exceeds the maximum allowed 256 bytes"
                );
            }
            parts.push(format!("{}={}", META_TENANT, val));
        }
        if let Some(o) =
            authz.owner.as_deref().filter(|x| !x.trim().is_empty())
        {
            let val = enc(o);
            if val.len() > S3_TAG_VALUE_MAX_LEN {
                bail!(
                    "S3 tag value for 'owner' exceeds the maximum allowed 256 bytes"
                );
            }
            parts.push(format!("{}={}", META_OWNER, val));
        }
        if let Some(vcsv) = authz.viewers_csv() {
            let val = enc(&vcsv);
            if val.len() > S3_TAG_VALUE_MAX_LEN {
                bail!(
                    "S3 tag value for 'viewer' (CSV) exceeds the maximum allowed 256 bytes"
                );
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
                if !header.eq_ignore_ascii_case(path) =>
            {
                bail!("Tenant mismatch: header {header:?}, path {path:?}");
            },
            (Some(header), Some(_)) => Ok((header.to_owned(), key.to_owned())),

            (Some(header), None) => {
                Ok((header.to_owned(), format!("{header}/{key}")))
            },
            (None, Some(path)) => Ok((path.to_owned(), key.to_owned())),
            (None, None) => {
                bail!(
                    "No tenant provided in authorization header or storage key"
                );
            },
        }
    }
}

#[async_trait]
impl BlobStorage for S3Adapter {
    async fn upload_file(
        &self,
        file_name: &str,
        data: &[u8],
    ) -> Result<String> {
        let body = ByteStream::from(data.to_vec());
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
        let (tenant, key) = Self::resolve_tenant_and_key(key, authz)?;

        let mut authz = authz.clone();
        authz.tenant = Some(tenant.clone());

        let mut request = self
            .client
            .put_object()
            .bucket(&self.bucket)
            .key(&key)
            .body(ByteStream::from(data.to_vec()))
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
            format!("Failed to upload validated object {:?}", key)
        })?;

        Ok(format!("s3://{}/{}", self.bucket, key))
    }

    async fn download_file(&self, key: &str) -> Result<Vec<u8>> {
        let response = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await?;
        let bytes = response.body.collect().await?.into_bytes().to_vec();
        Ok(bytes)
    }

    async fn authz_hints(
        &self,
        bucket: &str,
        key: &str,
    ) -> Result<AuthzHints> {
        let head = self
            .client
            .head_object()
            .bucket(bucket)
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
                    .map(|x| x.trim())
                    .filter(|x| !x.is_empty())
                    .map(|x| x.to_string())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();

        // Note: some S3-like systems may not support it.
        let tags = match self
            .client
            .get_object_tagging()
            .bucket(bucket)
            .key(key)
            .send()
            .await
        {
            Ok(resp) => resp
                .tag_set()
                .iter()
                .map(|t| {
                    let k = t.key().trim().to_lowercase();
                    let v = t.value().trim().to_string();
                    (k, v)
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
                    .map(|x| x.trim())
                    .filter(|x| !x.is_empty())
                    .map(|x| x.to_string()),
            );
        }

        Ok(AuthzHints {
            tenant,
            viewers,
            owner,
        })
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
        assert!(q.contains('&'));
    }

    #[test]
    fn s3_tagging_query_rejects_huge_csv_value() {
        let huge_viewers = (0..30)
            .map(|i| format!("user:long_username_id_{i}"))
            .collect();
        let h = AuthzHints {
            tenant: None,
            viewers: huge_viewers,
            owner: None,
        };
        let res = S3Adapter::s3_tagging_query(&h);
        assert!(res.is_err());
    }
}

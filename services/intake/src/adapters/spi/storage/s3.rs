//! Amazon S3 (or all S3-like) adapter.

use std::collections::HashMap;

use anyhow::{Context, Result};
use async_trait::async_trait;
use aws_sdk_s3::Client;
use aws_sdk_s3::primitives::ByteStream;

use crate::domain::ports::{AuthzHints, BlobStorage};

const META_TENANT: &str = "tenant";
const META_VIEWER: &str = "viewer";
const META_OWNER: &str = "owner";

pub struct S3Adapter {
    client: Client,
    bucket: String,
}

impl S3Adapter {
    /// Create a new [`S3Adapter`].
    pub async fn new(endpoint: &str, bucket: &str) -> Result<Self> {
        let config =
            aws_config::from_env().endpoint_url(endpoint).load().await;

        let s3_config = aws_sdk_s3::config::Builder::from(&config)
            .force_path_style(true)
            .build();

        let client = Client::from_conf(s3_config);

        client
            .head_bucket()
            .bucket(bucket)
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

        let meta_raw = if let Some(meta_raw) = head.metadata() {
            meta_raw
        } else {
            &HashMap::new()
        };
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

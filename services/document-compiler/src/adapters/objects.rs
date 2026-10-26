//! S3 stores immutable artifacts; libSQL never stores templates or PDF bytes.
use anyhow::{Result, ensure};
use aws_sdk_s3::Client;
use aws_sdk_s3::primitives::ByteStream;
use sha2::{Digest, Sha256};

use crate::ports::ObjectStore;

pub struct S3Objects {
    client: Client,
    bucket: String,
}
impl S3Objects {
    pub async fn probe(&self) -> Result<()> {
        self.client
            .head_bucket()
            .bucket(&self.bucket)
            .send()
            .await?;
        Ok(())
    }

    pub fn new(client: Client, bucket: String) -> Result<Self> {
        ensure!(
            !bucket.is_empty() && bucket.len() <= 63,
            "Invalid artifact bucket"
        );
        Ok(Self { client, bucket })
    }
}

#[async_trait::async_trait]
impl ObjectStore for S3Objects {
    async fn get(&self, key: &str, limit: usize) -> Result<Vec<u8>> {
        let mut object = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await?;
        ensure!(
            object
                .content_length()
                .is_some_and(|size| size >= 0 && size <= limit as i64),
            "Artifact exceeds limit"
        );
        let mut bytes =
            Vec::with_capacity(object.content_length().unwrap_or(0) as usize);
        while let Some(chunk) = object.body.try_next().await? {
            ensure!(
                bytes.len() + chunk.len() <= limit,
                "Artifact exceeds limit"
            );
            bytes.extend_from_slice(&chunk);
        }
        Ok(bytes)
    }

    async fn exists(&self, key: &str) -> Result<bool> {
        match self
            .client
            .head_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await
        {
            Ok(_) => Ok(true),
            Err(error)
                if error
                    .as_service_error()
                    .is_some_and(|error| error.is_not_found()) =>
            {
                Ok(false)
            },
            Err(error) => Err(error.into()),
        }
    }

    /// Conditional writes preserve revisions across concurrent Gateway
    /// replicas.
    async fn put_new(&self, key: &str, data: Vec<u8>) -> Result<()> {
        let length = data.len();
        let digest = Sha256::digest(&data);
        match self
            .client
            .put_object()
            .bucket(&self.bucket)
            .key(key)
            .if_none_match("*")
            .set_tagging(
                key.contains("/jobs/")
                    .then(|| "galadril-retention=seven-days".to_owned()),
            )
            .body(ByteStream::from(data))
            .send()
            .await
        {
            Ok(_) => Ok(()),
            Err(error)
                if error.raw_response().is_some_and(|response| {
                    response.status().as_u16() == 412
                }) =>
            {
                ensure!(
                    Sha256::digest(self.get(key, length).await?) == digest,
                    "Immutable artifact conflict"
                );
                Ok(())
            },
            Err(error) => Err(error.into()),
        }
    }

    async fn delete(&self, key: &str) -> Result<()> {
        self.client
            .delete_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await?;
        let versions = self
            .client
            .list_object_versions()
            .bucket(&self.bucket)
            .prefix(key)
            .send()
            .await?;
        ensure!(
            versions.is_truncated() != Some(true),
            "Artifact version limit exceeded"
        );
        for version in versions
            .versions()
            .iter()
            .filter(|version| version.key() == Some(key))
        {
            if let Some(id) = version.version_id() {
                self.client
                    .delete_object()
                    .bucket(&self.bucket)
                    .key(key)
                    .version_id(id)
                    .send()
                    .await?;
            }
        }
        for version in versions
            .delete_markers()
            .iter()
            .filter(|version| version.key() == Some(key))
        {
            if let Some(id) = version.version_id() {
                self.client
                    .delete_object()
                    .bucket(&self.bucket)
                    .key(key)
                    .version_id(id)
                    .send()
                    .await?;
            }
        }
        Ok(())
    }

    async fn list(
        &self,
        prefix: &str,
        after: &str,
    ) -> Result<(Vec<String>, String)> {
        ensure!(
            after.is_empty() || after.starts_with(prefix),
            "Invalid catalog cursor"
        );
        let result = self
            .client
            .list_objects_v2()
            .bucket(&self.bucket)
            .prefix(prefix)
            .max_keys(50)
            .set_start_after((!after.is_empty()).then(|| after.to_owned()))
            .send()
            .await?;
        let keys: Vec<String> = result
            .contents()
            .iter()
            .filter_map(|object| object.key().map(str::to_owned))
            .collect();
        let cursor = if result.is_truncated() == Some(true) {
            keys.last().cloned().unwrap_or_default()
        } else {
            String::new()
        };
        Ok((keys, cursor))
    }
}

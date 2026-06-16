//! S3 adapter used by Gateway uploads via presigned URLs.

use std::time::Duration;

use anyhow::{Context, Result};
use aws_config::Region;
use aws_sdk_s3::Client;
use aws_sdk_s3::config::Credentials;
use aws_sdk_s3::presigning::PresigningConfig;

pub struct S3Uploader {
    client: Client,
    staging_bucket: String,
    destination_bucket: String,
}

impl S3Uploader {
    /// Creates a new uploader configured for S3-compatible endpoints.
    pub async fn new(
        endpoint: &str,
        staging_bucket: &str,
        destination_bucket: &str,
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

        // Verify reachability of both critical storage buckets.
        client
            .head_bucket()
            .bucket(staging_bucket)
            .send()
            .await
            .context(format!(
                "Staging bucket {staging_bucket:?} not reachable"
            ))?;

        client
            .head_bucket()
            .bucket(destination_bucket)
            .send()
            .await
            .context(format!(
                "Destination bucket {destination_bucket:?} not reachable"
            ))?;

        Ok(Self {
            client,
            staging_bucket: staging_bucket.to_string(),
            destination_bucket: destination_bucket.to_string(),
        })
    }

    /// Generates a temporary presigned PUT URL targeting the staging bucket.
    pub async fn generate_presigned_upload_url(
        &self,
        key: &str,
        expires_in: Duration,
    ) -> Result<String> {
        let presigned_req = self
            .client
            .put_object()
            .bucket(&self.staging_bucket)
            .key(key)
            .presigned(PresigningConfig::expires_in(expires_in)?)
            .await
            .context("Failed to generate presigned PUT URL")?;

        Ok(presigned_req.uri().to_string())
    }

    /// Atomically copies object to destination bucket with new tags, then
    /// purges staging source.
    pub async fn finalize_object(
        &self,
        staging_key: &str,
        dest_key: &str,
        tagging_query: Option<&str>,
    ) -> Result<()> {
        let source = format!("{}/{}", self.staging_bucket, staging_key);
        let mut copy_req = self
            .client
            .copy_object()
            .bucket(&self.destination_bucket)
            .key(dest_key)
            .copy_source(source);

        if let Some(tags) = tagging_query.filter(|s| !s.trim().is_empty()) {
            copy_req = copy_req.tagging(tags);
            copy_req = copy_req.tagging_directive(
                aws_sdk_s3::types::TaggingDirective::Replace,
            );
        }

        copy_req.send().await.context(
            "Failed to copy object from staging to destination bucket",
        )?;

        self.client
            .delete_object()
            .bucket(&self.staging_bucket)
            .key(staging_key)
            .send()
            .await
            .context(
                "Failed to delete object from staging bucket after copy",
            )?;

        Ok(())
    }
}

//! S3 adapter used by Gateway uploads via presigned URLs.

use std::time::Duration;

use anyhow::{Context, Result, bail};
use aws_config::Region;
use aws_sdk_s3::Client;
use aws_sdk_s3::config::Credentials;
use aws_sdk_s3::presigning::PresigningConfig;
use aws_sdk_s3::types::TaggingDirective;

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
            .region(Region::new(region.to_owned()))
            .load()
            .await;

        let credentials =
            Credentials::new(access_key, secret_key, None, None, "static");

        let client = Client::from_conf(
            aws_sdk_s3::config::Builder::from(&config)
                .credentials_provider(credentials)
                .force_path_style(true)
                .build(),
        );

        // Verify reachability of both critical storage buckets.
        Self::verify_bucket(&client, staging_bucket).await?;
        Self::verify_bucket(&client, destination_bucket).await?;

        Ok(Self {
            client,
            staging_bucket: staging_bucket.to_owned(),
            destination_bucket: destination_bucket.to_owned(),
        })
    }

    async fn verify_bucket(client: &Client, bucket: &str) -> Result<()> {
        client
            .head_bucket()
            .bucket(bucket)
            .send()
            .await
            .with_context(|| format!("Bucket {bucket:?} is not reachable"))?;

        Ok(())
    }

    /// Generates a temporary presigned PUT URL targeting the staging bucket.
    pub async fn generate_presigned_upload_url(
        &self,
        key: &str,
        expires_in: Duration,
    ) -> Result<String> {
        Ok(self
            .client
            .put_object()
            .bucket(&self.staging_bucket)
            .key(key)
            .presigned(PresigningConfig::expires_in(expires_in)?)
            .await
            .context("Failed to generate presigned PUT URL")?
            .uri()
            .to_string())
    }

    /// Resolves destination key and validates tenant isolation rules.
    fn resolve_destination_key(
        dest_key: &str,
        tenant: Option<&str>,
    ) -> Result<String> {
        let key = dest_key.trim().trim_start_matches('/');
        let path_tenant = key.split_once('/').map(|(tenant, _)| tenant);
        let tenant = tenant.map(str::trim).filter(|value| !value.is_empty());

        match (tenant, path_tenant) {
            (Some(expected), Some(actual))
                if !expected.eq_ignore_ascii_case(actual) =>
            {
                bail!(
                    "Tenant mismatch in finalize operation. Context claims {expected:?}, but target path requests {actual:?}",
                );
            },
            (Some(_), Some(_)) | (None, Some(_)) => Ok(key.to_owned()),
            (Some(tenant), None) => Ok(format!("{tenant}/{key}")),
            (None, None) => {
                bail!("Object cannot be moved without a tenant context.");
            },
        }
    }

    async fn copy_to_destination(
        &self,
        staging_key: &str,
        destination_key: &str,
        tagging_query: Option<&str>,
    ) -> Result<()> {
        let source = format!("{}/{}", self.staging_bucket, staging_key);

        let mut request = self
            .client
            .copy_object()
            .bucket(&self.destination_bucket)
            .key(destination_key)
            .copy_source(source);

        if let Some(tags) =
            tagging_query.map(str::trim).filter(|tags| !tags.is_empty())
        {
            request = request
                .tagging(tags)
                .tagging_directive(TaggingDirective::Replace);
        }

        request.send().await.with_context(|| {
            format!(
                "Failed to copy object from staging to destination path {:?}",
                destination_key,
            )
        })?;

        Ok(())
    }

    async fn delete_staging_object(&self, staging_key: &str) -> Result<()> {
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

    /// Atomically copies object to destination bucket with new tags, then
    /// purges staging source. Validates tenant multi-tenancy rules before
    /// execution.
    pub async fn finalize_object(
        &self,
        staging_key: &str,
        dest_key: &str,
        tenant: Option<&str>,
        tagging_query: Option<&str>,
    ) -> Result<String> {
        let destination_key = Self::resolve_destination_key(dest_key, tenant)?;

        self.copy_to_destination(staging_key, &destination_key, tagging_query)
            .await?;

        self.delete_staging_object(staging_key).await?;

        Ok(destination_key)
    }
}

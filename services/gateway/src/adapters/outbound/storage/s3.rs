//! S3 adapter used by Gateway uploads via presigned URLs.

use std::time::Duration;

use anyhow::{Context, Result, bail};
use aws_config::Region;
use aws_sdk_s3::Client;
use aws_sdk_s3::config::Credentials;
use aws_sdk_s3::presigning::PresigningConfig;
use aws_sdk_s3::primitives::ByteStream;
use aws_sdk_s3::types::{MetadataDirective, TaggingDirective};

use crate::application::ports::attachment_store::AttachmentStore;
use crate::application::ports::conversation_agent::AgentAttachment;
use crate::application::ports::conversation_store::{
    AttachmentKind, MessageAttachment,
};
use crate::application::ports::pipeline_publisher::PipelinePublisher;
use crate::application::ports::upload_store::{
    UploadFinalization, UploadStore,
};

const MAX_ATTACHMENT_BYTES: i64 = 25 * 1024 * 1024;

pub struct S3Uploader {
    client: Client,
    staging_bucket: String,
    destination_bucket: String,
    config_bucket: String,
}

impl S3Uploader {
    /// Creates a new uploader configured for S3-compatible endpoints.
    pub async fn new(
        endpoint: &str,
        staging_bucket: &str,
        destination_bucket: &str,
        config_bucket: &str,
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
        Self::verify_bucket(&client, config_bucket).await?;

        Ok(Self {
            client,
            staging_bucket: staging_bucket.to_owned(),
            destination_bucket: destination_bucket.to_owned(),
            config_bucket: config_bucket.to_owned(),
        })
    }

    /// Fails startup when a required storage bucket is unavailable.
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

    /// Validates that a staging object key belongs to the provided tenant
    /// and authenticated user.
    fn validate_staging_key(
        key: &str,
        tenant: &str,
        user: &str,
    ) -> Result<()> {
        let key = key.trim().trim_start_matches('/');

        let mut parts = key.split('/');

        let key_tenant = parts.next();
        let key_user = parts.next();

        match (key_tenant, key_user) {
            (Some(actual_tenant), Some(actual_user))
                if actual_tenant == tenant && actual_user == user =>
            {
                Ok(())
            },
            _ => {
                bail!(
                    "Staging object does not belong to authenticated tenant/user"
                )
            },
        }
    }

    /// Resolves destination key and validates tenant isolation rules.
    fn resolve_destination_key(
        dest_key: &str,
        tenant: &str,
    ) -> Result<String> {
        let key = dest_key.trim().trim_start_matches('/');
        let path_tenant = key.split_once('/').map(|(tenant, _)| tenant);

        match path_tenant {
            Some(actual) if tenant != actual => {
                bail!(
                    "Tenant mismatch in finalize operation. Context claims {tenant:?}, but target path requests {actual:?}",
                );
            },
            Some(_) => Ok(key.to_owned()),
            None => Ok(format!("{tenant}/{key}")),
        }
    }

    /// Copies one staged object while replacing untrusted metadata and tags.
    async fn copy_to_destination(
        &self,
        request: &UploadFinalization<'_>,
        destination_key: &str,
    ) -> Result<()> {
        let source =
            format!("{}/{}", self.staging_bucket, request.staging_key);

        let mut copy = self
            .client
            .copy_object()
            .bucket(&self.destination_bucket)
            .key(destination_key)
            .copy_source(source)
            .metadata_directive(MetadataDirective::Replace)
            .metadata("tenant", request.tenant_id)
            .metadata("owner", request.user_id)
            .metadata("authz-origin", "gateway")
            .metadata("authz-permission", "ingest")
            .metadata("authz-resource", format!("raw:{destination_key}"))
            .metadata("authz-delegation-id", request.delegation_id);

        let issuer = request.authn_issuer.trim();
        if !issuer.is_empty() {
            copy = copy.metadata("authz-issuer", issuer);
        }

        if let Some(tags) = request
            .tagging_query
            .map(str::trim)
            .filter(|tags| !tags.is_empty())
        {
            copy = copy
                .tagging(tags)
                .tagging_directive(TaggingDirective::Replace);
        }

        copy.send().await.with_context(|| {
            format!(
                "Failed to copy object from staging to destination path {:?}",
                destination_key,
            )
        })?;

        Ok(())
    }

    /// Deletes a staging object only after its destination copy succeeds.
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

    /// Rejects object keys that are outside the exact tenant namespace.
    fn validate_tenant_object_key<'a>(
        key: &'a str,
        tenant_id: &str,
    ) -> Result<&'a str> {
        let normalized = key.trim().trim_start_matches('/');
        match normalized.split_once('/') {
            Some((tenant, _)) if tenant == tenant_id => Ok(normalized),
            _ => {
                bail!("Attachment object is outside the authenticated tenant")
            },
        }
    }

    /// Validates bounded S3 metadata against the declared Scribe media kind.
    fn validate_attachment_metadata(
        attachment: &MessageAttachment,
        content_type: Option<&str>,
        content_length: Option<i64>,
    ) -> Result<()> {
        let content_length = content_length
            .context("Attachment is missing its S3 content length")?;
        if !(1..=MAX_ATTACHMENT_BYTES).contains(&content_length) {
            bail!(
                "Attachment size must be between 1 and {MAX_ATTACHMENT_BYTES} bytes"
            );
        }
        let content_type = content_type
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .context("Attachment is missing its S3 content type")?;
        let required_prefix = match attachment.kind {
            AttachmentKind::Image => "image/",
            AttachmentKind::Audio => "audio/",
        };
        if !content_type
            .get(..required_prefix.len())
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case(required_prefix))
        {
            bail!("Attachment content type does not match its media kind");
        }
        if let Some(expected) = attachment.size_bytes &&
            content_length != expected
        {
            bail!("Attachment size does not match durable object metadata");
        }
        if let Some(expected) = attachment.content_type.as_deref() &&
            !content_type.eq_ignore_ascii_case(expected.trim())
        {
            bail!(
                "Attachment media type does not match durable object metadata"
            );
        }
        Ok(())
    }

    /// Promotes an object with trusted metadata, then purges its staging copy.
    async fn finalize_object(
        &self,
        request: UploadFinalization<'_>,
    ) -> Result<String> {
        Self::validate_staging_key(
            request.staging_key,
            request.tenant_id,
            request.user_id,
        )?;

        let destination_key = Self::resolve_destination_key(
            request.destination_key,
            request.tenant_id,
        )?;

        self.copy_to_destination(&request, &destination_key).await?;
        self.delete_staging_object(request.staging_key).await?;

        Ok(destination_key)
    }
}

#[async_trait::async_trait]
impl UploadStore for S3Uploader {
    /// Generates a temporary presigned PUT URL targeting the staging bucket.
    async fn presign_upload(
        &self,
        key: &str,
        expires_in: Duration,
    ) -> Result<String> {
        self.generate_presigned_upload_url(key, expires_in).await
    }

    /// Promotes one authenticated object into the tenant destination prefix.
    async fn finalize_upload(
        &self,
        request: UploadFinalization<'_>,
    ) -> Result<String> {
        self.finalize_object(request).await
    }
}

#[async_trait::async_trait]
impl AttachmentStore for S3Uploader {
    /// Validates tenant metadata and returns read-only, short-lived S3 URLs.
    async fn resolve_for_scribe(
        &self,
        tenant_id: &str,
        _user_id: &str,
        attachments: &[MessageAttachment],
        expires_in: Duration,
    ) -> Result<Vec<AgentAttachment>> {
        let presigning = PresigningConfig::expires_in(expires_in)?;
        let mut resolved = Vec::with_capacity(attachments.len());
        for attachment in attachments {
            let object_key = Self::validate_tenant_object_key(
                &attachment.object_key,
                tenant_id,
            )?;
            let head = self
                .client
                .head_object()
                .bucket(&self.destination_bucket)
                .key(object_key)
                .send()
                .await
                .with_context(|| {
                    format!(
                        "Failed to validate attachment object {object_key:?}"
                    )
                })?;
            let metadata = head.metadata().context(
                "Attachment is missing Gateway authorization metadata",
            )?;
            if metadata.get("tenant").map(String::as_str) != Some(tenant_id) ||
                metadata.get("authz-origin").map(String::as_str) !=
                    Some("gateway")
            {
                bail!("Attachment authorization metadata is invalid");
            }
            Self::validate_attachment_metadata(
                attachment,
                head.content_type(),
                head.content_length(),
            )?;
            let url = self
                .client
                .get_object()
                .bucket(&self.destination_bucket)
                .key(object_key)
                .presigned(presigning.clone())
                .await
                .context("Failed to sign attachment download")?
                .uri()
                .to_string();
            resolved.push(AgentAttachment {
                kind: match attachment.kind {
                    AttachmentKind::Image => AttachmentKind::Image,
                    AttachmentKind::Audio => AttachmentKind::Audio,
                },
                url,
            });
        }
        Ok(resolved)
    }
}

#[async_trait::async_trait]
impl PipelinePublisher for S3Uploader {
    /// Writes JSON-as-YAML to the exact tenant prefix consumed by Intake.
    async fn publish(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
        definition: &serde_json::Value,
    ) -> Result<()> {
        if pipeline_id.is_empty() || pipeline_id.contains('/') {
            bail!("Invalid pipeline identifier for runtime publication");
        }
        let body = serde_json::to_vec(definition)
            .context("Failed to serialize pipeline definition")?;
        self.client
            .put_object()
            .bucket(&self.config_bucket)
            .key(format!("{tenant_id}/{pipeline_id}.yaml"))
            .content_type("application/yaml")
            .metadata("tenant", tenant_id)
            .metadata("pipeline", pipeline_id)
            .metadata("revision", revision_id)
            .body(ByteStream::from(body))
            .send()
            .await
            .context("Failed to publish pipeline runtime configuration")?;
        Ok(())
    }

    /// Removes the active configuration object from Intake discovery.
    async fn retire(&self, tenant_id: &str, pipeline_id: &str) -> Result<()> {
        if pipeline_id.is_empty() || pipeline_id.contains('/') {
            bail!("Invalid pipeline identifier for runtime retirement");
        }
        self.client
            .delete_object()
            .bucket(&self.config_bucket)
            .key(format!("{tenant_id}/{pipeline_id}.yaml"))
            .send()
            .await
            .context("Failed to retire pipeline runtime configuration")?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn attachment_keys_require_an_exact_tenant_prefix() {
        assert!(matches!(
            S3Uploader::validate_tenant_object_key(
                "tenant-a/chat/image.png",
                "tenant-a"
            ),
            Ok("tenant-a/chat/image.png")
        ));
        assert!(
            S3Uploader::validate_tenant_object_key(
                "tenant-ab/chat/image.png",
                "tenant-a"
            )
            .is_err()
        );
        assert!(
            S3Uploader::validate_tenant_object_key("image.png", "tenant-a")
                .is_err()
        );
    }

    #[test]
    fn staging_keys_require_exact_tenant_and_user_prefixes() {
        assert!(
            S3Uploader::validate_staging_key(
                "TenantA/user-a/upload",
                "TenantA",
                "user-a"
            )
            .is_ok()
        );
        assert!(
            S3Uploader::validate_staging_key(
                "tenanta/user-a/upload",
                "TenantA",
                "user-a"
            )
            .is_err()
        );
        assert!(
            S3Uploader::validate_staging_key(
                "TenantA/user-b/upload",
                "TenantA",
                "user-a"
            )
            .is_err()
        );
        assert!(
            S3Uploader::resolve_destination_key(
                "tenanta/default/file.bin",
                "TenantA"
            )
            .is_err()
        );
    }

    #[test]
    fn attachment_metadata_is_bounded_and_matches_media_kind() {
        let image = MessageAttachment {
            object_key: "tenant-a/chat/image.png".to_owned(),
            kind: AttachmentKind::Image,
            file_name: None,
            content_type: None,
            size_bytes: None,
        };
        assert!(
            S3Uploader::validate_attachment_metadata(
                &image,
                Some("image/png"),
                Some(1024)
            )
            .is_ok()
        );
        assert!(
            S3Uploader::validate_attachment_metadata(
                &image,
                Some("audio/mpeg"),
                Some(1024)
            )
            .is_err()
        );
        assert!(
            S3Uploader::validate_attachment_metadata(
                &image,
                Some("image/png"),
                Some(MAX_ATTACHMENT_BYTES + 1)
            )
            .is_err()
        );
        assert!(
            S3Uploader::validate_attachment_metadata(&image, None, Some(1))
                .is_err()
        );
    }
}

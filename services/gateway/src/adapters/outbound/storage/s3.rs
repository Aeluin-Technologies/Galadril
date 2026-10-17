//! S3 adapter used by Gateway uploads via presigned URLs.

use std::time::Duration;

use anyhow::{Context, Result, bail};
use aws_config::Region;
use aws_sdk_s3::Client;
use aws_sdk_s3::config::Credentials;
use aws_sdk_s3::presigning::PresigningConfig;
use aws_sdk_s3::types::{MetadataDirective, TaggingDirective};

use crate::application::ports::attachment_store::AttachmentStore;
use crate::application::ports::conversation_agent::AgentAttachment;
use crate::application::ports::conversation_store::{
    AttachmentKind, MessageAttachment,
};
use crate::application::ports::upload_store::{
    UploadFinalization, UploadStore,
};

const MAX_ATTACHMENT_BYTES: i64 = 25 * 1024 * 1024;

pub struct S3Uploader {
    client: Client,
    upload_client: Client,
    staging_bucket: String,
    destination_bucket: String,
}

impl S3Uploader {
    /// Creates a new uploader configured for S3-compatible endpoints.
    pub async fn new(
        endpoint: &str,
        public_endpoint: Option<&str>,
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
                .credentials_provider(credentials.clone())
                .force_path_style(true)
                .build(),
        );

        let upload_client = Client::from_conf(
            aws_sdk_s3::config::Builder::from(&config)
                .endpoint_url(public_endpoint.unwrap_or(endpoint))
                .credentials_provider(credentials)
                .force_path_style(true)
                .build(),
        );

        // Verify reachability of both critical storage buckets.
        Self::verify_bucket(&client, staging_bucket).await?;
        Self::verify_bucket(&client, destination_bucket).await?;

        Ok(Self {
            client,
            upload_client,
            staging_bucket: staging_bucket.to_owned(),
            destination_bucket: destination_bucket.to_owned(),
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

    /// Validates the staging capability against its exact destination scope.
    fn validate_staging_key(
        key: &str,
        tenant: &str,
        user: &str,
        group: &str,
    ) -> Result<()> {
        let key = key.trim().trim_start_matches('/');

        let mut parts = key.split('/');

        let key_tenant = parts.next();
        let key_user = parts.next();
        let key_group = parts.next();
        let nonce = parts.next();

        match (key_tenant, key_user, key_group, nonce, parts.next()) {
            (
                Some(actual_tenant),
                Some(actual_user),
                Some(actual_group),
                Some(nonce),
                None,
            ) if actual_tenant == tenant &&
                actual_user == urlencoding::encode(user) &&
                actual_group == group &&
                nonce.len() == 32 &&
                nonce.bytes().all(|byte| byte.is_ascii_hexdigit()) =>
            {
                Ok(())
            },
            _ => {
                bail!(
                    "Staging object does not match authenticated upload scope"
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
        anyhow::ensure!(
            key == key.trim() &&
                !key.starts_with('/') &&
                !key.split('/').any(|part| matches!(part, "" | "." | "..")),
            "Invalid attachment key"
        );
        let normalized = key;
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
            AttachmentKind::Document => "application/pdf",
        };
        if !(match attachment.kind {
            AttachmentKind::Document => {
                content_type.eq_ignore_ascii_case(required_prefix)
            },
            _ => content_type.get(..required_prefix.len()).is_some_and(
                |prefix| prefix.eq_ignore_ascii_case(required_prefix),
            ),
        }) {
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
        let destination_key = Self::resolve_destination_key(
            request.destination_key,
            request.tenant_id,
        )?;
        let group = destination_key
            .split('/')
            .nth(2)
            .filter(|value| !value.is_empty())
            .context("Destination raw group is missing")?;
        Self::validate_staging_key(
            request.staging_key,
            request.tenant_id,
            request.user_id,
            group,
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
            if metadata.get("purpose").map(String::as_str) == Some("pengolo") ||
                metadata.get("tenant").map(String::as_str) !=
                    Some(tenant_id) ||
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
                    AttachmentKind::Document => AttachmentKind::Document,
                },
                url,
            });
        }
        Ok(resolved)
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn document_attachments_require_pdf_metadata() {
        let attachment = MessageAttachment {
            object_key: "tenant_a/report.pdf".to_owned(),
            kind: AttachmentKind::Document,
            file_name: None,
            content_type: None,
            size_bytes: None,
        };
        assert!(
            S3Uploader::validate_attachment_metadata(
                &attachment,
                Some("application/pdf"),
                Some(42)
            )
            .is_ok()
        );
        assert!(
            S3Uploader::validate_attachment_metadata(
                &attachment,
                Some("application/octet-stream"),
                Some(42)
            )
            .is_err()
        );
    }
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
    fn staging_keys_require_exact_tenant_user_and_group_scope() {
        assert!(
            S3Uploader::validate_staging_key(
                "TenantA/user-a/sales/0123456789abcdef0123456789abcdef",
                "TenantA",
                "user-a",
                "sales",
            )
            .is_ok()
        );
        assert!(
            S3Uploader::validate_staging_key(
                "tenanta/user-a/sales/0123456789abcdef0123456789abcdef",
                "TenantA",
                "user-a",
                "sales",
            )
            .is_err()
        );
        assert!(
            S3Uploader::validate_staging_key(
                "TenantA/user-b/sales/0123456789abcdef0123456789abcdef",
                "TenantA",
                "user-a",
                "sales",
            )
            .is_err()
        );
        assert!(
            S3Uploader::validate_staging_key(
                "TenantA/user-a/returns/0123456789abcdef0123456789abcdef",
                "TenantA",
                "user-a",
                "sales",
            )
            .is_err()
        );
        assert!(
            S3Uploader::resolve_destination_key(
                "tenanta/raw/default/file.bin",
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

#[async_trait::async_trait]
impl crate::application::ports::pengolo_store::PengoloObjects for S3Uploader {
    async fn upload_url(
        &self,
        file: &crate::application::ports::pengolo_store::StagedFile<'_>,
    ) -> Result<(String, std::collections::BTreeMap<String, String>)> {
        Self::validate_staging_key(
            file.source,
            file.tenant,
            file.owner,
            "pengolo",
        )?;
        Self::validate_tenant_object_key(file.destination, file.tenant)?;
        let disposition = format!(
            "attachment; filename*=UTF-8''{}",
            urlencoding::encode(file.name)
        );
        let request = self
            .upload_client
            .put_object()
            .bucket(&self.staging_bucket)
            .key(file.source)
            .if_none_match("*")
            .content_type(file.mime)
            .content_length(file.size)
            .content_disposition(disposition)
            .checksum_sha256(file.checksum)
            .metadata("tenant", file.tenant)
            .metadata("owner", file.owner)
            .metadata("purpose", "pengolo")
            .metadata("filename", urlencoding::encode(file.name))
            .presigned(PresigningConfig::expires_in(Duration::from_secs(300))?)
            .await
            .context("File upload unavailable")?;
        let headers = request
            .headers()
            .filter(|(key, _)| {
                key.starts_with("x-amz-") ||
                    matches!(
                        *key,
                        "content-type" |
                            "content-disposition" |
                            "if-none-match"
                    )
            })
            .map(|(key, value)| (key.to_owned(), value.to_owned()))
            .collect();
        Ok((request.uri().to_owned(), headers))
    }

    async fn finalize(
        &self,
        file: &crate::application::ports::pengolo_store::StagedFile<'_>,
    ) -> Result<i64> {
        use aws_sdk_s3::types::{ChecksumAlgorithm, ChecksumMode};
        Self::validate_staging_key(
            file.source,
            file.tenant,
            file.owner,
            "pengolo",
        )?;
        Self::validate_tenant_object_key(file.destination, file.tenant)?;
        let head = self
            .client
            .head_object()
            .bucket(&self.staging_bucket)
            .key(file.source)
            .checksum_mode(ChecksumMode::Enabled)
            .send()
            .await
            .context("Uploaded file unavailable")?;
        let metadata = head.metadata().context("Missing file metadata")?;
        anyhow::ensure!(
            head.content_type() == Some(file.mime) &&
                head.content_length() == Some(file.size) &&
                head.checksum_sha256() == Some(file.checksum) &&
                metadata.get("tenant").map(String::as_str) ==
                    Some(file.tenant) &&
                metadata.get("owner").map(String::as_str) ==
                    Some(file.owner) &&
                metadata.get("purpose").map(String::as_str) ==
                    Some("pengolo") &&
                metadata.get("filename").map(String::as_str) ==
                    Some(urlencoding::encode(file.name).as_ref()),
            "Uploaded file metadata mismatch"
        );
        let uploaded_at = head
            .last_modified()
            .context("Missing upload timestamp")?
            .to_millis()?;
        anyhow::ensure!(
            (0..crate::domain::pengolo::STAGING_TTL_MS).contains(
                &chrono::Utc::now()
                    .timestamp_millis()
                    .saturating_sub(uploaded_at)
            ),
            "Staging upload expired"
        );
        let etag = head.e_tag().context("Missing upload version")?;
        let prefix = self
            .client
            .get_object()
            .bucket(&self.staging_bucket)
            .key(file.source)
            .if_match(etag)
            .set_version_id(head.version_id().map(str::to_owned))
            .range("bytes=0-8191")
            .send()
            .await
            .context("File format verification unavailable")?;
        let prefix = prefix
            .body
            .collect()
            .await
            .context("File format verification unavailable")?
            .into_bytes();
        crate::domain::pengolo::validate_file_prefix(file.mime, &prefix)?;
        let mut source = format!(
            "{}/{}",
            self.staging_bucket,
            urlencoding::encode(file.source)
        );
        if let Some(version) = head.version_id() {
            source.push_str("?versionId=");
            source.push_str(&urlencoding::encode(version));
        }
        let copied = self
            .client
            .copy_object()
            .bucket(&self.destination_bucket)
            .key(file.destination)
            .copy_source(source)
            .copy_source_if_match(etag)
            .if_none_match("*")
            .metadata_directive(MetadataDirective::Replace)
            .tagging_directive(TaggingDirective::Replace)
            .content_type(file.mime)
            .content_disposition(format!(
                "attachment; filename*=UTF-8''{}",
                urlencoding::encode(file.name)
            ))
            .checksum_algorithm(ChecksumAlgorithm::Sha256)
            .metadata("tenant", file.tenant)
            .metadata("owner", file.owner)
            .metadata("purpose", "pengolo")
            .metadata("filename", urlencoding::encode(file.name))
            .metadata("upload-sha256", file.checksum)
            .metadata("staging-source", file.source)
            .send()
            .await;
        if copied.is_err() {
            // A concurrent finalization may already have promoted this exact
            // immutable object.
            let existing = self
                .client
                .head_object()
                .bucket(&self.destination_bucket)
                .key(file.destination)
                .checksum_mode(ChecksumMode::Enabled)
                .send()
                .await
                .context("File promotion unavailable")?;
            let metadata =
                existing.metadata().context("Missing promoted metadata")?;
            anyhow::ensure!(
                existing.content_length() == Some(file.size) &&
                    existing.content_type() == Some(file.mime) &&
                    existing.checksum_sha256() == Some(file.checksum) &&
                    metadata.get("tenant").map(String::as_str) ==
                        Some(file.tenant) &&
                    metadata.get("filename").map(String::as_str) ==
                        Some(urlencoding::encode(file.name).as_ref()) &&
                    metadata.get("staging-source").map(String::as_str) ==
                        Some(file.source) &&
                    metadata.get("upload-sha256").map(String::as_str) ==
                        Some(file.checksum) &&
                    metadata.get("owner").map(String::as_str) ==
                        Some(file.owner) &&
                    metadata.get("purpose").map(String::as_str) ==
                        Some("pengolo"),
                "Conflicting file promotion"
            );
        }
        Ok(uploaded_at)
    }

    async fn delete_staging(&self, key: &str) -> Result<()> {
        self.purge_versions(&self.staging_bucket, key).await
    }

    async fn read(
        &self,
        key: &str,
    ) -> Result<crate::application::ports::pengolo_store::FileChunks> {
        let mut object = self
            .client
            .get_object()
            .bucket(&self.destination_bucket)
            .key(key)
            .send()
            .await
            .context("File unavailable")?;
        Ok(Box::pin(
            async_stream::stream! {while let Some(chunk)=object.body.next().await {yield chunk.map_err(anyhow::Error::from);}},
        ))
    }

    async fn download_url(
        &self,
        key: &str,
        ttl_seconds: u64,
    ) -> Result<String> {
        anyhow::ensure!(
            (1..=300).contains(&ttl_seconds),
            "Invalid file URL lifetime"
        );
        Ok(self
            .client
            .get_object()
            .bucket(&self.destination_bucket)
            .key(key)
            .response_content_disposition("attachment")
            .presigned(PresigningConfig::expires_in(Duration::from_secs(
                ttl_seconds,
            ))?)
            .await
            .context("File download unavailable")?
            .uri()
            .to_owned())
    }

    async fn delete(&self, key: &str) -> Result<()> {
        self.purge_versions(&self.destination_bucket, key).await
    }
}

impl S3Uploader {
    async fn purge_versions(&self, bucket: &str, key: &str) -> Result<()> {
        self.client
            .delete_object()
            .bucket(bucket)
            .key(key)
            .send()
            .await
            .context("File deletion unavailable")?;
        let mut marker: Option<String> = None;
        let mut version_marker: Option<String> = None;
        loop {
            let versions = self
                .client
                .list_object_versions()
                .bucket(bucket)
                .prefix(key)
                .set_key_marker(marker)
                .set_version_id_marker(version_marker)
                .send()
                .await
                .context("File version cleanup unavailable")?;
            for version in versions.versions() {
                if version.key() == Some(key) &&
                    let Some(id) = version.version_id()
                {
                    self.client
                        .delete_object()
                        .bucket(bucket)
                        .key(key)
                        .version_id(id)
                        .send()
                        .await
                        .context("File version deletion unavailable")?;
                }
            }
            for version in versions.delete_markers() {
                if version.key() == Some(key) &&
                    let Some(id) = version.version_id()
                {
                    self.client
                        .delete_object()
                        .bucket(bucket)
                        .key(key)
                        .version_id(id)
                        .send()
                        .await
                        .context("File marker deletion unavailable")?;
                }
            }
            if versions.is_truncated() != Some(true) {
                break;
            }
            marker = versions.next_key_marker().map(str::to_owned);
            version_marker =
                versions.next_version_id_marker().map(str::to_owned);
        }
        Ok(())
    }
}

#[cfg(test)]
mod pengolo_tests {
    use super::*;
    use crate::application::ports::pengolo_store::PengoloObjects;

    #[tokio::test]
    async fn pengolo_storage_server_enforces_checksums_and_copy_preconditions()
    -> Result<()> {
        use testcontainers_modules::testcontainers::core::{
            IntoContainerPort, WaitFor,
        };
        use testcontainers_modules::testcontainers::runners::AsyncRunner;
        use testcontainers_modules::testcontainers::{GenericImage, ImageExt};
        let container=GenericImage::new("rustfs/rustfs","1.0.1@sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c")
            .with_exposed_port(9000.tcp()).with_wait_for(WaitFor::Nothing)
            .with_env_var("RUSTFS_ACCESS_KEY","minioadmin").with_env_var("RUSTFS_SECRET_KEY","minioadmin")
            .with_env_var("RUSTFS_CONSOLE_ENABLE","false").with_cmd(["rustfs","/data"]).start().await?;
        let endpoint = format!(
            "http://{}:{}",
            container.get_host().await?,
            container.get_host_port_ipv4(9000).await?
        );
        let http = reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(1))
            .build()?;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
        // Liveness precedes storage and IAM initialization; only readiness
        // gates S3 writes.
        loop {
            if http
                .get(format!("{endpoint}/health/ready"))
                .send()
                .await
                .is_ok_and(|response| response.status().is_success())
            {
                break;
            }
            anyhow::ensure!(
                tokio::time::Instant::now() < deadline,
                "Storage fixture failed storage/IAM readiness"
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let config = aws_sdk_s3::config::Builder::new()
            .behavior_version_latest()
            .region(Region::new("us-east-1"))
            .credentials_provider(Credentials::new(
                "minioadmin",
                "minioadmin",
                None,
                None,
                "test",
            ))
            .endpoint_url(&endpoint)
            .force_path_style(true)
            .build();
        let client = Client::from_conf(config);
        client
            .list_buckets()
            .send()
            .await
            .context("Authenticating storage fixture before writes")?;
        for bucket in ["staging", "lake"] {
            client
                .create_bucket()
                .bucket(bucket)
                .send()
                .await
                .with_context(|| {
                    format!("Creating storage fixture bucket {bucket}")
                })?;
        }
        let uploader = S3Uploader {
            client: client.clone(),
            upload_client: client.clone(),
            staging_bucket: "staging".into(),
            destination_bucket: "lake".into(),
        };
        let file = crate::application::ports::pengolo_store::StagedFile {
            tenant: "tenant",
            owner: "owner",
            source: "tenant/owner/pengolo/00000000000000000000000000000001",
            destination: "tenant/owner/pengolo/file.txt",
            mime: "text/plain",
            size: 5,
            name: "file.txt",
            checksum: "LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ=",
        };
        let (url, headers) = uploader.upload_url(&file).await?;
        let http = reqwest::Client::builder().no_proxy().build()?;
        let put = |content: &'static str| {
            let mut request = http.put(&url);
            for (name, value) in &headers {
                request = request.header(name, value);
            }
            request.body(content)
        };
        assert!(!put("wrong").send().await?.status().is_success());
        assert!(put("hello").send().await?.status().is_success());
        assert_eq!(
            put("hello").send().await?.status(),
            reqwest::StatusCode::PRECONDITION_FAILED
        );
        client
            .put_object()
            .bucket("lake")
            .key(file.destination)
            .body(aws_sdk_s3::primitives::ByteStream::from_static(b"other"))
            .send()
            .await
            .context("Writing conflicting destination")?;
        assert!(
            uploader.finalize(&file).await.is_err(),
            "Existing destination must never be overwritten"
        );
        let existing = client
            .get_object()
            .bucket("lake")
            .key(file.destination)
            .send()
            .await
            .context("Reading conflicting destination")?
            .body
            .collect()
            .await?
            .into_bytes();
        assert_eq!(existing.as_ref(), b"other");
        uploader.delete(file.destination).await?;
        client
            .put_object()
            .bucket("lake")
            .key(file.destination)
            .content_type(file.mime)
            .metadata("owner", file.owner)
            .metadata("tenant", "other-tenant")
            .metadata("purpose", "pengolo")
            .metadata("upload-sha256", file.checksum)
            .metadata("filename", file.name)
            .body(aws_sdk_s3::primitives::ByteStream::from_static(b"hello"))
            .send()
            .await
            .context("Writing conflicting destination")?;
        assert!(
            uploader.finalize(&file).await.is_err(),
            "A conflicting destination cannot be accepted as an idempotent retry"
        );
        uploader.delete(file.destination).await?;
        assert!(uploader.finalize(&file).await? > 0);
        assert!(uploader.finalize(&file).await? > 0);
        uploader.delete(file.destination).await?;
        uploader.delete_staging(file.source).await?;
        Ok(())
    }

    #[tokio::test]
    async fn pengolo_uploads_are_write_once_and_bind_metadata() -> Result<()> {
        let config = aws_sdk_s3::config::Builder::new()
            .behavior_version_latest()
            .region(Region::new("us-east-1"))
            .credentials_provider(Credentials::new(
                "fixture", "fixture", None, None, "test",
            ))
            .endpoint_url("http://localhost:19000")
            .force_path_style(true)
            .build();
        let uploader = S3Uploader {
            client: Client::from_conf(config.clone()),
            upload_client: Client::from_conf(config),
            staging_bucket: "staging".into(),
            destination_bucket: "lake".into(),
        };
        let (url, headers) = uploader
            .upload_url(&crate::application::ports::pengolo_store::StagedFile {
                tenant:"tenant",owner:"owner",source:"tenant/owner/pengolo/00000000000000000000000000000001",
                destination:"tenant/owner/pengolo/file.txt",mime:"text/plain",size:12,name:"file.txt",
                checksum:"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            })
            .await?;
        assert!(reqwest::Url::parse(&url)?.path().starts_with("/staging/"));
        assert!(headers.contains_key("content-disposition"));
        let signed = reqwest::Url::parse(&url)?
            .query_pairs()
            .find(|(name, _)| name == "X-Amz-SignedHeaders")
            .map(|(_, value)| value.into_owned())
            .context("Missing signed headers")?;
        assert!(signed.split(';').any(|name| name == "content-disposition"));
        assert!(signed.split(';').any(|name| name == "content-length"));
        assert!(signed.split(';').any(|name| name == "x-amz-meta-tenant"));
        assert_eq!(
            headers.get("if-none-match").map(String::as_str),
            Some("*")
        );
        assert_eq!(
            headers.get("x-amz-meta-tenant").map(String::as_str),
            Some("tenant")
        );
        assert!(uploader.download_url("tenant/file.txt", 301).await.is_err());
        Ok(())
    }
}

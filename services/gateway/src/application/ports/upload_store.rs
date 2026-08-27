//! Storage boundary for authenticated direct-upload promotion.

use std::time::Duration;

use anyhow::Result;

/// Trusted metadata used to replace untrusted staging-object metadata.
pub struct UploadFinalization<'a> {
    pub staging_key: &'a str,
    pub destination_key: &'a str,
    pub tenant_id: &'a str,
    pub user_id: &'a str,
    pub authn_issuer: &'a str,
    pub delegation_id: &'a str,
    pub tagging_query: Option<&'a str>,
}

/// S3-compatible operations used by the upload application service.
#[async_trait::async_trait]
pub trait UploadStore: Send + Sync {
    /// Creates a short-lived direct PUT URL for one owner-scoped staging key.
    async fn presign_upload(
        &self,
        key: &str,
        expires_in: Duration,
    ) -> Result<String>;

    /// Promotes one staged object under trusted tenant ownership metadata.
    async fn finalize_upload(
        &self,
        request: UploadFinalization<'_>,
    ) -> Result<String>;
}

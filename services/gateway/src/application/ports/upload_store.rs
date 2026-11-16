//! Storage boundary for authenticated direct-upload promotion.

use std::time::Duration;

use anyhow::Result;

use crate::application::usecases::authorization::QueryContext;

/// Trusted metadata used to replace untrusted staging-object metadata.
pub struct UploadFinalization<'a> {
    pub staging_key: &'a str,
    /// Binds the copy to the exact source bytes admitted by the host.
    pub staging_revision: &'a str,
    /// Trusted GOL admission replaces any receipt supplied in staging
    /// metadata.
    pub gol_admission: &'a str,
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
    /// Returns the trusted ETag before requesting an ingress admission.
    async fn staging_revision(&self, staging_key: &str) -> Result<String>;
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

/// Trusted GOL host boundary used before an immutable raw object is promoted.
#[async_trait::async_trait]
pub trait UploadAdmission: Send + Sync {
    /// Admits only an approved group, actor, input revision, and live context.
    async fn issue_ingress(
        &self,
        tenant: &str,
        user: &str,
        source_object_id: &str,
        revision: &str,
        group: &str,
        context: &QueryContext,
    ) -> Result<String>;
}

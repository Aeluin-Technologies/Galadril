//! Tenant-validated S3 attachment access contract.

use std::time::Duration;

use anyhow::Result;

use crate::application::ports::conversation_agent::AgentAttachment;
use crate::application::ports::conversation_store::MessageAttachment;

/// Resolves durable S3 keys into short-lived URLs for one principal.
#[async_trait::async_trait]
pub trait AttachmentStore: Send + Sync {
    /// Validates object ownership metadata and signs read-only downloads.
    async fn resolve_for_scribe(
        &self,
        tenant_id: &str,
        user_id: &str,
        attachments: &[MessageAttachment],
        expires_in: Duration,
    ) -> Result<Vec<AgentAttachment>>;
}

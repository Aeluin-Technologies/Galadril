//! Tenant-scoped, secret-free configuration for provider-managed role
//! assignments.

use anyhow::Result;

pub use crate::authentication::FederationConfig;

#[async_trait::async_trait]
pub trait FederationStore: Send + Sync {
    /// Returns only the current tenant's approved claim mappings.
    async fn configurations(
        &self,
        tenant: &str,
    ) -> Result<Vec<FederationConfig>>;
    /// Atomically stores explicitly approved role names and their claim
    /// mappings.
    async fn configure_approved_roles(
        &self,
        tenant: &str,
        config: &FederationConfig,
    ) -> Result<()>;
}

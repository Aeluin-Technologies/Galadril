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
    /// Persists explicit administrator-approved mappings without creating
    /// roles.
    async fn configure(
        &self,
        tenant: &str,
        config: &FederationConfig,
    ) -> Result<()>;
}

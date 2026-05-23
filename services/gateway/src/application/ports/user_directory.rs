//! Outbound port for verifying user identity and tenant membership.

use anyhow::Result;

/// Minimal user status needed to gate requests.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UserStatus {
    Active,
    Disabled,
    NotFound,
}

#[async_trait::async_trait]
pub trait UserDirectory: Send + Sync {
    /// Returns whether the user exists, belongs to the tenant, and is active.
    async fn get_user_status(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<UserStatus>;
}

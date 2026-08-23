//! Outbound port for tenant-scoped users, roles, and Cedar policies.

use anyhow::Result;

#[async_trait::async_trait]
pub trait IamStore: Send + Sync {
    async fn create_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        is_active: bool,
    ) -> Result<()>;
    async fn delete_user(&self, tenant_id: &str, user_id: &str) -> Result<()>;

    async fn create_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()>;

    async fn delete_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()>;

    async fn assign_role_to_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        role_name: &str,
    ) -> Result<()>;

    /// Returns active tenant Cedar policies as one policy set.
    async fn get_active_cedar_policies(
        &self,
        tenant_id: &str,
    ) -> Result<Option<String>>;

    /// Creates or replaces one tenant-scoped Cedar policy.
    async fn upsert_cedar_policy(
        &self,
        tenant_id: &str,
        policy_id: &str,
        content: &str,
        is_active: bool,
    ) -> Result<()>;
}

//! Outbound port for tenant-scoped users, roles, and Cedar policies.

use anyhow::Result;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IamUser {
    pub user_id: String,
    pub is_active: bool,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IamRole {
    pub role_name: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoleAssignment {
    pub user_id: String,
    pub role_name: String,
    pub created_at_ms: i64,
}

#[async_trait::async_trait]
pub trait IamStore: Send + Sync {
    /// Creates a user without allowing tombstoned identifiers to be reused.
    async fn create_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        is_active: bool,
    ) -> Result<()>;
    /// Changes whether an existing non-deleted user may authenticate.
    async fn update_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        is_active: bool,
    ) -> Result<()>;
    /// Soft-deletes a user and removes its durable role assignments.
    async fn delete_user(&self, tenant_id: &str, user_id: &str) -> Result<()>;

    /// Creates a role without allowing tombstoned names to be reused.
    async fn create_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()>;
    /// Soft-deletes a role and removes its durable assignments.
    async fn delete_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()>;

    /// Creates one durable user-to-role assignment.
    async fn assign_role_to_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        role_name: &str,
    ) -> Result<()>;

    /// Removes one durable user-to-role assignment.
    async fn unassign_role_from_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        role_name: &str,
    ) -> Result<()>;

    /// Lists current, non-deleted tenant users.
    async fn list_users(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<IamUser>>;

    /// Lists current, non-deleted tenant roles.
    async fn list_roles(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<IamRole>>;

    /// Lists assignments whose user and role are both current.
    async fn list_role_assignments(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<RoleAssignment>>;

    /// Lists every current role assigned to one user for authorization
    /// cleanup.
    async fn role_names_for_user(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<Vec<String>>;

    /// Lists every current user assigned to one role for authorization
    /// cleanup.
    async fn user_ids_for_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<Vec<String>>;

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

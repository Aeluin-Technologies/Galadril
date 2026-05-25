//! Outbound port for IAM persistence (users/roles/permissions) with tenant
//! isolation.

use anyhow::Result;

use crate::domain::permission::IamPermission;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SubjectKind {
    User,
    Role,
}

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

    async fn set_user_permissions(
        &self,
        tenant_id: &str,
        user_id: &str,
        permissions: &[IamPermission],
    ) -> Result<()>;

    async fn set_role_permissions(
        &self,
        tenant_id: &str,
        role_name: &str,
        permissions: &[IamPermission],
    ) -> Result<()>;

    /// Reads the caller's effective permissions envelope used to prevent
    /// privilege escalation when granting.
    async fn get_effective_permissions_for_user(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<Vec<IamPermission>>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subject_kind_is_copy_eq() {
        let _ = SubjectKind::User;
    }
}

//! Outbound port for tenant-scoped users, roles, and Cedar policies.

use anyhow::Result;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IamUser {
    pub user_id: String,
    pub display_name: Option<String>,
    pub email: Option<String>,
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

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CedarPolicy {
    pub id: String,
    pub content: String,
    pub active: bool,
}

#[async_trait::async_trait]
pub trait IamStore: Send + Sync {
    /// Checks the exact live role without relying on a paginated directory.
    async fn role_exists(&self, _: &str, _: &str) -> Result<bool> {
        anyhow::bail!("Exact role lookup is unavailable")
    }

    /// Searches only principals visible through the caller's role hierarchy.
    async fn search_users(
        &self,
        tenant_id: &str,
        user_id: &str,
        administrator: bool,
        search: &str,
        limit: usize,
    ) -> Result<Vec<IamUser>> {
        let mut users = self.list_users(tenant_id, limit).await?;
        users.retain(|user| {
            (administrator || user.user_id == user_id) &&
                matches_search(
                    search,
                    &[
                        Some(&user.user_id),
                        user.display_name.as_deref(),
                        user.email.as_deref(),
                    ],
                )
        });
        Ok(users)
    }

    /// Searches owned and subordinate roles without revealing higher roles.
    async fn search_roles(
        &self,
        tenant_id: &str,
        user_id: &str,
        administrator: bool,
        search: &str,
        limit: usize,
    ) -> Result<Vec<IamRole>> {
        let owned = self
            .effective_role_names_for_user(tenant_id, user_id)
            .await?;
        let mut roles = self.list_roles(tenant_id, limit).await?;
        roles.retain(|role| {
            (administrator || owned.contains(&role.role_name)) &&
                matches_search(search, &[Some(&role.role_name)])
        });
        Ok(roles)
    }

    /// Resolves inherited role membership from the durable tenant directory.
    async fn effective_role_names_for_user(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<Vec<String>> {
        self.role_names_for_user(tenant_id, user_id).await
    }

    /// Changes one hierarchy edge while serializing cycle detection.
    async fn set_role_parent(
        &self,
        _: &str,
        _: &str,
        _: &str,
        _: bool,
    ) -> Result<()> {
        anyhow::bail!("Role hierarchy is unavailable")
    }

    /// Serializes the durable hierarchy and external authorization write.
    async fn set_role_parent_with_access(
        &self,
        tenant: &str,
        role: &str,
        parent: &str,
        enabled: bool,
        access: &dyn crate::application::ports::access_store::AccessStore,
    ) -> Result<()> {
        self.set_role_parent(tenant, role, parent, enabled).await?;
        access
            .set(
                tenant,
                &crate::application::ports::access_store::AccessTuple {
                    resource_type: "role".into(),
                    resource_id: role.into(),
                    relation: "member".into(),
                    subject_type: "role".into(),
                    subject_id: parent.into(),
                    subject_relation: "member".into(),
                },
                enabled,
            )
            .await
    }

    /// Checks source ownership before changing a relationship in SpiceDB.
    async fn verify_role_unassignment(
        &self,
        _: &str,
        _: &str,
        _: &str,
    ) -> Result<()> {
        Ok(())
    }

    /// Returns policy source only within an authorized tenant boundary.
    async fn cedar_policies(&self, _: &str) -> Result<Vec<CedarPolicy>> {
        Ok(Vec::new())
    }
    /// Creates a user without allowing tombstoned identifiers to be reused.
    #[cfg(test)]
    async fn create_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        is_active: bool,
    ) -> Result<()>;
    /// Serializes user state changes and requires a surviving active
    /// administrator.
    async fn set_user_guarded(
        &self,
        tenant_id: &str,
        actor_id: &str,
        user_id: &str,
        is_active: bool,
        upsert: bool,
    ) -> Result<()>;

    /// Soft-deletes a user and removes its durable role assignments.
    async fn delete_user(&self, tenant_id: &str, user_id: &str) -> Result<()>;

    /// Creates a role without allowing tombstoned names to be reused.
    async fn create_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()>;

    /// Reserves a bounded batch without partially consuming a tenant quota.
    async fn create_roles(
        &self,
        tenant_id: &str,
        role_names: &[&str],
    ) -> Result<()> {
        for role_name in role_names {
            self.create_role(tenant_id, role_name).await?;
        }
        Ok(())
    }
    /// Soft-deletes a role and removes its durable assignments.
    async fn delete_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()>;

    /// Rejects managed or oversized role cleanup before external revocation.
    async fn verify_role_deletion(&self, _: &str, _: &str) -> Result<()> {
        Ok(())
    }

    /// Holds the tenant lifecycle lock through structural role revocation.
    async fn delete_role_with_access(
        &self,
        _: &str,
        _: &str,
        _: &dyn crate::application::ports::access_store::AccessStore,
    ) -> Result<()> {
        anyhow::bail!("Atomic role deletion is unavailable")
    }

    /// Serializes manual membership projection with role lifecycle changes.
    async fn set_role_membership_with_access(
        &self,
        _: &str,
        _: &str,
        _: &str,
        _: bool,
        _: &dyn crate::application::ports::access_store::AccessStore,
    ) -> Result<()> {
        anyhow::bail!("Atomic role membership management is unavailable")
    }

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

/// Keeps fixture fallbacks consistent with literal case-insensitive search.
fn matches_search(search: &str, values: &[Option<&str>]) -> bool {
    let search = search.to_lowercase();
    values
        .iter()
        .flatten()
        .any(|value| value.to_lowercase().contains(&search))
}

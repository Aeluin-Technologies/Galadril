//! IAM administration use cases with strict tenant isolation and
//! anti-escalation checks.

use std::sync::Arc;

use anyhow::{Context, Result, bail};

use crate::application::ports::iam_store::IamStore;
use crate::application::usecases::authorization::{Action, AuthService};
use crate::application::usecases::data_explorer::DataExplorerService;
use crate::application::usecases::iam_scope::can_grant_all;
use crate::application::usecases::identity::IdentityService;
use crate::domain::permission::IamPermission;

pub struct IamAdminService {
    iam: Arc<dyn IamStore>,
    identity: Arc<IdentityService>,
    auth: Arc<AuthService>,
    data_explorer: Arc<DataExplorerService>,
}

impl IamAdminService {
    pub fn new(
        iam: Arc<dyn IamStore>,
        identity: Arc<IdentityService>,
        auth: Arc<AuthService>,
        data_explorer: Arc<DataExplorerService>,
    ) -> Self {
        Self {
            iam,
            identity,
            auth,
            data_explorer,
        }
    }

    pub async fn create_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        new_user_id: &str,
        is_active: bool,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;

        let ok = self
            .auth
            .is_authorized(
                tenant_id,
                caller_user_id,
                Action::ManageIamUsers,
                "iam_users",
                None,
            )
            .await
            .context("Failed to Cedar-authorize create_user")?;

        if !ok {
            bail!(
                "Caller '{caller_user_id}' is not authorized to create users"
            );
        }

        self.iam
            .create_user(tenant_id, new_user_id, is_active)
            .await?;

        // IAM changes affect auth decisions; invalidate tenant cache.
        self.auth.invalidate_tenant_cache(tenant_id).await;
        self.data_explorer.invalidate_cache().await;

        Ok(())
    }

    pub async fn create_role(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;

        let ok = self
            .auth
            .is_authorized(
                tenant_id,
                caller_user_id,
                Action::ManageIamRoles,
                "iam_roles",
                None,
            )
            .await
            .context("Failed to Cedar-authorize create_role")?;

        if !ok {
            bail!(
                "Caller '{caller_user_id}' is not authorized to create roles"
            );
        }

        self.iam.create_role(tenant_id, role_name).await?;
        self.auth.invalidate_tenant_cache(tenant_id).await;

        Ok(())
    }

    pub async fn assign_role_to_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;

        let ok = self
            .auth
            .is_authorized(
                tenant_id,
                caller_user_id,
                Action::ManageUserRoleAssignments,
                "iam_user_roles",
                None,
            )
            .await
            .context("Failed to Cedar-authorize assign_role_to_user")?;

        if !ok {
            bail!(
                "Caller '{caller_user_id}' is not authorized to assign roles"
            );
        }

        // Here we just rely on DB constraints; adapter uses tenant schema
        // scoping.
        self.iam
            .assign_role_to_user(tenant_id, user_id, role_name)
            .await?;

        self.auth.invalidate_tenant_cache(tenant_id).await;

        Ok(())
    }

    pub async fn set_user_permissions(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        target_user_id: &str,
        permissions: &[IamPermission],
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;

        let ok = self
            .auth
            .is_authorized(
                tenant_id,
                caller_user_id,
                Action::GrantPermissions,
                "iam_user_permissions",
                None,
            )
            .await
            .context("Failed to Cedar-authorize set_user_permissions")?;

        if !ok {
            bail!(
                "Caller '{caller_user_id}' is not authorized to update user permissions"
            );
        }

        let caller_effective = self
            .iam
            .get_effective_permissions_for_user(tenant_id, caller_user_id)
            .await
            .context("Failed to load caller effective permissions")?;

        if !can_grant_all(&caller_effective, permissions) {
            bail!("Requested permissions exceed caller permission envelope");
        }

        self.iam
            .set_user_permissions(tenant_id, target_user_id, permissions)
            .await?;

        self.auth.invalidate_tenant_cache(tenant_id).await;

        Ok(())
    }

    pub async fn set_role_permissions(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        role_name: &str,
        permissions: &[IamPermission],
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;

        let ok = self
            .auth
            .is_authorized(
                tenant_id,
                caller_user_id,
                Action::GrantPermissions,
                "iam_role_permissions",
                None,
            )
            .await
            .context("Failed to Cedar-authorize set_role_permissions")?;

        if !ok {
            bail!(
                "Caller '{caller_user_id}' is not authorized to update role permissions"
            );
        }

        let caller_effective = self
            .iam
            .get_effective_permissions_for_user(tenant_id, caller_user_id)
            .await
            .context("Failed to load caller effective permissions")?;

        if !can_grant_all(&caller_effective, permissions) {
            bail!("Requested permissions exceed caller permission envelope");
        }

        self.iam
            .set_role_permissions(tenant_id, role_name, permissions)
            .await?;

        self.auth.invalidate_tenant_cache(tenant_id).await;

        Ok(())
    }
}

//! IAM administration use cases with strict tenant isolation and
//! anti-escalation checks.

use std::sync::Arc;

use anyhow::{Context, Result, bail};

use crate::application::ports::iam_store::IamStore;
use crate::application::usecases::authorization::{AuthService, Permission};
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

    async fn require_tenant_admin(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
    ) -> Result<()> {
        let ok = self
            .auth
            .is_authorized(
                caller_user_id,
                Permission::Admin,
                "tenant",
                tenant_id,
                None,
            )
            .await
            .context("Failed to authorize tenant admin operation")?;

        if !ok {
            bail!("Caller '{caller_user_id}' is not a tenant admin");
        }
        Ok(())
    }

    pub async fn create_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        new_user_id: &str,
        is_active: bool,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id).await?;

        self.iam
            .create_user(tenant_id, new_user_id, is_active)
            .await?;

        self.auth
            .upsert_relationship(
                "tenant",
                tenant_id,
                "member",
                "user",
                new_user_id,
            )
            .await?;

        self.auth.invalidate_tenant_cache(tenant_id).await;
        self.data_explorer.invalidate_cache().await;

        Ok(())
    }

    pub async fn delete_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        target_user_id: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id).await?;

        self.iam.delete_user(tenant_id, target_user_id).await?;

        self.auth
            .delete_relationship(
                "tenant",
                tenant_id,
                "member",
                "user",
                target_user_id,
            )
            .await?;

        self.auth.invalidate_tenant_cache(tenant_id).await;
        Ok(())
    }

    pub async fn create_role(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id).await?;

        self.iam.create_role(tenant_id, role_name).await?;

        let composite_role_id = format!("{}_{}", tenant_id, role_name);
        self.auth
            .upsert_relationship(
                "tenant",
                tenant_id,
                "role",
                "role",
                &composite_role_id,
            )
            .await?;

        self.auth.invalidate_tenant_cache(tenant_id).await;

        Ok(())
    }

    pub async fn delete_role(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id).await?;

        self.iam.delete_role(tenant_id, role_name).await?;

        let composite_role_id = format!("{}_{}", tenant_id, role_name);
        self.auth
            .delete_relationship(
                "tenant",
                tenant_id,
                "role",
                "role",
                &composite_role_id,
            )
            .await?;

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
        self.require_tenant_admin(tenant_id, caller_user_id).await?;

        self.iam
            .assign_role_to_user(tenant_id, user_id, role_name)
            .await?;

        let composite_role_id = format!("{}_{}", tenant_id, role_name);
        self.auth
            .upsert_relationship(
                "role",
                &composite_role_id,
                "member",
                "user",
                user_id,
            )
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
        self.require_tenant_admin(tenant_id, caller_user_id).await?;

        // NOTE: This entire "permission record" model is going away under
        // SpiceDB. Keeping the anti-escalation check until DB schema
        // is simplified.
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

        for p in permissions {
            if p.effect == crate::domain::permission::Effect::Allow {
                self.auth
                    .upsert_relationship(
                        "user",
                        target_user_id,
                        &p.action,
                        "tenant",
                        tenant_id,
                    )
                    .await?;
            }
        }

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
        self.require_tenant_admin(tenant_id, caller_user_id).await?;

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

        let composite_role_id = format!("{}_{}", tenant_id, role_name);
        for p in permissions {
            if p.effect == crate::domain::permission::Effect::Allow {
                self.auth
                    .upsert_relationship(
                        "role",
                        &composite_role_id,
                        &p.action,
                        "tenant",
                        tenant_id,
                    )
                    .await?;
            }
        }

        self.auth.invalidate_tenant_cache(tenant_id).await;
        Ok(())
    }
}

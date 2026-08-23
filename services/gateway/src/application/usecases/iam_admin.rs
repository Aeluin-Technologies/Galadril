//! IAM administration use cases with strict tenant isolation and
//! anti-escalation checks.

use std::sync::Arc;

use anyhow::{Context, Result, bail};

use crate::application::ports::iam_store::IamStore;
use crate::application::usecases::authorization::{
    AuthService, Permission, QueryContext, validate_cedar_policy,
};
use crate::application::usecases::identity::IdentityService;

pub struct IamAdminService {
    iam: Arc<dyn IamStore>,
    identity: Arc<IdentityService>,
    auth: Arc<AuthService>,
}

impl IamAdminService {
    pub fn new(
        iam: Arc<dyn IamStore>,
        identity: Arc<IdentityService>,
        auth: Arc<AuthService>,
    ) -> Self {
        Self {
            iam,
            identity,
            auth,
        }
    }

    async fn require_tenant_admin(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
    ) -> Result<()> {
        let ok = self
            .auth
            .is_authorized(
                caller_user_id,
                tenant_id,
                Permission::Manage,
                "tenant",
                tenant_id,
                Some(context),
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
        context: &QueryContext,
        new_user_id: &str,
        is_active: bool,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id, context)
            .await?;

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

        Ok(())
    }

    pub async fn delete_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        target_user_id: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id, context)
            .await?;

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
        context: &QueryContext,
        role_name: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id, context)
            .await?;

        self.iam.create_role(tenant_id, role_name).await?;

        let composite_role_id = format!("{tenant_id}/{role_name}");
        self.auth
            .upsert_relationship(
                "tenant",
                tenant_id,
                "role",
                "role",
                &composite_role_id,
            )
            .await?;
        self.auth
            .upsert_relationship(
                "role",
                &composite_role_id,
                "parent",
                "tenant",
                tenant_id,
            )
            .await?;

        self.auth.invalidate_tenant_cache(tenant_id).await;

        Ok(())
    }

    pub async fn delete_role(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        role_name: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id, context)
            .await?;

        self.iam.delete_role(tenant_id, role_name).await?;

        let composite_role_id = format!("{tenant_id}/{role_name}");
        self.auth
            .delete_relationship(
                "tenant",
                tenant_id,
                "role",
                "role",
                &composite_role_id,
            )
            .await?;
        self.auth
            .delete_relationship(
                "role",
                &composite_role_id,
                "parent",
                "tenant",
                tenant_id,
            )
            .await?;

        self.auth.invalidate_tenant_cache(tenant_id).await;
        Ok(())
    }

    pub async fn assign_role_to_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id, context)
            .await?;

        self.iam
            .assign_role_to_user(tenant_id, user_id, role_name)
            .await?;

        let composite_role_id = format!("{tenant_id}/{role_name}");
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

    pub async fn set_cedar_policy(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        policy_id: &str,
        content: &str,
        is_active: bool,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, caller_user_id).await?;
        self.require_tenant_admin(tenant_id, caller_user_id, context)
            .await?;
        let policy_id = policy_id.trim();
        if policy_id.is_empty() ||
            policy_id.len() > 64 ||
            !policy_id.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-'
            })
        {
            bail!("Invalid Cedar policy identifier");
        }
        validate_cedar_policy(content)?;
        self.iam
            .upsert_cedar_policy(tenant_id, policy_id, content, is_active)
            .await?;
        self.auth.invalidate_tenant_cache(tenant_id).await;
        tracing::info!(
            event.name = "authorization.policy.updated",
            tenant_id,
            actor_id = caller_user_id,
            policy_id,
            is_active,
            "tenant Cedar policy updated"
        );
        Ok(())
    }
}

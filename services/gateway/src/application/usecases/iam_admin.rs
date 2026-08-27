//! IAM administration use cases with fail-closed authorization and audit.

use std::sync::Arc;

use anyhow::{Context, Result, bail};
use serde_json::json;

use crate::application::ports::iam_store::IamStore;
use crate::application::usecases::audit::{
    AuditAction, AuditOperation, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext, validate_cedar_policy,
};
use crate::application::usecases::identity::IdentityService;

/// Coordinates tenant IAM persistence, authorization relationships, and audit.
pub struct IamAdminService {
    iam: Arc<dyn IamStore>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
}

#[cfg(test)]
#[expect(
    clippy::items_after_test_module,
    reason = "the local test doubles stay adjacent to the service state they exercise"
)]
mod tests {
    use std::collections::{HashMap, HashSet};
    use std::sync::Mutex;

    use anyhow::{Result, anyhow, ensure};

    use super::*;
    use crate::application::ports::iam_store::{
        IamRole, IamUser, RoleAssignment,
    };
    use crate::application::test_support::{
        AuthorizationDecision, TestAuthorization, audit, identity,
    };

    #[derive(Default)]
    struct IamState {
        users: HashMap<String, bool>,
        roles: HashSet<String>,
        assignments: HashSet<(String, String)>,
        policies: HashMap<String, String>,
    }

    #[derive(Default)]
    struct MemoryIamStore {
        state: Mutex<IamState>,
    }

    impl MemoryIamStore {
        /// Locks IAM state without hiding poisoning failures.
        fn lock(&self) -> Result<std::sync::MutexGuard<'_, IamState>> {
            self.state
                .lock()
                .map_err(|error| anyhow!("IAM test lock poisoned: {error}"))
        }
    }

    #[async_trait::async_trait]
    impl IamStore for MemoryIamStore {
        async fn create_user(
            &self,
            _tenant_id: &str,
            user_id: &str,
            is_active: bool,
        ) -> Result<()> {
            self.lock()?.users.insert(user_id.to_owned(), is_active);
            Ok(())
        }

        async fn update_user(
            &self,
            _tenant_id: &str,
            user_id: &str,
            is_active: bool,
        ) -> Result<()> {
            let previous =
                self.lock()?.users.insert(user_id.to_owned(), is_active);
            ensure!(previous.is_some(), "user missing");
            Ok(())
        }

        async fn delete_user(
            &self,
            _tenant_id: &str,
            user_id: &str,
        ) -> Result<()> {
            let mut state = self.lock()?;
            ensure!(state.users.remove(user_id).is_some(), "user missing");
            state.assignments.retain(|(user, _)| user != user_id);
            Ok(())
        }

        async fn create_role(
            &self,
            _tenant_id: &str,
            role_name: &str,
        ) -> Result<()> {
            self.lock()?.roles.insert(role_name.to_owned());
            Ok(())
        }

        async fn delete_role(
            &self,
            _tenant_id: &str,
            role_name: &str,
        ) -> Result<()> {
            let mut state = self.lock()?;
            ensure!(state.roles.remove(role_name), "role missing");
            state.assignments.retain(|(_, role)| role != role_name);
            Ok(())
        }

        async fn assign_role_to_user(
            &self,
            _tenant_id: &str,
            user_id: &str,
            role_name: &str,
        ) -> Result<()> {
            let mut state = self.lock()?;
            ensure!(state.users.contains_key(user_id), "user missing");
            ensure!(state.roles.contains(role_name), "role missing");
            state
                .assignments
                .insert((user_id.to_owned(), role_name.to_owned()));
            Ok(())
        }

        async fn unassign_role_from_user(
            &self,
            _tenant_id: &str,
            user_id: &str,
            role_name: &str,
        ) -> Result<()> {
            self.lock()?
                .assignments
                .remove(&(user_id.to_owned(), role_name.to_owned()));
            Ok(())
        }

        async fn list_users(
            &self,
            _tenant_id: &str,
            limit: usize,
        ) -> Result<Vec<IamUser>> {
            Ok(self
                .lock()?
                .users
                .iter()
                .take(limit)
                .map(|(user_id, is_active)| IamUser {
                    user_id: user_id.clone(),
                    is_active: *is_active,
                    created_at_ms: 1,
                    updated_at_ms: 1,
                })
                .collect())
        }

        async fn list_roles(
            &self,
            _tenant_id: &str,
            limit: usize,
        ) -> Result<Vec<IamRole>> {
            Ok(self
                .lock()?
                .roles
                .iter()
                .take(limit)
                .map(|role_name| IamRole {
                    role_name: role_name.clone(),
                    created_at_ms: 1,
                    updated_at_ms: 1,
                })
                .collect())
        }

        async fn list_role_assignments(
            &self,
            _tenant_id: &str,
            limit: usize,
        ) -> Result<Vec<RoleAssignment>> {
            Ok(self
                .lock()?
                .assignments
                .iter()
                .take(limit)
                .map(|(user_id, role_name)| RoleAssignment {
                    user_id: user_id.clone(),
                    role_name: role_name.clone(),
                    created_at_ms: 1,
                })
                .collect())
        }

        async fn role_names_for_user(
            &self,
            _tenant_id: &str,
            user_id: &str,
        ) -> Result<Vec<String>> {
            Ok(self
                .lock()?
                .assignments
                .iter()
                .filter(|(user, _)| user == user_id)
                .map(|(_, role)| role.clone())
                .collect())
        }

        async fn user_ids_for_role(
            &self,
            _tenant_id: &str,
            role_name: &str,
        ) -> Result<Vec<String>> {
            Ok(self
                .lock()?
                .assignments
                .iter()
                .filter(|(_, role)| role == role_name)
                .map(|(user, _)| user.clone())
                .collect())
        }

        async fn get_active_cedar_policies(
            &self,
            _tenant_id: &str,
        ) -> Result<Option<String>> {
            let policies = self.lock()?;
            Ok((!policies.policies.is_empty()).then(|| {
                policies
                    .policies
                    .values()
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("\n")
            }))
        }

        async fn upsert_cedar_policy(
            &self,
            _tenant_id: &str,
            policy_id: &str,
            content: &str,
            _is_active: bool,
        ) -> Result<()> {
            self.lock()?
                .policies
                .insert(policy_id.to_owned(), content.to_owned());
            Ok(())
        }
    }

    #[tokio::test]
    async fn authorized_iam_lifecycle_synchronizes_relationships_and_audit()
    -> Result<()> {
        let iam = Arc::new(MemoryIamStore::default());
        let authorization =
            TestAuthorization::new(AuthorizationDecision::Allow);
        let (audit, audit_store) = audit();
        let service = IamAdminService::new(
            iam.clone(),
            identity(true),
            authorization.clone(),
            audit,
        );
        let context = QueryContext {
            request_id: "request-iam".to_owned(),
            ..QueryContext::default()
        };

        service
            .create_user("tenant_a", "admin", &context, "user_a", true)
            .await?;
        service
            .create_user("tenant_a", "admin", &context, "disabled", false)
            .await?;
        service
            .update_user("tenant_a", "admin", &context, "user_a", false)
            .await?;
        service
            .update_user("tenant_a", "admin", &context, "user_a", true)
            .await?;
        service
            .create_role("tenant_a", "admin", &context, "analyst")
            .await?;
        service
            .assign_role_to_user(
                "tenant_a", "admin", &context, "user_a", "analyst",
            )
            .await?;
        service
            .unassign_role_from_user(
                "tenant_a", "admin", &context, "user_a", "analyst",
            )
            .await?;
        service
            .assign_role_to_user(
                "tenant_a", "admin", &context, "user_a", "analyst",
            )
            .await?;
        service
            .delete_role("tenant_a", "admin", &context, "analyst")
            .await?;
        service
            .create_role("tenant_a", "admin", &context, "operator")
            .await?;
        service
            .assign_role_to_user(
                "tenant_a", "admin", &context, "user_a", "operator",
            )
            .await?;
        service
            .delete_user("tenant_a", "admin", &context, "user_a")
            .await?;
        service
            .set_cedar_policy(
                "tenant_a",
                "admin",
                &context,
                "contextual",
                "permit(principal, action, resource);",
                true,
            )
            .await?;

        let state = iam.lock()?;
        ensure!(!state.users.contains_key("user_a"));
        ensure!(state.users.get("disabled") == Some(&false));
        ensure!(!state.roles.contains("analyst"));
        ensure!(state.roles.contains("operator"));
        ensure!(state.assignments.is_empty());
        ensure!(state.policies.contains_key("contextual"));
        drop(state);
        let mutations = authorization.mutations.lock().map_err(|error| {
            anyhow!("authorization test lock poisoned: {error}")
        })?;
        ensure!(mutations.iter().any(|mutation| {
            mutation.operation == "delete" &&
                mutation.resource_type == "tenant" &&
                mutation.relation == "administrator"
        }));
        drop(mutations);
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(events.len() == 26);
        ensure!(events.iter().all(|event| event.request_id == "request-iam"));
        Ok(())
    }

    #[tokio::test]
    async fn invalid_cedar_policy_is_audited_without_persistence() -> Result<()>
    {
        let iam = Arc::new(MemoryIamStore::default());
        let (audit, audit_store) = audit();
        let service = IamAdminService::new(
            iam.clone(),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            audit,
        );
        let result = service
            .set_cedar_policy(
                "tenant_a",
                "admin",
                &QueryContext::default(),
                "bad/id",
                "not cedar",
                true,
            )
            .await;
        ensure!(result.is_err());
        ensure!(iam.lock()?.policies.is_empty());
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(events.len() == 2);
        ensure!(
            events
                .last()
                .and_then(|event| event.failure_kind.as_deref()) ==
                Some("domain_validation_failed")
        );
        Ok(())
    }
}

impl IamAdminService {
    /// Creates the IAM administration service from reusable domain services.
    pub fn new(
        iam: Arc<dyn IamStore>,
        identity: Arc<IdentityService>,
        auth: Arc<dyn Authorization>,
        audit: Arc<AuditService>,
    ) -> Self {
        Self {
            iam,
            identity,
            auth,
            audit,
        }
    }

    /// Persists an attempt before requiring active tenant administration.
    async fn begin_authorized_operation(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        target: AuditTarget,
    ) -> Result<AuditOperation> {
        let operation = self
            .audit
            .begin(tenant_id, caller_user_id, target, context)
            .await
            .context("Failed to persist audit attempt")?;

        if let Err(error) =
            self.identity.verify_user(tenant_id, caller_user_id).await
        {
            operation
                .denied("identity_denied")
                .await
                .context("Failed to persist identity denial")?;
            return Err(error);
        }

        let authorized = match self
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
        {
            Ok(decision) => decision,
            Err(error) => {
                operation
                    .failed("authorization_dependency_failed")
                    .await
                    .context("Failed to persist authorization failure")?;
                return Err(error)
                    .context("Failed to authorize tenant admin operation");
            },
        };
        if !authorized {
            operation
                .denied("authorization_denied")
                .await
                .context("Failed to persist authorization denial")?;
            bail!("Caller '{caller_user_id}' is not a tenant admin");
        }
        Ok(operation)
    }

    /// Changes a user's active status and synchronizes tenant membership.
    pub async fn update_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        user_id: &str,
        is_active: bool,
    ) -> Result<()> {
        let operation = self
            .begin_authorized_operation(
                tenant_id,
                caller_user_id,
                context,
                AuditTarget::new(AuditAction::UpdateUser, "user", user_id),
            )
            .await?;
        if let Err(error) =
            self.iam.update_user(tenant_id, user_id, is_active).await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        let relationship = if is_active {
            self.auth
                .upsert_relationship(
                    "tenant", tenant_id, "member", "user", user_id,
                )
                .await
        } else {
            self.auth
                .delete_relationship(
                    "tenant", tenant_id, "member", "user", user_id,
                )
                .await
        };
        if let Err(error) = relationship {
            operation.failed("authorization_replication_failed").await?;
            return Err(error);
        }
        self.auth.invalidate_tenant_cache(tenant_id).await;
        operation.succeeded().await
    }

    /// Tombstones a user after removing every known SpiceDB subject relation.
    pub async fn delete_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        user_id: &str,
    ) -> Result<()> {
        let operation = self
            .begin_authorized_operation(
                tenant_id,
                caller_user_id,
                context,
                AuditTarget::new(AuditAction::DeleteUser, "user", user_id),
            )
            .await?;
        let roles =
            match self.iam.role_names_for_user(tenant_id, user_id).await {
                Ok(roles) => roles,
                Err(error) => {
                    operation.failed("database_read_failed").await?;
                    return Err(error);
                },
            };
        for role_name in roles {
            let role_id = format!("{tenant_id}/{role_name}");
            if let Err(error) = self
                .auth
                .delete_relationship(
                    "role", &role_id, "member", "user", user_id,
                )
                .await
            {
                operation.failed("authorization_replication_failed").await?;
                return Err(error);
            }
        }
        for relation in ["member", "administrator"] {
            if let Err(error) = self
                .auth
                .delete_relationship(
                    "tenant", tenant_id, relation, "user", user_id,
                )
                .await
            {
                operation.failed("authorization_replication_failed").await?;
                return Err(error);
            }
        }
        if let Err(error) = self.iam.delete_user(tenant_id, user_id).await {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        self.auth.invalidate_tenant_cache(tenant_id).await;
        operation.succeeded().await
    }

    /// Tombstones a role only after removing all authorization relationships.
    pub async fn delete_role(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        role_name: &str,
    ) -> Result<()> {
        let role_id = format!("{tenant_id}/{role_name}");
        let operation = self
            .begin_authorized_operation(
                tenant_id,
                caller_user_id,
                context,
                AuditTarget::new(AuditAction::DeleteRole, "role", &role_id),
            )
            .await?;
        let users =
            match self.iam.user_ids_for_role(tenant_id, role_name).await {
                Ok(users) => users,
                Err(error) => {
                    operation.failed("database_read_failed").await?;
                    return Err(error);
                },
            };
        for user_id in users {
            if let Err(error) = self
                .auth
                .delete_relationship(
                    "role", &role_id, "member", "user", &user_id,
                )
                .await
            {
                operation.failed("authorization_replication_failed").await?;
                return Err(error);
            }
        }
        for (resource_type, resource_id, relation, subject_type, subject_id) in [
            ("tenant", tenant_id, "role", "role", role_id.as_str()),
            ("role", role_id.as_str(), "parent", "tenant", tenant_id),
        ] {
            if let Err(error) = self
                .auth
                .delete_relationship(
                    resource_type,
                    resource_id,
                    relation,
                    subject_type,
                    subject_id,
                )
                .await
            {
                operation.failed("authorization_replication_failed").await?;
                return Err(error);
            }
        }
        if let Err(error) = self.iam.delete_role(tenant_id, role_name).await {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        self.auth.invalidate_tenant_cache(tenant_id).await;
        operation.succeeded().await
    }

    /// Creates a tenant user and its structural membership relationship.
    pub async fn create_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        new_user_id: &str,
        is_active: bool,
    ) -> Result<()> {
        let operation = self
            .begin_authorized_operation(
                tenant_id,
                caller_user_id,
                context,
                AuditTarget::new(AuditAction::CreateUser, "user", new_user_id),
            )
            .await?;
        if let Err(error) = self
            .iam
            .create_user(tenant_id, new_user_id, is_active)
            .await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        if is_active &&
            let Err(error) = self
                .auth
                .upsert_relationship(
                    "tenant",
                    tenant_id,
                    "member",
                    "user",
                    new_user_id,
                )
                .await
        {
            operation.failed("authorization_replication_failed").await?;
            return Err(error);
        }
        self.auth.invalidate_tenant_cache(tenant_id).await;
        operation.succeeded().await
    }

    /// Creates a tenant-scoped reusable authorization role.
    pub async fn create_role(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        role_name: &str,
    ) -> Result<()> {
        let composite_role_id = format!("{tenant_id}/{role_name}");
        let operation = self
            .begin_authorized_operation(
                tenant_id,
                caller_user_id,
                context,
                AuditTarget::new(
                    AuditAction::CreateRole,
                    "role",
                    &composite_role_id,
                ),
            )
            .await?;
        if let Err(error) = self.iam.create_role(tenant_id, role_name).await {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        if let Err(error) = self
            .auth
            .upsert_relationship(
                "tenant",
                tenant_id,
                "role",
                "role",
                &composite_role_id,
            )
            .await
        {
            operation.failed("authorization_replication_failed").await?;
            return Err(error);
        }
        if let Err(error) = self
            .auth
            .upsert_relationship(
                "role",
                &composite_role_id,
                "parent",
                "tenant",
                tenant_id,
            )
            .await
        {
            operation.failed("authorization_replication_failed").await?;
            return Err(error);
        }
        self.auth.invalidate_tenant_cache(tenant_id).await;
        operation.succeeded().await
    }

    /// Assigns a current active user to a current role.
    pub async fn assign_role_to_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let composite_role_id = format!("{tenant_id}/{role_name}");
        let operation = self
            .begin_authorized_operation(
                tenant_id,
                caller_user_id,
                context,
                AuditTarget::new(
                    AuditAction::AssignRoleToUser,
                    "role_assignment",
                    user_id,
                )
                .with_details(json!({
                    "role_name": role_name,
                    "user_id": user_id,
                })),
            )
            .await?;
        if let Err(error) = self
            .iam
            .assign_role_to_user(tenant_id, user_id, role_name)
            .await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        if let Err(error) = self
            .auth
            .upsert_relationship(
                "role",
                &composite_role_id,
                "member",
                "user",
                user_id,
            )
            .await
        {
            operation.failed("authorization_replication_failed").await?;
            return Err(error);
        }
        self.auth.invalidate_tenant_cache(tenant_id).await;
        operation.succeeded().await
    }

    /// Removes one user-to-role assignment from both security stores.
    pub async fn unassign_role_from_user(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let composite_role_id = format!("{tenant_id}/{role_name}");
        let operation = self
            .begin_authorized_operation(
                tenant_id,
                caller_user_id,
                context,
                AuditTarget::new(
                    AuditAction::UnassignRoleFromUser,
                    "role_assignment",
                    user_id,
                )
                .with_details(json!({
                    "role_name": role_name,
                    "user_id": user_id,
                })),
            )
            .await?;
        if let Err(error) = self
            .auth
            .delete_relationship(
                "role",
                &composite_role_id,
                "member",
                "user",
                user_id,
            )
            .await
        {
            operation.failed("authorization_replication_failed").await?;
            return Err(error);
        }
        if let Err(error) = self
            .iam
            .unassign_role_from_user(tenant_id, user_id, role_name)
            .await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        self.auth.invalidate_tenant_cache(tenant_id).await;
        operation.succeeded().await
    }

    /// Validates and persists one tenant-scoped Cedar policy.
    pub async fn set_cedar_policy(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        policy_id: &str,
        content: &str,
        is_active: bool,
    ) -> Result<()> {
        let policy_id = policy_id.trim();
        let operation = self
            .begin_authorized_operation(
                tenant_id,
                caller_user_id,
                context,
                AuditTarget::new(
                    AuditAction::SetCedarPolicy,
                    "cedar_policy",
                    policy_id,
                ),
            )
            .await?;
        if policy_id.is_empty() ||
            policy_id.len() > 64 ||
            !policy_id.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-'
            })
        {
            operation.failed("domain_validation_failed").await?;
            bail!("Invalid Cedar policy identifier");
        }
        if let Err(error) = validate_cedar_policy(content) {
            operation.failed("domain_validation_failed").await?;
            return Err(error);
        }
        if let Err(error) = self
            .iam
            .upsert_cedar_policy(tenant_id, policy_id, content, is_active)
            .await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        self.auth.invalidate_tenant_cache(tenant_id).await;
        operation.succeeded().await
    }
}

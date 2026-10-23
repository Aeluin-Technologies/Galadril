//! IAM administration use cases with fail-closed authorization and audit.

use std::sync::Arc;

use anyhow::{Context, Result, bail};
use serde_json::json;

use crate::application::ports::access_store::{AccessStore, AccessTuple};
use crate::application::ports::iam_store::{
    CedarPolicy, IamRole, IamStore, IamUser, RoleAssignment,
};
use crate::application::ports::pagination::Page;
use crate::application::usecases::audit::{
    AuditAction, AuditOperation, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext, validate_cedar_policy,
};
use crate::application::usecases::identity::IdentityService;
use crate::domain::directory_label::{
    DirectorySubject, validate_display_name,
};
use crate::domain::key::sanitize_upload_group;

/// Narrow data-domain privileges that a tenant administrator may delegate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DataDomainGrant {
    Ingest,
    Read,
}

impl DataDomainGrant {
    /// Returns the matching SpiceDB relation without accepting free-form
    /// input.
    const fn relation(self) -> &'static str {
        match self {
            Self::Ingest => "ingester",
            Self::Read => "reader",
        }
    }
}

/// An administrator's bounded, single-subject domain grant change.
pub struct DataDomainGrantRequest<'a> {
    pub group_id: &'a str,
    pub subject_user_id: &'a str,
    pub grant: DataDomainGrant,
    pub enabled: bool,
}

/// Restricts editable tuples so administrators cannot change tenant
/// boundaries.
pub(crate) fn validate_resource_access(tuple: &AccessTuple) -> Result<()> {
    let allowed = match tuple.resource_type.as_str() {
        "group" => matches!(tuple.relation.as_str(), "reader" | "ingester"),
        "project" | "table" | "document" | "ontology" | "conversation" => {
            matches!(
                tuple.relation.as_str(),
                "reader" | "editor" | "administrator"
            )
        },
        "pipeline" => matches!(
            tuple.relation.as_str(),
            "reader" | "editor" | "executor" | "administrator"
        ),
        "entity_state" => {
            matches!(tuple.relation.as_str(), "reader" | "editor")
        },
        "raw" | "event" => tuple.relation == "reader",
        _ => false,
    };
    anyhow::ensure!(allowed, "Choose a supported resource permission");
    crate::domain::sso::validate_identifier(&tuple.subject_id)?;
    anyhow::ensure!(
        matches!(
            (tuple.subject_type.as_str(), tuple.subject_relation.as_str()),
            ("user", "") | ("role", "member")
        ),
        "Choose a person or role as the access recipient"
    );
    anyhow::ensure!(
        !tuple.resource_id.is_empty()
            && tuple.resource_id.len() <= 512
            && !tuple.resource_id.chars().any(char::is_control)
            && tuple
                .resource_id
                .split('/')
                .all(|part| !part.is_empty() && part != "." && part != ".."),
        "Invalid resource identifier"
    );
    Ok(())
}

fn validate_search(search: &str) -> Result<()> {
    anyhow::ensure!(
        search.len() <= 128 && !search.chars().any(char::is_control),
        "Search must contain at most 128 characters"
    );
    Ok(())
}

/// Coordinates tenant IAM persistence, authorization relationships, and audit.
pub struct IamAdminService {
    iam: Arc<dyn IamStore>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
    access: Option<Arc<dyn AccessStore>>,
}

#[cfg(test)]
#[expect(
    clippy::items_after_test_module,
    reason = "the local test doubles stay adjacent to the service state they exercise"
)]
pub(crate) mod tests {
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
    pub(crate) struct MemoryIamStore {
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
        async fn search_users_page(
            &self,
            tenant: &str,
            user: &str,
            administrator: bool,
            search: &str,
            limit: usize,
            after: Option<&str>,
        ) -> Result<Vec<IamUser>> {
            let mut users = self
                .search_users(tenant, user, administrator, search, usize::MAX)
                .await?;
            users.retain(|candidate| {
                after.is_none_or(|cursor| candidate.user_id.as_str() > cursor)
            });
            users.sort_unstable_by(|left, right| {
                left.user_id.cmp(&right.user_id)
            });
            users.truncate(limit);
            Ok(users)
        }

        async fn search_roles_page(
            &self,
            tenant: &str,
            user: &str,
            administrator: bool,
            search: &str,
            limit: usize,
            after: Option<&str>,
        ) -> Result<Vec<IamRole>> {
            let mut roles = self
                .search_roles(tenant, user, administrator, search, usize::MAX)
                .await?;
            roles.retain(|role| {
                after.is_none_or(|cursor| role.role_name.as_str() > cursor)
            });
            roles.truncate(limit);
            Ok(roles)
        }

        async fn role_exists(&self, _: &str, role: &str) -> Result<bool> {
            Ok(self.lock()?.roles.contains(role))
        }

        async fn search_users(
            &self,
            tenant: &str,
            user: &str,
            administrator: bool,
            search: &str,
            limit: usize,
        ) -> Result<Vec<IamUser>> {
            let owned = self.role_names_for_user(tenant, user).await?;
            let assignments = self.list_role_assignments(tenant, 100).await?;
            let mut users = self.list_users(tenant, limit).await?;
            users.retain(|candidate| {
                (administrator
                    || candidate.user_id == user
                    || assignments.iter().any(|edge| {
                        edge.user_id == candidate.user_id
                            && owned.contains(&edge.role_name)
                    }))
                    && candidate.user_id.contains(search)
            });
            Ok(users)
        }

        async fn create_user(
            &self,
            _tenant_id: &str,
            user_id: &str,
            is_active: bool,
        ) -> Result<()> {
            self.lock()?.users.insert(user_id.to_owned(), is_active);
            Ok(())
        }

        async fn set_user_guarded(
            &self,
            _: &str,
            actor_id: &str,
            user_id: &str,
            is_active: bool,
            upsert: bool,
        ) -> Result<()> {
            ensure!(
                is_active || actor_id != user_id,
                "You cannot suspend or delete your own account"
            );
            let mut state = self.lock()?;
            ensure!(
                state.users.get(actor_id) == Some(&true),
                "An active administrator must perform this operation"
            );
            ensure!(
                upsert || state.users.contains_key(user_id),
                "User is unavailable"
            );
            state.users.insert(user_id.to_owned(), is_active);
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
                    display_name: None,
                    email: None,
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
            let mut roles =
                self.lock()?.roles.iter().cloned().collect::<Vec<_>>();
            roles.sort_unstable();
            Ok(roles
                .into_iter()
                .take(limit)
                .map(|role_name| IamRole {
                    role_name,
                    display_name: None,
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

    struct MemoryAccessStore {
        tuples: Vec<AccessTuple>,
        writes: Mutex<Vec<AccessTuple>>,
    }

    struct DirectoryAuthorization;

    #[async_trait::async_trait]
    impl Authorization for DirectoryAuthorization {
        async fn is_authorized(
            &self,
            user: &str,
            _: &str,
            permission: Permission,
            _: &str,
            _: &str,
            _: Option<&QueryContext>,
        ) -> Result<bool> {
            Ok(permission != Permission::Manage || user == "privileged")
        }

        async fn upsert_relationship(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
        ) -> Result<()> {
            anyhow::bail!("Unexpected directory mutation")
        }

        async fn delete_relationship(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
        ) -> Result<()> {
            anyhow::bail!("Unexpected directory mutation")
        }

        async fn invalidate_tenant_cache(&self, _: &str) {}
    }

    #[tokio::test]
    async fn shared_subordinate_role_does_not_reveal_tenant_administrators()
    -> Result<()> {
        let iam = Arc::new(MemoryIamStore::default());
        iam.create_role("tenant_a", "junior").await?;
        for user in ["analyst", "privileged"] {
            iam.create_user("tenant_a", user, true).await?;
            iam.assign_role_to_user("tenant_a", user, "junior").await?;
        }
        let (audit, _) = audit();
        let service = IamAdminService::new(
            iam,
            identity(true),
            Arc::new(DirectoryAuthorization),
            audit,
        );
        let users = service
            .users("tenant_a", "analyst", &QueryContext::default(), "", 100)
            .await?;
        ensure!(
            users.len() == 1
                && users.first().is_some_and(|user| user.user_id == "analyst"),
            "Sharing a data role must not disclose a structurally privileged administrator"
        );
        Ok(())
    }

    #[async_trait::async_trait]
    impl AccessStore for MemoryAccessStore {
        async fn tuples(&self, _: &str, _: usize) -> Result<Vec<AccessTuple>> {
            Ok(self.tuples.clone())
        }

        async fn set(
            &self,
            _: &str,
            tuple: &AccessTuple,
            _: bool,
        ) -> Result<()> {
            self.writes
                .lock()
                .map_err(|error| anyhow!("access fixture lock: {error}"))?
                .push(tuple.clone());
            Ok(())
        }
    }

    #[tokio::test]
    async fn resource_access_uses_exact_role_identity_beyond_search_page()
    -> Result<()> {
        let iam = Arc::new(MemoryIamStore::default());
        for index in 0..100 {
            iam.create_role("tenant_a", &format!("a{index:03}dev"))
                .await?;
        }
        iam.create_role("tenant_a", "dev").await?;
        ensure!(
            !iam.search_roles("tenant_a", "admin", true, "dev", 100)
                .await?
                .iter()
                .any(|role| role.role_name == "dev"),
            "The fixture must reproduce an exact role beyond the substring search page"
        );
        let access = Arc::new(MemoryAccessStore {
            tuples: Vec::new(),
            writes: Mutex::default(),
        });
        let (audit, _) = audit();
        let service = IamAdminService::new(
            iam,
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            audit,
        )
        .with_access_store(access.clone());
        let mut tuple = AccessTuple {
            resource_type: "document".into(),
            resource_id: "report".into(),
            relation: "reader".into(),
            subject_type: "role".into(),
            subject_id: "dev".into(),
            subject_relation: "member".into(),
        };
        for enabled in [true, false] {
            service
                .set_resource_access(
                    "tenant_a",
                    "admin",
                    &QueryContext::default(),
                    &tuple,
                    enabled,
                )
                .await?;
        }
        tuple.subject_id = "de".into();
        ensure!(
            service
                .set_resource_access(
                    "tenant_a",
                    "admin",
                    &QueryContext::default(),
                    &tuple,
                    true,
                )
                .await
                .is_err(),
            "A substring match must never stand in for a current role"
        );
        ensure!(
            access
                .writes
                .lock()
                .map_err(|error| anyhow!("access fixture lock: {error}"))?
                .len()
                == 2
        );
        Ok(())
    }

    #[tokio::test]
    async fn security_filtering_does_not_hide_graph_directory_truncation()
    -> Result<()> {
        let iam = Arc::new(MemoryIamStore::default());
        iam.create_role("tenant_a", "junior").await?;
        for user in ["analyst".to_owned(), "privileged".to_owned()]
            .into_iter()
            .chain((0..98).map(|index| format!("member_{index}")))
        {
            iam.create_user("tenant_a", &user, true).await?;
            iam.assign_role_to_user("tenant_a", &user, "junior").await?;
        }
        let (audit, _) = audit();
        let service = IamAdminService::new(
            iam,
            identity(true),
            Arc::new(DirectoryAuthorization),
            audit,
        )
        .with_access_store(Arc::new(MemoryAccessStore {
            tuples: Vec::new(),
            writes: Mutex::default(),
        }));
        let graph = service
            .access_graph(
                "tenant_a",
                "analyst",
                &QueryContext::default(),
                "",
                100,
            )
            .await?;
        ensure!(
            graph.get("truncated") == Some(&json!(true)),
            "Removing privileged principals must not hide a previously reached SQL result cap"
        );
        Ok(())
    }

    #[tokio::test]
    async fn access_graph_filters_unknown_principals_and_enforces_result_limit()
    -> Result<()> {
        let iam = Arc::new(MemoryIamStore::default());
        iam.create_user("tenant_a", "alice", true).await?;
        let make = |subject: &str, id: &str| AccessTuple {
            resource_type: "document".into(),
            resource_id: id.into(),
            relation: "reader".into(),
            subject_type: "user".into(),
            subject_id: subject.into(),
            subject_relation: String::new(),
        };
        let access = Arc::new(MemoryAccessStore {
            tuples: vec![
                make("foreign_user", "private"),
                make("alice", "report-a"),
                make("alice", "report-b"),
            ],
            writes: Mutex::default(),
        });
        let (audit, _) = audit();
        let service = IamAdminService::new(
            iam,
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            audit,
        )
        .with_access_store(access);
        let graph = service
            .access_graph("tenant_a", "admin", &QueryContext::default(), "", 1)
            .await?;
        ensure!(
            graph
                .get("edges")
                .and_then(serde_json::Value::as_array)
                .is_some_and(|edges| edges.len() == 1)
        );
        ensure!(graph.get("truncated") == Some(&json!(true)));
        ensure!(!graph.to_string().contains("foreign_user"));
        ensure!(!graph.to_string().contains("private"));
        Ok(())
    }

    #[tokio::test]
    async fn nonadministrator_cannot_read_graph_or_mutate_resource_access()
    -> Result<()> {
        let (audit, _) = audit();
        let access = Arc::new(MemoryAccessStore {
            tuples: Vec::new(),
            writes: Mutex::default(),
        });
        let service = IamAdminService::new(
            Arc::new(MemoryIamStore::default()),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Deny),
            audit,
        )
        .with_access_store(access.clone());
        ensure!(
            service
                .access_graph(
                    "tenant_a",
                    "member",
                    &QueryContext::default(),
                    "",
                    10
                )
                .await
                .is_err()
        );
        let tuple = AccessTuple {
            resource_type: "document".into(),
            resource_id: "report".into(),
            relation: "reader".into(),
            subject_type: "user".into(),
            subject_id: "alice".into(),
            subject_relation: String::new(),
        };
        ensure!(
            service
                .set_resource_access(
                    "tenant_a",
                    "member",
                    &QueryContext::default(),
                    &tuple,
                    true
                )
                .await
                .is_err()
        );
        ensure!(
            access
                .writes
                .lock()
                .map_err(|error| anyhow!("access fixture lock: {error}"))?
                .is_empty()
        );
        Ok(())
    }

    #[tokio::test]
    async fn administrator_cannot_suspend_or_delete_self_through_any_user_mutation()
    -> Result<()> {
        for operation in ["update", "create", "delete"] {
            let iam = Arc::new(MemoryIamStore::default());
            iam.create_user("tenant_a", "admin", true).await?;
            let authorization =
                TestAuthorization::new(AuthorizationDecision::Allow);
            let (audit, _) = audit();
            let service = IamAdminService::new(
                iam.clone(),
                identity(true),
                authorization.clone(),
                audit,
            );
            let context = QueryContext::default();
            let result = match operation {
                "update" => {
                    service
                        .update_user(
                            "tenant_a", "admin", &context, "admin", false,
                        )
                        .await
                },
                "create" => {
                    service
                        .create_user(
                            "tenant_a", "admin", &context, "admin", false,
                        )
                        .await
                },
                _ => {
                    service
                        .delete_user("tenant_a", "admin", &context, "admin")
                        .await
                },
            };
            ensure!(
                result.is_err(),
                "self-removal accepted through {operation}"
            );
            ensure!(iam.lock()?.users.get("admin") == Some(&true));
            ensure!(
                authorization
                    .mutations
                    .lock()
                    .map_err(|error| anyhow!("mutation lock: {error}"))?
                    .is_empty()
            );
        }
        Ok(())
    }

    #[tokio::test]
    async fn authorized_iam_lifecycle_synchronizes_relationships_and_audit()
    -> Result<()> {
        let iam = Arc::new(MemoryIamStore::default());
        iam.create_user("tenant_a", "admin", true).await?;
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
            mutation.operation == "delete"
                && mutation.resource_type == "tenant"
                && mutation.relation == "administrator"
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
    async fn directory_labels_require_administrator_authorization()
    -> Result<()> {
        let iam = Arc::new(MemoryIamStore::default());
        let (audit, audit_store) = audit();
        let service = IamAdminService::new(
            iam,
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Deny),
            audit,
        );
        let result = service
            .set_display_name(
                "tenant_a",
                "viewer",
                &QueryContext::default(),
                DirectorySubject::Role,
                "analyst",
                "Operations",
            )
            .await;
        ensure!(result.is_err());
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(events.len() == 2);
        ensure!(events.last().is_some_and(
            |event| event.action == AuditAction::SetDirectoryLabel.as_str()
        ));
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
                .and_then(|event| event.failure_kind.as_deref())
                == Some("domain_validation_failed")
        );
        Ok(())
    }

    #[tokio::test]
    async fn domain_grants_require_admin_and_stay_within_one_group()
    -> Result<()> {
        let iam = Arc::new(MemoryIamStore::default());
        let authorization =
            TestAuthorization::new(AuthorizationDecision::Allow);
        let (admin_audit, _) = audit();
        let service = IamAdminService::new(
            iam,
            identity(true),
            authorization.clone(),
            admin_audit,
        );
        service
            .set_data_domain_grant(
                "tenant_a",
                "admin",
                &QueryContext::default(),
                DataDomainGrantRequest {
                    group_id: "sales",
                    subject_user_id: "steward",
                    grant: DataDomainGrant::Ingest,
                    enabled: true,
                },
            )
            .await?;
        {
            let mutations =
                authorization.mutations.lock().map_err(|error| {
                    anyhow!("authorization test lock poisoned: {error}")
                })?;
            ensure!(mutations.iter().any(|mutation| mutation.resource_type
                == "group"
                && mutation.resource_id == "tenant_a/sales"
                && mutation.relation == "ingester"
                && mutation.subject_id == "steward"));
        }
        ensure!(
            service
                .set_data_domain_grant(
                    "tenant_a",
                    "admin",
                    &QueryContext::default(),
                    DataDomainGrantRequest {
                        group_id: "../returns",
                        subject_user_id: "steward",
                        grant: DataDomainGrant::Read,
                        enabled: true,
                    },
                )
                .await
                .is_err()
        );
        let (denied_audit, _) = audit();
        let denied_auth = TestAuthorization::new(AuthorizationDecision::Deny);
        let denied = IamAdminService::new(
            Arc::new(MemoryIamStore::default()),
            identity(true),
            denied_auth.clone(),
            denied_audit,
        );
        ensure!(
            denied
                .set_data_domain_grant(
                    "tenant_a",
                    "steward",
                    &QueryContext::default(),
                    DataDomainGrantRequest {
                        group_id: "sales",
                        subject_user_id: "steward",
                        grant: DataDomainGrant::Read,
                        enabled: true,
                    },
                )
                .await
                .is_err()
        );
        ensure!(
            denied_auth
                .mutations
                .lock()
                .map_err(|error| anyhow!(
                    "authorization test lock poisoned: {error}"
                ))?
                .is_empty()
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
            access: None,
        }
    }

    /// Shares the existing SpiceDB endpoint for bounded administration reads.
    pub fn with_access_store(mut self, access: Arc<dyn AccessStore>) -> Self {
        self.access = Some(access);
        self
    }

    /// Reserves approved role names atomically before publishing their tuples.
    pub async fn create_roles(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        names: &[&str],
    ) -> Result<()> {
        anyhow::ensure!(
            names.len() <= 64,
            "At most 64 role names can be created at once"
        );
        for name in names {
            crate::domain::sso::validate_identifier(name)?;
        }
        let operation = self
            .begin_authorized_operation(
                tenant,
                user,
                context,
                AuditTarget::new(
                    AuditAction::CreateRole,
                    "role_batch",
                    tenant,
                )
                .with_details(json!({"count":names.len()})),
            )
            .await?;
        let result = async {
            self.iam.create_roles(tenant, names).await?;
            for name in names {
                let id = format!("{tenant}/{name}");
                self.auth
                    .upsert_relationship("tenant", tenant, "role", "role", &id)
                    .await?;
                self.auth
                    .upsert_relationship(
                        "role", &id, "parent", "tenant", tenant,
                    )
                    .await?;
            }
            self.auth.invalidate_tenant_cache(tenant).await;
            Ok(())
        }
        .await;
        match result {
            Ok(()) => operation.succeeded().await,
            Err(error) => {
                operation.failed("role_batch_failed").await?;
                Err(error)
            },
        }
    }

    async fn directory_administrator(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
    ) -> Result<bool> {
        self.identity.verify_user(tenant, user).await?;
        let administrator = self
            .auth
            .is_authorized(
                user,
                tenant,
                Permission::Manage,
                "tenant",
                tenant,
                Some(context),
            )
            .await?;
        if !administrator
            && !self
                .auth
                .is_authorized(
                    user,
                    tenant,
                    Permission::View,
                    "tenant",
                    tenant,
                    Some(context),
                )
                .await?
        {
            bail!("Authorization denied");
        }
        Ok(administrator)
    }

    async fn filter_directory_administrators(
        &self,
        tenant: &str,
        caller: &str,
        context: &QueryContext,
        administrator: bool,
        users: &mut Vec<IamUser>,
    ) -> Result<()> {
        if administrator {
            return Ok(());
        }
        // A shared data role does not make a tenant administrator a
        // subordinate. Fully consistent structural checks preserve
        // this separate boundary.
        for index in (0..users.len()).rev() {
            let candidate =
                users.get(index).context("Invalid directory result")?;
            if candidate.user_id != caller
                && self
                    .auth
                    .is_authorized(
                        &candidate.user_id,
                        tenant,
                        Permission::Manage,
                        "tenant",
                        tenant,
                        Some(context),
                    )
                    .await?
            {
                users.remove(index);
            }
        }
        Ok(())
    }

    /// Refills after administrator filtering so invisible users cannot consume
    /// a page.
    pub async fn users_page(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        search: &str,
        limit: usize,
        after: Option<&str>,
    ) -> Result<Page<IamUser>> {
        validate_search(search)?;
        if let Some(cursor) = after {
            crate::domain::sso::validate_identifier(cursor)?;
        }
        let administrator =
            self.directory_administrator(tenant, user, context).await?;
        let limit = limit.clamp(1, 100);
        let mut items = Vec::with_capacity(limit + 1);
        let mut cursor = after.map(str::to_owned);
        while items.len() <= limit {
            let requested = limit + 1 - items.len();
            let mut batch = self
                .iam
                .search_users_page(
                    tenant,
                    user,
                    administrator,
                    search.trim(),
                    requested,
                    cursor.as_deref(),
                )
                .await?;
            let exhausted = batch.len() < requested;
            let Some(last) = batch.last() else { break };
            anyhow::ensure!(
                cursor
                    .as_deref()
                    .is_none_or(|previous| last.user_id.as_str() > previous),
                "Invalid directory cursor"
            );
            cursor = Some(last.user_id.clone());
            self.filter_directory_administrators(
                tenant,
                user,
                context,
                administrator,
                &mut batch,
            )
            .await?;
            items.extend(batch);
            if exhausted {
                break;
            }
        }
        let after = (items.len() > limit)
            .then(|| items.get(limit - 1).map(|user| user.user_id.clone()))
            .flatten();
        items.truncate(limit);
        Ok(Page { items, after })
    }

    /// The role cursor is applied inside the current tenant's live hierarchy
    /// query.
    pub async fn roles_page(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        search: &str,
        limit: usize,
        after: Option<&str>,
    ) -> Result<Page<IamRole>> {
        validate_search(search)?;
        if let Some(cursor) = after {
            crate::domain::sso::validate_identifier(cursor)?;
        }
        let administrator =
            self.directory_administrator(tenant, user, context).await?;
        let limit = limit.clamp(1, 100);
        let mut items = self
            .iam
            .search_roles_page(
                tenant,
                user,
                administrator,
                search.trim(),
                limit + 1,
                after,
            )
            .await?;
        let after = (items.len() > limit)
            .then(|| items.get(limit - 1).map(|role| role.role_name.clone()))
            .flatten();
        items.truncate(limit);
        Ok(Page { items, after })
    }

    /// Assignment values are administrative metadata, independently of
    /// role-name visibility.
    pub async fn assigned_roles(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        subject: &str,
    ) -> Result<Vec<String>> {
        crate::domain::sso::validate_identifier(subject)?;
        anyhow::ensure!(
            self.directory_administrator(tenant, user, context).await?,
            "Authorization denied"
        );
        self.iam.role_names_for_user(tenant, subject).await
    }

    /// Searches before bounding rows so matches beyond the first page remain
    /// usable.
    pub async fn users(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        search: &str,
        limit: usize,
    ) -> Result<Vec<IamUser>> {
        validate_search(search)?;
        let administrator =
            self.directory_administrator(tenant, user, context).await?;
        let mut users = self
            .iam
            .search_users(
                tenant,
                user,
                administrator,
                search.trim(),
                limit.clamp(1, 100),
            )
            .await?;
        self.filter_directory_administrators(
            tenant,
            user,
            context,
            administrator,
            &mut users,
        )
        .await?;
        Ok(users)
    }

    /// Filters both endpoints of membership edges using the directory
    /// boundary.
    pub async fn role_assignments(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        limit: usize,
    ) -> Result<Vec<RoleAssignment>> {
        let administrator =
            self.directory_administrator(tenant, user, context).await?;
        let mut assignments = self
            .iam
            .list_role_assignments(tenant, limit.clamp(1, 100))
            .await?;
        if !administrator {
            let roles =
                self.iam.effective_role_names_for_user(tenant, user).await?;
            let users = self.users(tenant, user, context, "", 100).await?;
            assignments.retain(|assignment| {
                roles.contains(&assignment.role_name)
                    && users
                        .iter()
                        .any(|user| user.user_id == assignment.user_id)
            });
        }
        Ok(assignments)
    }

    /// Loads policy source only after verifying current administrator
    /// authority.
    pub async fn cedar_policies(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
    ) -> Result<Vec<CedarPolicy>> {
        anyhow::ensure!(
            self.directory_administrator(tenant, user, context).await?,
            "Authorization denied"
        );
        self.iam.cedar_policies(tenant).await
    }

    /// Persists hierarchy intent before granting access, and revokes access
    /// first.
    pub async fn set_role_parent(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        role: &str,
        parent: &str,
        enabled: bool,
    ) -> Result<()> {
        crate::domain::sso::validate_identifier(role)?;
        crate::domain::sso::validate_identifier(parent)?;
        let operation = self
            .begin_authorized_operation(
                tenant,
                user,
                context,
                AuditTarget::new(AuditAction::SetRoleParent, "role", role),
            )
            .await?;
        let access = self
            .access
            .as_ref()
            .context("Access management is unavailable")?;
        let result = async {
            self.iam
                .set_role_parent_with_access(
                    tenant,
                    role,
                    parent,
                    enabled,
                    access.as_ref(),
                )
                .await?;
            self.auth.invalidate_tenant_cache(tenant).await;
            Ok(())
        }
        .await;
        match result {
            Ok(()) => operation.succeeded().await,
            Err(error) => {
                operation.failed("role_hierarchy_failed").await?;
                Err(error)
            },
        }
    }

    /// Modifies only existing tenant resources and current tenant principals.
    pub async fn set_resource_access(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        tuple: &AccessTuple,
        enabled: bool,
    ) -> Result<()> {
        validate_resource_access(tuple)?;
        let operation = self
            .begin_authorized_operation(
                tenant,
                user,
                context,
                AuditTarget::new(
                    AuditAction::SetResourceAccess,
                    &tuple.resource_type,
                    &tuple.resource_id,
                ),
            )
            .await?;
        let result = async {
            if tuple.subject_type == "user" {
                self.identity.verify_user(tenant, &tuple.subject_id).await?;
            } else {
                anyhow::ensure!(
                    self.iam.role_exists(tenant, &tuple.subject_id).await?,
                    "Role is unavailable"
                );
            }
            let qualified = format!("{tenant}/{}", tuple.resource_id);
            anyhow::ensure!(
                self.auth
                    .is_authorized(
                        user,
                        tenant,
                        Permission::Manage,
                        &tuple.resource_type,
                        &qualified,
                        Some(context)
                    )
                    .await?,
                "Resource is unavailable or access is denied"
            );
            self.access
                .as_ref()
                .context("Access management is unavailable")?
                .set(tenant, tuple, enabled)
                .await?;
            self.auth.invalidate_tenant_cache(tenant).await;
            Ok(())
        }
        .await;
        match result {
            Ok(()) => operation.succeeded().await,
            Err(error) => {
                operation.failed("resource_access_failed").await?;
                Err(error)
            },
        }
    }

    /// Returns real SpiceDB tuples only when both graph endpoints are visible.
    pub async fn access_graph(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        search: &str,
        limit: usize,
    ) -> Result<serde_json::Value> {
        use std::collections::{BTreeMap, HashSet};
        validate_search(search)?;
        let administrator =
            self.directory_administrator(tenant, user, context).await?;
        let mut users = self
            .iam
            .search_users(tenant, user, administrator, "", 100)
            .await?;
        let directory_truncated = users.len() == 100;
        self.filter_directory_administrators(
            tenant,
            user,
            context,
            administrator,
            &mut users,
        )
        .await?;
        let mut roles = self
            .iam
            .search_roles(tenant, user, administrator, "", 100)
            .await?;
        let access = self
            .access
            .as_ref()
            .context("Access management is unavailable")?;
        let mut anchors = Vec::with_capacity(5);
        let mut anchor_truncated = false;
        if !search.trim().is_empty() {
            let mut matching_users = self
                .iam
                .search_users(tenant, user, administrator, search.trim(), 6)
                .await?;
            anchor_truncated = matching_users.len() == 6;
            self.filter_directory_administrators(
                tenant,
                user,
                context,
                administrator,
                &mut matching_users,
            )
            .await?;
            let matching_roles = self
                .iam
                .search_roles(tenant, user, administrator, search.trim(), 6)
                .await?;
            anchor_truncated |=
                matching_users.len() + matching_roles.len() > 5;
            for role in matching_roles {
                if anchors.len() < 5 {
                    anchors.push(("role", role.role_name.clone()));
                }
                if !roles
                    .iter()
                    .any(|existing| existing.role_name == role.role_name)
                {
                    roles.push(role);
                }
            }
            for principal in matching_users {
                if anchors.len() < 5 {
                    anchors.push(("user", principal.user_id.clone()));
                }
                if !users
                    .iter()
                    .any(|existing| existing.user_id == principal.user_id)
                {
                    users.push(principal);
                }
            }
        }
        let known_users: HashSet<&str> =
            users.iter().map(|u| u.user_id.as_str()).collect();
        let known_roles: HashSet<&str> =
            roles.iter().map(|r| r.role_name.as_str()).collect();
        let bound = limit.clamp(1, 100);
        // One lookahead tuple signals truncation without making a small graph
        // request scan an entire tenant's security index.
        let tuples = if anchors.is_empty() {
            access.tuples(tenant, bound + 1).await?
        } else {
            let mut tuples = Vec::with_capacity(bound + 1);
            let anchor_count = anchors.len();
            for (index, (kind, id)) in anchors.into_iter().enumerate() {
                let remaining = bound + 1 - tuples.len();
                tuples.extend(
                    access
                        .tuples_for_subject(tenant, kind, &id, remaining)
                        .await?,
                );
                if tuples.len() > bound {
                    anchor_truncated |= index + 1 < anchor_count;
                    break;
                }
            }
            tuples
        };
        let mut nodes = BTreeMap::new();
        let mut edges = Vec::new();
        let mut truncated = directory_truncated
            || anchor_truncated
            || tuples.len() > bound
            || users.len() >= 100
            || roles.len() >= 100;
        let needle = search.trim().to_lowercase();
        for tuple in tuples {
            let subject_visible = match tuple.subject_type.as_str() {
                "user" => known_users.contains(tuple.subject_id.as_str()),
                "role" => known_roles.contains(tuple.subject_id.as_str()),
                _ => false,
            };
            if !subject_visible {
                continue;
            }
            let resource_visible = if tuple.resource_type == "role" {
                known_roles.contains(tuple.resource_id.as_str())
            } else if matches!(
                tuple.resource_type.as_str(),
                "group"
                    | "project"
                    | "table"
                    | "raw"
                    | "document"
                    | "ontology"
                    | "pipeline"
                    | "conversation"
                    | "entity_state"
                    | "event"
            ) {
                let qualified = format!("{tenant}/{}", tuple.resource_id);
                self.auth
                    .is_authorized(
                        user,
                        tenant,
                        Permission::View,
                        &tuple.resource_type,
                        &qualified,
                        Some(context),
                    )
                    .await?
            } else {
                false
            };
            if !resource_visible {
                continue;
            }
            let source =
                format!("{}:{}", tuple.subject_type, tuple.subject_id);
            let target =
                format!("{}:{}", tuple.resource_type, tuple.resource_id);
            let subject_label = users
                .iter()
                .find(|u| u.user_id == tuple.subject_id)
                .and_then(|u| u.display_name.as_deref().or(u.email.as_deref()))
                .or_else(|| {
                    roles
                        .iter()
                        .find(|role| role.role_name == tuple.subject_id)
                        .and_then(|role| role.display_name.as_deref())
                })
                .unwrap_or(&tuple.subject_id);
            if !needle.is_empty()
                && !subject_label.to_lowercase().contains(&needle)
                && !tuple.resource_id.to_lowercase().contains(&needle)
                && !tuple.relation.contains(&needle)
            {
                continue;
            }
            if edges.len() >= bound {
                truncated = true;
                break;
            }
            nodes.entry(source.clone()).or_insert_with(||json!({"id":source,"kind":tuple.subject_type,"label":subject_label,"resourceId":tuple.subject_id}));
            let target_label = roles
                .iter()
                .find(|role| {
                    tuple.resource_type == "role"
                        && role.role_name == tuple.resource_id
                })
                .and_then(|role| role.display_name.as_deref())
                .unwrap_or(&tuple.resource_id);
            nodes.entry(target.clone()).or_insert_with(||json!({"id":target,"kind":tuple.resource_type,"label":target_label,"resourceId":tuple.resource_id}));
            edges.push(json!({"source":source,"target":target,"relation":if tuple.resource_type=="role" && tuple.subject_type=="role" && tuple.relation=="member" {"superior"} else {tuple.relation.as_str()}}));
        }
        Ok(
            json!({"nodes":nodes.into_values().collect::<Vec<_>>(),"edges":edges,"truncated":truncated}),
        )
    }

    /// Persists an attempt before requiring active tenant administration.
    pub(crate) async fn begin_authorized_operation(
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

    /// Delegates one bounded data-domain grant after tenant-admin
    /// authorization.
    pub async fn set_data_domain_grant(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        request: DataDomainGrantRequest<'_>,
    ) -> Result<()> {
        let group_id = sanitize_upload_group(Some(request.group_id))?;
        let resource_id = format!("{tenant_id}/{group_id}");
        let operation = self
            .begin_authorized_operation(
                tenant_id,
                caller_user_id,
                context,
                AuditTarget::new(
                    AuditAction::SetDataDomainGrant,
                    "group",
                    &resource_id,
                ),
            )
            .await?;
        if let Err(error) = self
            .identity
            .verify_user(tenant_id, request.subject_user_id)
            .await
        {
            operation.denied("subject_identity_denied").await?;
            return Err(error);
        }
        let relation = request.grant.relation();
        let update = if request.enabled {
            if let Err(error) = self
                .auth
                .upsert_relationship(
                    "group",
                    &resource_id,
                    "parent",
                    "tenant",
                    tenant_id,
                )
                .await
            {
                operation.failed("authorization_replication_failed").await?;
                return Err(error);
            }
            self.auth
                .upsert_relationship(
                    "group",
                    &resource_id,
                    relation,
                    "user",
                    request.subject_user_id,
                )
                .await
        } else {
            self.auth
                .delete_relationship(
                    "group",
                    &resource_id,
                    relation,
                    "user",
                    request.subject_user_id,
                )
                .await
        };
        if let Err(error) = update {
            operation.failed("authorization_replication_failed").await?;
            return Err(error);
        }
        self.auth.invalidate_tenant_cache(tenant_id).await;
        operation.succeeded().await
    }

    /// Audits metadata changes separately from membership and policy mutations.
    pub async fn set_display_name(
        &self,
        tenant: &str,
        caller: &str,
        context: &QueryContext,
        kind: DirectorySubject,
        id: &str,
        name: &str,
    ) -> Result<()> {
        let operation = self
            .begin_authorized_operation(
                tenant,
                caller,
                context,
                AuditTarget::new(
                    AuditAction::SetDirectoryLabel,
                    kind.as_str(),
                    id,
                ),
            )
            .await?;
        let normalized = match crate::domain::sso::validate_identifier(id)
            .and_then(|()| validate_display_name(name))
        {
            Ok(value) => value,
            Err(error) => {
                operation.failed("domain_validation_failed").await?;
                return Err(error);
            },
        };
        if let Err(error) = self
            .iam
            .set_display_name(tenant, kind, id, normalized)
            .await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        operation.succeeded().await
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
        if let Err(error) = self
            .iam
            .set_user_guarded(
                tenant_id,
                caller_user_id,
                user_id,
                is_active,
                false,
            )
            .await
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
        // Disable access before removing administrator relations so concurrent
        // deletions cannot remove both callers' recovery authority.
        if let Err(error) = self
            .iam
            .set_user_guarded(tenant_id, caller_user_id, user_id, false, false)
            .await
        {
            operation.failed("user_state_guard_rejected").await?;
            return Err(error);
        }
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
        if let Some(access) = &self.access {
            if let Err(error) = self
                .iam
                .delete_role_with_access(tenant_id, role_name, access.as_ref())
                .await
            {
                operation.failed("role_lifecycle_change_failed").await?;
                return Err(error);
            }
            self.auth.invalidate_tenant_cache(tenant_id).await;
            return operation.succeeded().await;
        }
        if let Err(error) =
            self.iam.verify_role_deletion(tenant_id, role_name).await
        {
            operation.failed("role_lifecycle_change_failed").await?;
            return Err(error);
        }
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
            .set_user_guarded(
                tenant_id,
                caller_user_id,
                new_user_id,
                is_active,
                true,
            )
            .await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        let relationship = if is_active {
            self.auth
                .upsert_relationship(
                    "tenant",
                    tenant_id,
                    "member",
                    "user",
                    new_user_id,
                )
                .await
        } else {
            self.auth
                .delete_relationship(
                    "tenant",
                    tenant_id,
                    "member",
                    "user",
                    new_user_id,
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

    /// Creates a tenant-scoped reusable authorization role.
    pub async fn create_role(
        &self,
        tenant_id: &str,
        caller_user_id: &str,
        context: &QueryContext,
        role_name: &str,
    ) -> Result<()> {
        crate::domain::sso::validate_identifier(role_name)?;
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
        if let Some(access) = &self.access {
            if let Err(error) = self
                .iam
                .set_role_membership_with_access(
                    tenant_id,
                    user_id,
                    role_name,
                    true,
                    access.as_ref(),
                )
                .await
            {
                operation.failed("role_lifecycle_change_failed").await?;
                return Err(error);
            }
            self.auth.invalidate_tenant_cache(tenant_id).await;
            return operation.succeeded().await;
        }
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
        if let Some(access) = &self.access {
            if let Err(error) = self
                .iam
                .set_role_membership_with_access(
                    tenant_id,
                    user_id,
                    role_name,
                    false,
                    access.as_ref(),
                )
                .await
            {
                operation.failed("role_lifecycle_change_failed").await?;
                return Err(error);
            }
            self.auth.invalidate_tenant_cache(tenant_id).await;
            return operation.succeeded().await;
        }
        if let Err(error) = self
            .iam
            .verify_role_unassignment(tenant_id, user_id, role_name)
            .await
        {
            operation.failed("role_managed_externally").await?;
            return Err(error);
        }
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
        if policy_id.is_empty()
            || policy_id.len() > 64
            || !policy_id.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-'
            })
        {
            operation.failed("domain_validation_failed").await?;
            bail!("Invalid Cedar policy identifier");
        }
        if content.len() > 8192 {
            operation.failed("domain_validation_failed").await?;
            bail!("Access policies must contain at most 8192 bytes");
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

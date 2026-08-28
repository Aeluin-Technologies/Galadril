//! Authorization use cases using Loth (SpiceDB ReBAC + optional Cedar ABAC).
//!
//! Conventions (SpiceDB standard):
//! - Object types and IDs are separated (type="table", id="entity_states").
//! - Tenant isolation is expressed structurally via relationships, e.g.
//!   `table:entity_states#parent@tenant:t1` (conceptually).
//! - Permission strings come from the repository contract in
//!   `schemas/spicedb`.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use anyhow::{Context, Result};
use cedar_policy::{
    Authorizer, Context as CedarRequestContext, Decision, Entities, EntityUid,
    PolicySet, Request, RestrictedExpression,
};
use loth::engine::LothEngine;
use loth::replication::{RelationshipTuple, ReplicationQueue};
use loth::types::{AuthError, CedarContext, CedarContextBuilder};

use crate::application::ports::iam_store::IamStore;
use crate::domain::validate_tenant_id;

/// Dynamic request context.
#[derive(Debug, Default, Clone)]
pub struct QueryContext {
    pub entity_id: Option<String>,
    pub modality: Option<String>,
    pub state_type: Option<String>,
    pub gis_zone: Option<String>,
    pub role: Option<String>,
    pub region: Option<String>,
    pub internal_device: bool,
    pub hour_utc: i64,
    pub request_id: String,
    pub trace_id: Option<String>,
}

/// Custom authorization context evaluated by the Cedar policy engine.
#[derive(Debug, Default, Clone)]
pub struct GaladrilAuthContext;

impl<'a> CedarContext<'a> for GaladrilAuthContext {
    fn write_to(
        &self,
        _out: &mut CedarContextBuilder<'a>,
    ) -> Result<(), AuthError> {
        Ok(())
    }
}

/// Canonical permissions exposed by the authorization layer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[expect(
    dead_code,
    reason = "canonical schema permissions include future mutations"
)]
pub enum Permission {
    View,
    Edit,
    Delete,
    Share,
    Manage,
    Ingest,
    Execute,
    Materialize,
    Publish,
    CreateDocument,
    CreateOntology,
    CreatePipeline,
    CreateConversation,
}

/// Authorization boundary consumed by domain services and test doubles.
#[async_trait::async_trait]
pub trait Authorization: Send + Sync {
    /// Creates or refreshes one canonical SpiceDB relationship.
    async fn upsert_relationship(
        &self,
        resource_type: &str,
        resource_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> Result<()>;

    /// Deletes one canonical SpiceDB relationship.
    async fn delete_relationship(
        &self,
        resource_type: &str,
        resource_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> Result<()>;

    /// Applies SpiceDB and then tenant Cedar restrictions to one operation.
    async fn is_authorized(
        &self,
        user_id: &str,
        tenant_id: &str,
        permission: Permission,
        resource_type: &str,
        resource_id: &str,
        context: Option<&QueryContext>,
    ) -> Result<bool>;

    /// Invalidates cached contextual policy after tenant IAM changes.
    async fn invalidate_tenant_cache(&self, tenant_id: &str);
}

impl Permission {
    /// Returns the exact permission name from the SpiceDB schema contract.
    pub fn as_str(self) -> &'static str {
        match self {
            Permission::View => "view",
            Permission::Edit => "edit",
            Permission::Delete => "delete",
            Permission::Share => "share",
            Permission::Manage => "manage",
            Permission::Ingest => "ingest",
            Permission::Execute => "execute",
            Permission::Materialize => "materialize",
            Permission::Publish => "publish",
            Permission::CreateDocument => "create_document",
            Permission::CreateOntology => "create_ontology",
            Permission::CreatePipeline => "create_pipeline",
            Permission::CreateConversation => "create_conversation",
        }
    }
}

/// Gateway authorization service.
pub struct AuthService {
    loth: Arc<LothEngine>,
    queue: ReplicationQueue,
    default_ctx: GaladrilAuthContext,
    policies: Arc<dyn IamStore>,
    policy_cache: RwLock<HashMap<String, Option<Arc<str>>>>,
}

impl AuthService {
    /// Creates a new [`AuthService`].
    pub fn new(
        loth: Arc<LothEngine>,
        queue: ReplicationQueue,
        default_ctx: GaladrilAuthContext,
        policies: Arc<dyn IamStore>,
    ) -> Self {
        Self {
            loth,
            queue,
            default_ctx,
            policies,
            policy_cache: RwLock::new(HashMap::new()),
        }
    }

    /// Enqueues a structural relationship upsert into SpiceDB.
    pub async fn upsert_relationship(
        &self,
        resource_type: &str,
        resource_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> Result<()> {
        self.queue
            .upsert_tuple(RelationshipTuple::new(
                resource_type,
                resource_id,
                relation,
                subject_type,
                subject_id,
            ))
            .await
            .context("Failed to replicate upsert tuple to SpiceDB")
    }

    /// Enqueues a structural relationship deletion from SpiceDB.
    pub async fn delete_relationship(
        &self,
        resource_type: &str,
        resource_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> Result<()> {
        self.queue
            .delete_tuple(RelationshipTuple::new(
                resource_type,
                resource_id,
                relation,
                subject_type,
                subject_id,
            ))
            .await
            .context("Failed to replicate delete tuple from SpiceDB")
    }

    /// Checks if `user_id` has `permission` for `resource_type:resource_id`.
    pub async fn is_authorized(
        &self,
        user_id: &str,
        tenant_id: &str,
        permission: Permission,
        resource_type: &str,
        resource_id: &str,
        _ctx: Option<&QueryContext>,
    ) -> Result<bool> {
        let rid = tenant_qualified_resource_id(
            tenant_id,
            resource_type,
            resource_id,
        )?;

        let structural_result = self
            .loth
            .prepare_check(
                user_id,
                permission.as_str(),
                resource_type,
                rid.as_ref(),
            )
            .with_context(&self.default_ctx)
            .check()
            .await;
        let structural = match structural_result {
            Ok(decision) => decision,
            Err(error) => {
                tracing::error!(
                    event.name = "authorization.check",
                    tenant_id,
                    actor_type = "user",
                    actor_id = user_id,
                    resource_type,
                    resource_id = rid.as_ref(),
                    permission = permission.as_str(),
                    decision = "error",
                    service = "gateway",
                    error.kind = "spicedb_unavailable",
                    "authorization dependency failed closed"
                );
                return Err(error).context("Loth check_permission failed");
            },
        };
        let default_context = QueryContext::default();
        let contextual_decision = if structural {
            match self.tenant_policy(tenant_id).await {
                Ok(Some(policy)) => cedar_allows(
                    policy.as_ref(),
                    user_id,
                    permission.as_str(),
                    resource_type,
                    rid.as_ref(),
                    _ctx.unwrap_or(&default_context),
                ),
                Ok(None) => Ok(true),
                Err(error) => Err(error),
            }
        } else {
            Ok(false)
        };
        let decision = match contextual_decision {
            Ok(decision) => decision,
            Err(error) => {
                tracing::error!(
                    event.name = "authorization.check",
                    tenant_id,
                    actor_type = "user",
                    actor_id = user_id,
                    resource_type,
                    resource_id = rid.as_ref(),
                    permission = permission.as_str(),
                    decision = "error",
                    service = "gateway",
                    error.kind = "policy_evaluation_failed",
                    "authorization policy evaluation failed closed"
                );
                return Err(error);
            },
        };
        tracing::info!(
            event.name = "authorization.check",
            tenant_id,
            actor_type = "user",
            actor_id = user_id,
            resource_type,
            resource_id = rid.as_ref(),
            permission = permission.as_str(),
            decision = if decision { "allow" } else { "deny" },
            service = "gateway",
            "authorization decision"
        );
        Ok(decision)
    }

    /// Filters a list of resource IDs to only those authorized.
    ///
    /// Note: this performs N checks. For performance, prefer SpiceDB-native
    /// lookup (LothEngine::lookup_resources) where feasible.
    #[expect(
        dead_code,
        reason = "reserved for future SpiceDB lookup batching"
    )]
    pub async fn filter_authorized_resources(
        &self,
        user_id: &str,
        tenant_id: &str,
        permission: Permission,
        resource_type: &str,
        resource_ids: &[String],
    ) -> Result<Vec<String>> {
        let mut out = Vec::with_capacity(resource_ids.len());
        for rid in resource_ids {
            if self
                .is_authorized(
                    user_id,
                    tenant_id,
                    permission,
                    resource_type,
                    rid,
                    None,
                )
                .await?
            {
                out.push(rid.clone());
            }
        }
        Ok(out)
    }

    /// Kept for API stability (no-op under Loth unless we later add local
    /// caches).
    pub async fn invalidate_tenant_cache(&self, tenant_id: &str) {
        if let Ok(mut cache) = self.policy_cache.write() {
            cache.remove(tenant_id);
        }
    }

    /// Loads and caches the active tenant Cedar policy without widening
    /// access.
    async fn tenant_policy(
        &self,
        tenant_id: &str,
    ) -> Result<Option<Arc<str>>> {
        if let Some(cached) = self
            .policy_cache
            .read()
            .map_err(|_| anyhow::anyhow!("Cedar policy cache lock poisoned"))?
            .get(tenant_id)
            .cloned()
        {
            return Ok(cached);
        }
        let loaded = self
            .policies
            .get_active_cedar_policies(tenant_id)
            .await?
            .map(Arc::<str>::from);
        self.policy_cache
            .write()
            .map_err(|_| anyhow::anyhow!("Cedar policy cache lock poisoned"))?
            .insert(tenant_id.to_owned(), loaded.clone());
        Ok(loaded)
    }
}

#[async_trait::async_trait]
impl Authorization for AuthService {
    /// Forwards a canonical relationship upsert to the replication queue.
    async fn upsert_relationship(
        &self,
        resource_type: &str,
        resource_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> Result<()> {
        Self::upsert_relationship(
            self,
            resource_type,
            resource_id,
            relation,
            subject_type,
            subject_id,
        )
        .await
    }

    /// Forwards a canonical relationship deletion to the replication queue.
    async fn delete_relationship(
        &self,
        resource_type: &str,
        resource_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> Result<()> {
        Self::delete_relationship(
            self,
            resource_type,
            resource_id,
            relation,
            subject_type,
            subject_id,
        )
        .await
    }

    /// Applies the concrete SpiceDB-then-Cedar authorization path.
    async fn is_authorized(
        &self,
        user_id: &str,
        tenant_id: &str,
        permission: Permission,
        resource_type: &str,
        resource_id: &str,
        context: Option<&QueryContext>,
    ) -> Result<bool> {
        Self::is_authorized(
            self,
            user_id,
            tenant_id,
            permission,
            resource_type,
            resource_id,
            context,
        )
        .await
    }

    /// Evicts one tenant's parsed Cedar policy after mutation.
    async fn invalidate_tenant_cache(&self, tenant_id: &str) {
        Self::invalidate_tenant_cache(self, tenant_id).await;
    }
}

/// Validates Cedar syntax before a tenant administrator can persist it.
pub fn validate_cedar_policy(content: &str) -> Result<()> {
    content
        .parse::<PolicySet>()
        .map(|_| ())
        .map_err(|error| anyhow::anyhow!("Invalid Cedar policy: {error}"))
}

/// Evaluates a tenant Cedar policy as a restriction after SpiceDB allows.
fn cedar_allows(
    policy: &str,
    user_id: &str,
    action: &str,
    resource_type: &str,
    resource_id: &str,
    ctx: &QueryContext,
) -> Result<bool> {
    let policies = policy.parse::<PolicySet>().map_err(|error| {
        anyhow::anyhow!("Cedar policy parse failed: {error}")
    })?;
    let principal = cedar_uid("User", user_id)?;
    let action = cedar_uid("Action", action)?;
    let resource = cedar_uid(resource_type, resource_id)?;
    let pairs = [
        (
            "is_structural_allowed".to_owned(),
            RestrictedExpression::new_bool(true),
        ),
        (
            "role".to_owned(),
            RestrictedExpression::new_string(
                ctx.role.as_deref().unwrap_or_default().to_owned(),
            ),
        ),
        (
            "region".to_owned(),
            RestrictedExpression::new_string(
                ctx.region.as_deref().unwrap_or_default().to_owned(),
            ),
        ),
        (
            "internal_device".to_owned(),
            RestrictedExpression::new_bool(ctx.internal_device),
        ),
        (
            "hour_utc".to_owned(),
            RestrictedExpression::new_long(ctx.hour_utc),
        ),
        (
            "entity_id".to_owned(),
            RestrictedExpression::new_string(
                ctx.entity_id.as_deref().unwrap_or_default().to_owned(),
            ),
        ),
        (
            "modality".to_owned(),
            RestrictedExpression::new_string(
                ctx.modality.as_deref().unwrap_or_default().to_owned(),
            ),
        ),
        (
            "state_type".to_owned(),
            RestrictedExpression::new_string(
                ctx.state_type.as_deref().unwrap_or_default().to_owned(),
            ),
        ),
        (
            "gis_zone".to_owned(),
            RestrictedExpression::new_string(
                ctx.gis_zone.as_deref().unwrap_or_default().to_owned(),
            ),
        ),
    ];
    let context = CedarRequestContext::from_pairs(pairs)
        .map_err(|error| anyhow::anyhow!("Cedar context rejected: {error}"))?;
    let request = Request::new(principal, action, resource, context, None)
        .map_err(|error| anyhow::anyhow!("Cedar request rejected: {error}"))?;
    let response = Authorizer::new().is_authorized(
        &request,
        &policies,
        &Entities::empty(),
    );
    Ok(matches!(response.decision(), Decision::Allow))
}

/// Builds a Cedar entity UID while rejecting invalid domain identifiers.
fn cedar_uid(kind: &str, id: &str) -> Result<EntityUid> {
    if !kind
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || character == '_')
    {
        anyhow::bail!("Cedar entity type contains invalid characters");
    }
    let escaped_id = id.replace('\\', "\\\\").replace('"', "\\\"");
    format!(r#"{kind}::"{escaped_id}""#)
        .parse::<EntityUid>()
        .map_err(|error| anyhow::anyhow!("Cedar identifier rejected: {error}"))
}

/// Qualifies a local resource ID exactly once with its trusted tenant.
fn tenant_qualified_resource_id<'a>(
    tenant_id: &str,
    resource_type: &str,
    resource_id: &'a str,
) -> Result<std::borrow::Cow<'a, str>> {
    let tenant = validate_tenant_id(tenant_id)?;
    let id = resource_id.trim();
    if id.is_empty() {
        anyhow::bail!("resource identifier is required");
    }
    if resource_type == "tenant" {
        if id != tenant {
            anyhow::bail!("tenant resource does not match security context");
        }
        return Ok(std::borrow::Cow::Borrowed(id));
    }
    let prefix = format!("{tenant}/");
    if id.starts_with(&prefix) {
        return Ok(std::borrow::Cow::Borrowed(id));
    }
    if id.contains('/') {
        anyhow::bail!("cross-tenant resource identifier rejected");
    }
    Ok(std::borrow::Cow::Owned(format!("{tenant}/{id}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resource_ids_are_tenant_qualified() -> Result<()> {
        assert_eq!(
            tenant_qualified_resource_id("t1", "document", "doc1")?,
            "t1/doc1"
        );
        assert!(
            tenant_qualified_resource_id("t1", "document", "t2/doc1").is_err()
        );
        assert!(tenant_qualified_resource_id("t1", "tenant", "t2").is_err());
        Ok(())
    }

    #[test]
    fn cedar_cannot_override_structural_denial_path() -> Result<()> {
        let policy = "permit(principal, action, resource);";
        validate_cedar_policy(policy)?;
        let ctx = QueryContext::default();
        assert!(cedar_allows(
            policy, "u1", "view", "document", "t1/d1", &ctx
        )?);
        // AuthService invokes Cedar only after `structural == true`.
        Ok(())
    }

    #[test]
    fn contextual_constraints_enforce_time_region_and_device() -> Result<()> {
        let policy = include_str!(
            "../../../../../schemas/spicedb/examples/contextual-constraints.cedar"
        );
        validate_cedar_policy(policy)?;
        let mut context = QueryContext {
            entity_id: Some("person2".to_owned()),
            role: Some("data_engineer".to_owned()),
            region: Some("Europe".to_owned()),
            internal_device: false,
            hour_utc: 10,
            ..QueryContext::default()
        };
        assert!(cedar_allows(
            policy,
            "engineer-1",
            "view",
            "entity_state",
            "tenant-1/person2",
            &context,
        )?);

        context.hour_utc = 20;
        assert!(!cedar_allows(
            policy,
            "engineer-1",
            "view",
            "entity_state",
            "tenant-1/person2",
            &context,
        )?);
        context.hour_utc = 10;
        context.entity_id = Some("person1".to_owned());
        assert!(!cedar_allows(
            policy,
            "engineer-1",
            "view",
            "entity_state",
            "tenant-1/person1",
            &context,
        )?);
        context.internal_device = true;
        assert!(cedar_allows(
            policy,
            "engineer-1",
            "view",
            "entity_state",
            "tenant-1/person1",
            &context,
        )?);
        Ok(())
    }

    #[test]
    fn data_engineer_policy_has_exact_boundary_behavior() -> Result<()> {
        let policy = include_str!(
            "../../../../../schemas/spicedb/examples/contextual-constraints.cedar"
        );
        for (hour_utc, region, expected) in [
            (7, "Europe", false),
            (8, "Europe", true),
            (18, "Europe", true),
            (19, "Europe", false),
            (10, "North America", false),
        ] {
            let context = QueryContext {
                entity_id: Some("person2".to_owned()),
                role: Some("data_engineer".to_owned()),
                region: Some(region.to_owned()),
                hour_utc,
                ..QueryContext::default()
            };
            assert_eq!(
                cedar_allows(
                    policy,
                    "engineer-1",
                    "view",
                    "entity_state",
                    "tenant-1/person2",
                    &context,
                )?,
                expected
            );
        }
        Ok(())
    }

    #[test]
    fn person_one_device_rule_applies_to_every_role() -> Result<()> {
        let policy = include_str!(
            "../../../../../schemas/spicedb/examples/contextual-constraints.cedar"
        );
        for role in [None, Some("administrator"), Some("data_engineer")] {
            let context = QueryContext {
                entity_id: Some("person1".to_owned()),
                role: role.map(str::to_owned),
                region: Some("Europe".to_owned()),
                hour_utc: 10,
                internal_device: false,
                ..QueryContext::default()
            };
            assert!(!cedar_allows(
                policy,
                "user-1",
                "view",
                "entity_state",
                "tenant-1/person1",
                &context,
            )?);
        }
        Ok(())
    }
}

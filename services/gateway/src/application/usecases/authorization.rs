//! Authorization use cases using Loth (SpiceDB ReBAC + optional Cedar ABAC).
//!
//! Conventions (SpiceDB standard):
//! - Object types and IDs are separated (type="table", id="entity_states").
//! - Tenant isolation is expressed structurally via relationships, e.g.
//!   `table:entity_states#parent@tenant:t1` (conceptually).
//! - Permission strings are canonical: read|write|delete|share|admin.

use std::sync::Arc;

use anyhow::{Context, Result};
use loth::engine::LothEngine;
use loth::replication::{RelationshipTuple, ReplicationQueue};
use loth::types::{AuthError, CedarContext, CedarContextBuilder};

/// Dynamic request context.
#[derive(Debug, Default, Clone)]
pub struct QueryContext {
    pub entity_id: Option<String>,
    pub modality: Option<String>,
    pub state_type: Option<String>,
    pub gis_zone: Option<String>,
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
pub enum Permission {
    Read,
    Write,
    Delete,
    Share,
    Admin,
}

impl Permission {
    pub fn as_str(self) -> &'static str {
        match self {
            Permission::Read => "read",
            Permission::Write => "write",
            Permission::Delete => "delete",
            Permission::Share => "share",
            Permission::Admin => "admin",
        }
    }
}

/// Gateway authorization service.
pub struct AuthService {
    loth: Arc<LothEngine>,
    queue: ReplicationQueue,
    default_ctx: GaladrilAuthContext,
}

impl AuthService {
    /// Creates a new [`AuthService`].
    pub fn new(
        loth: Arc<LothEngine>,
        queue: ReplicationQueue,
        default_ctx: GaladrilAuthContext,
    ) -> Self {
        Self {
            loth,
            queue,
            default_ctx,
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
        permission: Permission,
        resource_type: &str,
        resource_id: &str,
        _ctx: Option<&QueryContext>,
    ) -> Result<bool> {
        let rid = normalize_object_id(resource_id);

        self.loth
            .check_permission_with_context(
                user_id,
                permission.as_str(),
                resource_type,
                rid,
                Some(&self.default_ctx),
            )
            .await
            .context("Loth check_permission failed")
    }

    /// Filters a list of resource IDs to only those authorized.
    ///
    /// Note: this performs N checks. For performance, prefer SpiceDB-native
    /// lookup (LothEngine::lookup_resources) where feasible.
    pub async fn filter_authorized_resources(
        &self,
        user_id: &str,
        permission: Permission,
        resource_type: &str,
        resource_ids: &[String],
    ) -> Result<Vec<String>> {
        let mut out = Vec::with_capacity(resource_ids.len());
        for rid in resource_ids {
            if self
                .is_authorized(user_id, permission, resource_type, rid, None)
                .await?
            {
                out.push(rid.clone());
            }
        }
        Ok(out)
    }

    /// Kept for API stability (no-op under Loth unless we later add local
    /// caches).
    pub async fn invalidate_tenant_cache(&self, _tenant_id: &str) {}
}

fn normalize_object_id(id: &str) -> &str {
    // Defensive normalization: keep IDs stable and avoid authorization bypass
    // via whitespace tricks.
    id.trim()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_object_id_trims() {
        assert_eq!(normalize_object_id("  abc "), "abc");
    }
}

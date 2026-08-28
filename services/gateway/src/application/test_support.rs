//! Deterministic in-memory security and audit doubles for use-case tests.

use std::sync::{Arc, Mutex};

use anyhow::{Result, anyhow, bail};

use crate::application::ports::audit_store::{
    AuditEvent, AuditFilter, AuditStore, NewAuditEvent,
};
use crate::application::ports::user_directory::{UserDirectory, UserStatus};
use crate::application::usecases::audit::AuditService;
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::application::usecases::identity::IdentityService;

/// Configurable authorization result used to cover fail-closed paths.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AuthorizationDecision {
    Allow,
    Deny,
    Fail,
}

/// Recorded relationship mutation for exact security assertions.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RelationshipMutation {
    pub operation: &'static str,
    pub resource_type: String,
    pub resource_id: String,
    pub relation: String,
    pub subject_type: String,
    pub subject_id: String,
}

/// In-memory authorization boundary with observable relationship writes.
pub(crate) struct TestAuthorization {
    pub decision: AuthorizationDecision,
    pub mutations: Mutex<Vec<RelationshipMutation>>,
}

impl TestAuthorization {
    /// Creates an authorization double with a stable check result.
    pub(crate) fn new(decision: AuthorizationDecision) -> Arc<Self> {
        Arc::new(Self {
            decision,
            mutations: Mutex::new(Vec::new()),
        })
    }

    /// Records one relationship mutation without external side effects.
    fn record(
        &self,
        operation: &'static str,
        resource_type: &str,
        resource_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> Result<()> {
        self.mutations
            .lock()
            .map_err(|error| {
                anyhow!("authorization test lock poisoned: {error}")
            })?
            .push(RelationshipMutation {
                operation,
                resource_type: resource_type.to_owned(),
                resource_id: resource_id.to_owned(),
                relation: relation.to_owned(),
                subject_type: subject_type.to_owned(),
                subject_id: subject_id.to_owned(),
            });
        Ok(())
    }
}

#[async_trait::async_trait]
impl Authorization for TestAuthorization {
    /// Records a relationship upsert for later assertions.
    async fn upsert_relationship(
        &self,
        resource_type: &str,
        resource_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> Result<()> {
        self.record(
            "upsert",
            resource_type,
            resource_id,
            relation,
            subject_type,
            subject_id,
        )
    }

    /// Records a relationship deletion for later assertions.
    async fn delete_relationship(
        &self,
        resource_type: &str,
        resource_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> Result<()> {
        self.record(
            "delete",
            resource_type,
            resource_id,
            relation,
            subject_type,
            subject_id,
        )
    }

    /// Returns the configured structural and contextual decision.
    async fn is_authorized(
        &self,
        _user_id: &str,
        _tenant_id: &str,
        _permission: Permission,
        _resource_type: &str,
        _resource_id: &str,
        _context: Option<&QueryContext>,
    ) -> Result<bool> {
        match self.decision {
            AuthorizationDecision::Allow => Ok(true),
            AuthorizationDecision::Deny => Ok(false),
            AuthorizationDecision::Fail => bail!("authorization unavailable"),
        }
    }

    /// Ignores cache invalidation because this double has no cache.
    async fn invalidate_tenant_cache(&self, _tenant_id: &str) {}
}

/// Identity directory with a stable active/inactive outcome.
struct TestUserDirectory {
    active: bool,
}

#[async_trait::async_trait]
impl UserDirectory for TestUserDirectory {
    /// Returns the configured active or disabled identity state.
    async fn get_user_status(
        &self,
        _tenant_id: &str,
        _user_id: &str,
    ) -> Result<UserStatus> {
        Ok(if self.active {
            UserStatus::Active
        } else {
            UserStatus::Disabled
        })
    }
}

/// Creates a concrete identity service over an in-memory directory.
pub(crate) fn identity(active: bool) -> Arc<IdentityService> {
    Arc::new(IdentityService::new(Arc::new(TestUserDirectory { active })))
}

/// Append-only audit double shared with assertions.
#[derive(Default)]
pub(crate) struct TestAuditStore {
    pub events: Mutex<Vec<NewAuditEvent>>,
}

#[async_trait::async_trait]
impl AuditStore for TestAuditStore {
    /// Appends one immutable event to the observable in-memory log.
    async fn append(&self, event: &NewAuditEvent) -> Result<()> {
        self.events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?
            .push(event.clone());
        Ok(())
    }

    /// Returns an empty read model because use-case tests inspect writes.
    async fn list(
        &self,
        _tenant_id: &str,
        _filter: &AuditFilter,
        _limit: usize,
    ) -> Result<Vec<AuditEvent>> {
        Ok(Vec::new())
    }
}

/// Creates an audit service and its observable append-only store.
pub(crate) fn audit() -> (Arc<AuditService>, Arc<TestAuditStore>) {
    let store = Arc::new(TestAuditStore::default());
    (Arc::new(AuditService::new(store.clone())), store)
}

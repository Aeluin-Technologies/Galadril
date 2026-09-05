//! Durable audit orchestration for sensitive control-plane operations.

use std::sync::Arc;

use anyhow::Result;
use serde_json::Value;
use uuid::Uuid;

use crate::application::ports::audit_store::{
    AuditEvent, AuditFilter, AuditOutcome, AuditStore, NewAuditEvent,
};
use crate::application::usecases::authorization::QueryContext;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
/// Canonical sensitive actions written to durable audit history.
pub enum AuditAction {
    CreateUser,
    CreateRole,
    AssignRoleToUser,
    UnassignRoleFromUser,
    SetCedarPolicy,
    ConversationalQuery,
    CreateConversation,
    UpdateConversation,
    DeleteConversation,
    CreateMessage,
    UpdateMessage,
    DeleteMessage,
    CreatePipeline,
    UpdatePipeline,
    DeletePipeline,
    PublishPipeline,
    UpdateUser,
    DeleteUser,
    DeleteRole,
    ScribeDatabaseQuery,
    PublishOntology,
    RetireOntology,
    RequestStagingUpload,
    CompleteUpload,
}

impl AuditAction {
    /// Returns the stable persistence name used by audit consumers.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::CreateUser => "create_user",
            Self::CreateRole => "create_role",
            Self::AssignRoleToUser => "assign_role_to_user",
            Self::UnassignRoleFromUser => "unassign_role_from_user",
            Self::SetCedarPolicy => "set_cedar_policy",
            Self::ConversationalQuery => "conversational_query",
            Self::CreateConversation => "create_conversation",
            Self::UpdateConversation => "update_conversation",
            Self::DeleteConversation => "delete_conversation",
            Self::CreateMessage => "create_message",
            Self::UpdateMessage => "update_message",
            Self::DeleteMessage => "delete_message",
            Self::CreatePipeline => "create_pipeline",
            Self::UpdatePipeline => "update_pipeline",
            Self::DeletePipeline => "delete_pipeline",
            Self::PublishPipeline => "publish_pipeline",
            Self::UpdateUser => "update_user",
            Self::DeleteUser => "delete_user",
            Self::DeleteRole => "delete_role",
            Self::ScribeDatabaseQuery => "scribe_database_query",
            Self::PublishOntology => "publish_ontology",
            Self::RetireOntology => "retire_ontology",
            Self::RequestStagingUpload => "request_staging_upload",
            Self::CompleteUpload => "complete_upload",
        }
    }
}

/// Identifies the resource and immutable artifacts affected by an operation.
pub struct AuditTarget {
    action: AuditAction,
    resource_type: String,
    resource_id: String,
    revision_id: Option<String>,
    publication_id: Option<String>,
    details: Value,
}

impl AuditTarget {
    /// Creates a target without optional immutable provenance identifiers.
    pub fn new(
        action: AuditAction,
        resource_type: impl Into<String>,
        resource_id: impl Into<String>,
    ) -> Self {
        Self {
            action,
            resource_type: resource_type.into(),
            resource_id: resource_id.into(),
            revision_id: None,
            publication_id: None,
            details: Value::Object(serde_json::Map::new()),
        }
    }

    /// Associates an immutable revision identifier with every audit outcome.
    pub fn with_revision_id(mut self, revision_id: impl Into<String>) -> Self {
        self.revision_id = Some(revision_id.into());
        self
    }

    /// Associates an immutable publication identifier with every audit
    /// outcome.
    pub fn with_publication_id(
        mut self,
        publication_id: impl Into<String>,
    ) -> Self {
        self.publication_id = Some(publication_id.into());
        self
    }

    /// Adds bounded, non-sensitive operation metadata to the audit event.
    pub fn with_details(mut self, details: Value) -> Self {
        self.details = details;
        self
    }
}

/// Persists paired attempted and terminal outcomes for sensitive operations.
pub struct AuditService {
    store: Arc<dyn AuditStore>,
}

impl AuditService {
    /// Creates an audit service over an append-only persistence port.
    pub fn new(store: Arc<dyn AuditStore>) -> Self {
        Self { store }
    }

    /// Persists the attempted outcome before returning an operation guard.
    pub async fn begin(
        &self,
        tenant_id: &str,
        actor_id: &str,
        target: AuditTarget,
        context: &QueryContext,
    ) -> Result<AuditOperation> {
        if !target.details.is_object() {
            anyhow::bail!("Audit details must be a JSON object");
        }
        let operation = AuditOperation {
            store: Arc::clone(&self.store),
            tenant_id: tenant_id.to_owned(),
            operation_id: Uuid::new_v4().simple().to_string(),
            actor_id: actor_id.to_owned(),
            action: target.action.as_str().to_owned(),
            resource_type: target.resource_type,
            resource_id: target.resource_id,
            request_id: context.request_id.clone(),
            trace_id: context.trace_id.clone(),
            revision_id: target.revision_id,
            publication_id: target.publication_id,
            details: target.details,
        };
        operation.record(AuditOutcome::Attempted, None).await?;
        Ok(operation)
    }

    /// Lists bounded tenant audit history through PostgreSQL RLS.
    pub async fn list(
        &self,
        tenant_id: &str,
        filter: &AuditFilter,
        limit: usize,
    ) -> Result<Vec<AuditEvent>> {
        self.store
            .list(tenant_id, filter, limit.clamp(1, 100))
            .await
    }
}

/// Owns immutable identity shared by one attempt and terminal outcome.
pub struct AuditOperation {
    store: Arc<dyn AuditStore>,
    tenant_id: String,
    operation_id: String,
    actor_id: String,
    action: String,
    resource_type: String,
    resource_id: String,
    request_id: String,
    trace_id: Option<String>,
    revision_id: Option<String>,
    publication_id: Option<String>,
    details: Value,
}

impl AuditOperation {
    /// Replaces a provisional audit identifier with the committed native ID.
    pub fn with_revision_id(mut self, revision_id: impl Into<String>) -> Self {
        self.revision_id = Some(revision_id.into());
        self
    }

    /// Appends one immutable outcome and emits matching OTLP trace fields.
    async fn record(
        &self,
        outcome: AuditOutcome,
        failure_kind: Option<&str>,
    ) -> Result<()> {
        let event = NewAuditEvent {
            tenant_id: self.tenant_id.clone(),
            audit_id: Uuid::new_v4().simple().to_string(),
            operation_id: self.operation_id.clone(),
            actor_type: "user".to_owned(),
            actor_id: self.actor_id.clone(),
            action: self.action.clone(),
            resource_type: self.resource_type.clone(),
            resource_id: self.resource_id.clone(),
            outcome,
            failure_kind: failure_kind.map(str::to_owned),
            request_id: self.request_id.clone(),
            trace_id: self.trace_id.clone(),
            revision_id: self.revision_id.clone(),
            publication_id: self.publication_id.clone(),
            details: self.details.clone(),
        };
        self.store.append(&event).await?;
        tracing::info!(
            event.name = "audit.event.persisted",
            tenant_id = self.tenant_id,
            operation_id = self.operation_id,
            actor_type = "user",
            actor_id = self.actor_id,
            action = self.action,
            resource_type = self.resource_type,
            resource_id = self.resource_id,
            outcome = outcome.as_str(),
            failure_kind,
            request_id = self.request_id,
            trace_id = self.trace_id.as_deref(),
            service = "gateway",
            "durable audit event persisted"
        );
        Ok(())
    }

    /// Persists successful completion of the operation.
    pub async fn succeeded(self) -> Result<()> {
        self.record(AuditOutcome::Succeeded, None).await
    }

    /// Persists a dependency or domain failure without sensitive payloads.
    pub async fn failed(self, failure_kind: &str) -> Result<()> {
        self.record(AuditOutcome::Failed, Some(failure_kind)).await
    }

    /// Persists a fail-closed identity or authorization denial.
    pub async fn denied(self, failure_kind: &str) -> Result<()> {
        self.record(AuditOutcome::Denied, Some(failure_kind)).await
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use anyhow::anyhow;

    use super::*;

    #[derive(Default)]
    struct MemoryAuditStore {
        events: Mutex<Vec<NewAuditEvent>>,
    }

    #[async_trait::async_trait]
    impl AuditStore for MemoryAuditStore {
        async fn append(&self, event: &NewAuditEvent) -> Result<()> {
            self.events
                .lock()
                .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?
                .push(event.clone());
            Ok(())
        }

        async fn list(
            &self,
            _tenant_id: &str,
            _filter: &AuditFilter,
            _limit: usize,
        ) -> Result<Vec<AuditEvent>> {
            Ok(Vec::new())
        }
    }

    #[test]
    fn audit_actions_use_canonical_domain_operation_names() {
        assert_eq!(AuditAction::CreateUser.as_str(), "create_user");
        assert_eq!(
            AuditAction::AssignRoleToUser.as_str(),
            "assign_role_to_user"
        );
        assert_eq!(
            AuditAction::ConversationalQuery.as_str(),
            "conversational_query"
        );
    }

    #[tokio::test]
    async fn operation_persists_attempt_and_terminal_outcome() -> Result<()> {
        let store = Arc::new(MemoryAuditStore::default());
        let service = AuditService::new(store.clone());
        let context = QueryContext {
            request_id: "request-1".to_owned(),
            trace_id: Some("0123456789abcdef0123456789abcdef".to_owned()),
            ..QueryContext::default()
        };

        service
            .begin(
                "tenant-1",
                "user-1",
                AuditTarget::new(AuditAction::CreateUser, "user", "user-2"),
                &context,
            )
            .await?
            .succeeded()
            .await?;

        let events = store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        assert_eq!(events.len(), 2);
        let attempted =
            events.first().ok_or_else(|| anyhow!("missing attempt"))?;
        let succeeded =
            events.get(1).ok_or_else(|| anyhow!("missing terminal"))?;
        assert_eq!(attempted.outcome, AuditOutcome::Attempted);
        assert_eq!(succeeded.outcome, AuditOutcome::Succeeded);
        assert_eq!(attempted.operation_id, succeeded.operation_id);
        assert_eq!(succeeded.request_id, "request-1");
        assert_eq!(succeeded.details, serde_json::json!({}));
        Ok(())
    }

    #[tokio::test]
    async fn operation_preserves_non_sensitive_assignment_details()
    -> Result<()> {
        let store = Arc::new(MemoryAuditStore::default());
        let service = AuditService::new(store.clone());
        let context = QueryContext {
            request_id: "request-2".to_owned(),
            ..QueryContext::default()
        };
        let details = serde_json::json!({
            "user_id": "user-2",
            "role_name": "analyst",
        });
        service
            .begin(
                "tenant-1",
                "admin-1",
                AuditTarget::new(
                    AuditAction::AssignRoleToUser,
                    "role_assignment",
                    "user-2",
                )
                .with_revision_id("revision-7")
                .with_publication_id("publication-3")
                .with_details(details.clone()),
                &context,
            )
            .await?
            .succeeded()
            .await?;

        let events = store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        assert!(events.iter().all(|event| event.details == details));
        assert!(events.iter().all(|event| {
            event.revision_id.as_deref() == Some("revision-7") &&
                event.publication_id.as_deref() == Some("publication-3")
        }));
        Ok(())
    }
}

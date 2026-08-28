//! Durable append-only audit persistence contracts.

use anyhow::{Result, bail};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
/// Durable terminal state for one sensitive operation observation.
pub enum AuditOutcome {
    Attempted,
    Succeeded,
    Failed,
    Denied,
}

impl AuditOutcome {
    /// Returns the stable PostgreSQL value.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Attempted => "attempted",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Denied => "denied",
        }
    }

    /// Parses a trusted PostgreSQL value while failing closed on drift.
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "attempted" => Ok(Self::Attempted),
            "succeeded" => Ok(Self::Succeeded),
            "failed" => Ok(Self::Failed),
            "denied" => Ok(Self::Denied),
            _ => bail!("Unknown audit outcome"),
        }
    }
}

#[derive(Debug, Clone)]
/// Input for one append-only audit event.
pub struct NewAuditEvent {
    pub tenant_id: String,
    pub audit_id: String,
    pub operation_id: String,
    pub actor_type: String,
    pub actor_id: String,
    pub action: String,
    pub resource_type: String,
    pub resource_id: String,
    pub outcome: AuditOutcome,
    pub failure_kind: Option<String>,
    pub request_id: String,
    pub trace_id: Option<String>,
    pub revision_id: Option<String>,
    pub publication_id: Option<String>,
    pub details: Value,
}

#[derive(Debug, Clone, PartialEq)]
/// Tenant-scoped audit projection returned by the control plane.
pub struct AuditEvent {
    pub audit_id: String,
    pub operation_id: String,
    pub actor_type: String,
    pub actor_id: String,
    pub action: String,
    pub resource_type: String,
    pub resource_id: String,
    pub outcome: AuditOutcome,
    pub failure_kind: Option<String>,
    pub request_id: String,
    pub trace_id: Option<String>,
    pub revision_id: Option<String>,
    pub publication_id: Option<String>,
    pub details: Value,
    pub occurred_at_ms: i64,
}

#[derive(Debug, Default, Clone)]
/// Optional exact-match filters for bounded audit history queries.
pub struct AuditFilter {
    pub action: Option<String>,
    pub resource_type: Option<String>,
    pub resource_id: Option<String>,
    pub outcome: Option<AuditOutcome>,
}

#[async_trait::async_trait]
/// Append-only persistence boundary for audit events.
pub trait AuditStore: Send + Sync {
    /// Appends one immutable event in the supplied tenant scope.
    async fn append(&self, event: &NewAuditEvent) -> Result<()>;

    /// Lists bounded history without exposing rows from another tenant.
    async fn list(
        &self,
        tenant_id: &str,
        filter: &AuditFilter,
        limit: usize,
    ) -> Result<Vec<AuditEvent>>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_outcomes_round_trip_storage_values() -> Result<()> {
        for outcome in [
            AuditOutcome::Attempted,
            AuditOutcome::Succeeded,
            AuditOutcome::Failed,
            AuditOutcome::Denied,
        ] {
            assert_eq!(AuditOutcome::parse(outcome.as_str())?, outcome);
        }
        assert!(AuditOutcome::parse("unknown").is_err());
        Ok(())
    }
}

//! PostgreSQL append-only audit adapter.

use anyhow::{Context, Result};
use serde_json::Value;
use sqlx::Row;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::audit_store::{
    AuditEvent, AuditFilter, AuditOutcome, AuditStore, NewAuditEvent,
};

const HARD_LIMIT: usize = 100;

pub struct PgAuditStore {
    database: Database,
}

impl PgAuditStore {
    /// Creates an audit store over the shared RLS-aware database pool.
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Converts PostgreSQL timestamps to Unix milliseconds.
    fn to_ms(value: sqlx::types::time::OffsetDateTime) -> i64 {
        value.unix_timestamp() * 1000 +
            i64::from(value.nanosecond()) / 1_000_000
    }
}

#[async_trait::async_trait]
impl AuditStore for PgAuditStore {
    /// Appends one immutable event in a tenant-scoped transaction.
    async fn append(&self, event: &NewAuditEvent) -> Result<()> {
        let mut tx = self.database.tenant(&event.tenant_id).await?;
        sqlx::query(
            r#"
            INSERT INTO audit_events (
                tenant_id, audit_id, operation_id, actor_type, actor_id,
                action, resource_type, resource_id, outcome, failure_kind,
                request_id, trace_id, revision_id, publication_id, details
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15
            )
            "#,
        )
        .bind(&event.tenant_id)
        .bind(&event.audit_id)
        .bind(&event.operation_id)
        .bind(&event.actor_type)
        .bind(&event.actor_id)
        .bind(&event.action)
        .bind(&event.resource_type)
        .bind(&event.resource_id)
        .bind(event.outcome.as_str())
        .bind(&event.failure_kind)
        .bind(&event.request_id)
        .bind(&event.trace_id)
        .bind(&event.revision_id)
        .bind(&event.publication_id)
        .bind(&event.details)
        .execute(&mut *tx)
        .await
        .context("Failed to append audit event")?;
        tx.commit().await.context("Failed to commit audit event")?;
        Ok(())
    }

    /// Lists bounded tenant audit history using exact optional filters.
    async fn list(
        &self,
        tenant_id: &str,
        filter: &AuditFilter,
        limit: usize,
    ) -> Result<Vec<AuditEvent>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let outcome = filter.outcome.map(AuditOutcome::as_str);
        let rows = sqlx::query(
            r#"
            SELECT audit_id, operation_id, actor_type, actor_id, action,
                   resource_type, resource_id, outcome, failure_kind,
                   request_id, trace_id, revision_id, publication_id, details,
                   occurred_at
            FROM audit_events
            WHERE tenant_id = $1
              AND ($2::text IS NULL OR action = $2)
              AND ($3::text IS NULL OR resource_type = $3)
              AND ($4::text IS NULL OR resource_id = $4)
              AND ($5::text IS NULL OR outcome = $5)
            ORDER BY occurred_at DESC, audit_id DESC
            LIMIT $6
            "#,
        )
        .bind(tenant_id)
        .bind(filter.action.as_deref())
        .bind(filter.resource_type.as_deref())
        .bind(filter.resource_id.as_deref())
        .bind(outcome)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list audit events")?;
        tx.commit().await.context("Failed to commit audit read")?;

        let mut events = Vec::with_capacity(rows.len());
        for row in rows {
            let occurred_at: sqlx::types::time::OffsetDateTime =
                row.try_get("occurred_at")?;
            let outcome: String = row.try_get("outcome")?;
            events.push(AuditEvent {
                audit_id: row.try_get("audit_id")?,
                operation_id: row.try_get("operation_id")?,
                actor_type: row.try_get("actor_type")?,
                actor_id: row.try_get("actor_id")?,
                action: row.try_get("action")?,
                resource_type: row.try_get("resource_type")?,
                resource_id: row.try_get("resource_id")?,
                outcome: AuditOutcome::parse(&outcome)?,
                failure_kind: row.try_get("failure_kind")?,
                request_id: row.try_get("request_id")?,
                trace_id: row.try_get("trace_id")?,
                revision_id: row.try_get("revision_id")?,
                publication_id: row.try_get("publication_id")?,
                details: row.try_get::<Value, _>("details")?,
                occurred_at_ms: Self::to_ms(occurred_at),
            });
        }
        Ok(events)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_limit_is_bounded() {
        assert_eq!(1usize.clamp(1, HARD_LIMIT), 1);
        assert_eq!(usize::MAX.clamp(1, HARD_LIMIT), HARD_LIMIT);
    }
}

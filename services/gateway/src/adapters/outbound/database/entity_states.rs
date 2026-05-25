//! PostgreSQL adapter for searching and fetching entity states.

use anyhow::{Context, Result, bail};
use serde_json::Value;
use sqlx::{PgPool, Row};

use crate::adapters::outbound::database::tenant_schema::begin_tenant_tx;
use crate::application::ports::entity_state_store::{
    EntityStateRow, EntityStateStore,
};

const HARD_LIMIT: usize = 50;

pub struct PgEntityStateStore {
    pool: PgPool,
}

impl PgEntityStateStore {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    fn clamp_limit(limit: usize) -> i64 {
        let capped = limit.clamp(1, HARD_LIMIT);
        capped as i64
    }

    fn normalize_query(q: &str) -> Result<&str> {
        let s = q.trim();
        if s.is_empty() {
            bail!("Search query is empty");
        }
        Ok(s)
    }

    fn to_created_at_ms(dt: sqlx::types::time::OffsetDateTime) -> i64 {
        // OffsetDateTime::unix_timestamp() is seconds; nanos are available too.
        // Use ms for compactness and UI friendliness.
        dt.unix_timestamp() * 1000 + (dt.nanosecond() as i64 / 1_000_000)
    }
}

#[async_trait::async_trait]
impl EntityStateStore for PgEntityStateStore {
    async fn search_by_name(
        &self,
        tenant_id: &str,
        query: &str,
        limit: usize,
    ) -> Result<Vec<EntityStateRow>> {
        let q = Self::normalize_query(query)?;
        let lim = Self::clamp_limit(limit);

        let mut tx = begin_tenant_tx(&self.pool, tenant_id).await?;

        // NOTE: The physical schema stores state payload in `state_value` and
        // the hypertable time column is `event_time`.
        //
        // We only return the subset needed by the UI: entity_id, a "metadata"
        // JSON object (currently the full state_value), optional state_type,
        // and created_at_ms derived from event_time.
        let rows = sqlx::query(
            r#"
            SELECT entity_id, state_value, state_type, event_time
            FROM entity_states
            WHERE (state_value->>'name') ILIKE '%' || $1 || '%'
            ORDER BY event_time DESC
            LIMIT $2
            "#,
        )
        .bind(q)
        .bind(lim)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to search entity_states by state_value.name")?;

        tx.commit()
            .await
            .context("Failed to commit tenant search transaction")?;

        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let entity_id: String =
                row.try_get("entity_id").context("Missing entity_id")?;
            let state_value: Value =
                row.try_get("state_value").context("Missing state_value")?;
            let state_type: Option<String> = row.try_get("state_type").ok();

            let created_at_ms: Option<i64> = row
                .try_get::<Option<sqlx::types::time::OffsetDateTime>, _>(
                    "event_time",
                )
                .ok()
                .flatten()
                .map(Self::to_created_at_ms);

            out.push(EntityStateRow {
                entity_id,
                metadata: state_value,
                state_type,
                created_at_ms,
            });
        }

        Ok(out)
    }

    async fn latest_states_for_entity(
        &self,
        tenant_id: &str,
        entity_id: &str,
        limit: usize,
    ) -> Result<Vec<EntityStateRow>> {
        let lim = Self::clamp_limit(limit);

        let mut tx = begin_tenant_tx(&self.pool, tenant_id).await?;

        let rows = sqlx::query(
            r#"
            SELECT entity_id, state_value, state_type, event_time
            FROM entity_states
            WHERE entity_id = $1
            ORDER BY event_time DESC
            LIMIT $2
            "#,
        )
        .bind(entity_id)
        .bind(lim)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to fetch latest entity_states")?;

        tx.commit()
            .await
            .context("Failed to commit tenant latest_states transaction")?;

        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let entity_id: String =
                row.try_get("entity_id").context("Missing entity_id")?;
            let state_value: Value =
                row.try_get("state_value").context("Missing state_value")?;
            let state_type: Option<String> = row.try_get("state_type").ok();

            let created_at_ms: Option<i64> = row
                .try_get::<Option<sqlx::types::time::OffsetDateTime>, _>(
                    "event_time",
                )
                .ok()
                .flatten()
                .map(Self::to_created_at_ms);

            out.push(EntityStateRow {
                entity_id,
                metadata: state_value,
                state_type,
                created_at_ms,
            });
        }

        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_limit_enforces_hard_bound() {
        assert_eq!(PgEntityStateStore::clamp_limit(0), 1);
        assert_eq!(PgEntityStateStore::clamp_limit(1), 1);
        assert_eq!(PgEntityStateStore::clamp_limit(999), 50);
    }

    #[test]
    fn normalize_query_rejects_empty() {
        assert!(PgEntityStateStore::normalize_query("").is_err());
        assert!(PgEntityStateStore::normalize_query("   ").is_err());
        assert_eq!(PgEntityStateStore::normalize_query("  x ").unwrap(), "x");
    }
}

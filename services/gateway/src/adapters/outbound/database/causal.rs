//! RLS applies before the application projects a root-scoped analysis.

use anyhow::Result;
use sqlx::Row as _;

use super::connection::Database;
use crate::application::ports::causal_store::{CausalRun, CausalStore};

pub struct PgCausalStore {
    database: Database,
}

impl PgCausalStore {
    pub fn new(database: Database) -> Self {
        Self { database }
    }
}

#[async_trait::async_trait]
impl CausalStore for PgCausalStore {
    async fn latest_for_entity(
        &self,
        tenant_id: &str,
        entity_id: &str,
    ) -> Result<Option<CausalRun>> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let exists: bool = sqlx::query_scalar(
            "SELECT to_regclass('public.causal_runs') IS NOT NULL",
        )
        .fetch_one(&mut *transaction)
        .await?;
        if !exists {
            return Ok(None);
        }
        let row = sqlx::query(
            "SELECT cache_key, result_summary FROM causal_runs
             WHERE tenant_id = $1 AND target = $2 AND status = 'success'
             ORDER BY created_at DESC LIMIT 1",
        )
        .bind(tenant_id)
        .bind(format!("entity:{entity_id}"))
        .fetch_optional(&mut *transaction)
        .await?;
        let result = row
            .map(|row| -> Result<CausalRun> {
                Ok(CausalRun {
                    cache_key: row.try_get("cache_key")?,
                    summary: row.try_get("result_summary")?,
                })
            })
            .transpose()?;
        transaction.commit().await?;
        Ok(result)
    }
}

//! PostgreSQL implementation of cross-domain search.
//!
//! SECURITY:
//! - Always constrain by tenant_id in SQL.
//! - Callers must still apply SpiceDB/Loth filtering by entity_id.

use anyhow::{Context, Result, bail};
use pgvector::Vector;
use serde_json::Value;
use sqlx::{PgPool, Row};

use crate::adapters::outbound::database::tenant_schema::begin_tenant_tx;
use crate::application::ports::search_store::{
    EmbeddingRow, EventRow, SearchStore,
};

const HARD_LIMIT: usize = 50;

pub struct PgSearchStore {
    pool: PgPool,
}

impl PgSearchStore {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    fn clamp_limit(limit: usize) -> i64 {
        (limit.clamp(1, HARD_LIMIT)) as i64
    }

    fn to_ms(dt: sqlx::types::time::OffsetDateTime) -> i64 {
        dt.unix_timestamp() * 1000 + (dt.nanosecond() as i64 / 1_000_000)
    }

    fn embedding_to_vector(embedding: &[f32; 1024]) -> Vector {
        // pgvector::Vector owns a Vec<f32>; unavoidable heap allocation for
        // SQL binding. Keep it bounded and deterministic (1024 floats).
        Vector::from(embedding.to_vec())
    }
}

#[async_trait::async_trait]
impl SearchStore for PgSearchStore {
    async fn search_events(
        &self,
        tenant_id: &str,
        event_type: Option<&str>,
        text: Option<&str>,
        limit: usize,
    ) -> Result<Vec<EventRow>> {
        let lim = Self::clamp_limit(limit);

        let mut tx = begin_tenant_tx(&self.pool, tenant_id).await?;
        let rows = if let Some(et) = event_type {
            if let Some(t) = text {
                sqlx::query(
                    r#"
                    SELECT event_id, event_type, event_time, properties
                    FROM eskg_events
                    WHERE tenant_id = $1
                      AND event_type = $2
                      AND properties::text ILIKE '%' || $3 || '%'
                    ORDER BY event_time DESC
                    LIMIT $4
                    "#,
                )
                .bind(tenant_id)
                .bind(et)
                .bind(t)
                .bind(lim)
                .fetch_all(&mut *tx)
                .await
                .context("Failed to search eskg_events by type+text")?
            } else {
                sqlx::query(
                    r#"
                    SELECT event_id, event_type, event_time, properties
                    FROM eskg_events
                    WHERE tenant_id = $1
                      AND event_type = $2
                    ORDER BY event_time DESC
                    LIMIT $3
                    "#,
                )
                .bind(tenant_id)
                .bind(et)
                .bind(lim)
                .fetch_all(&mut *tx)
                .await
                .context("Failed to search eskg_events by type")?
            }
        } else if let Some(t) = text {
            sqlx::query(
                r#"
                SELECT event_id, event_type, event_time, properties
                FROM eskg_events
                WHERE tenant_id = $1
                  AND properties::text ILIKE '%' || $2 || '%'
                ORDER BY event_time DESC
                LIMIT $3
                "#,
            )
            .bind(tenant_id)
            .bind(t)
            .bind(lim)
            .fetch_all(&mut *tx)
            .await
            .context("Failed to search eskg_events by text")?
        } else {
            bail!("search_events requires event_type or text");
        };

        tx.commit()
            .await
            .context("Failed to commit event search tx")?;

        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let event_id: String = row.try_get("event_id")?;
            let event_type: String = row.try_get("event_type")?;
            let event_time: sqlx::types::time::OffsetDateTime =
                row.try_get("event_time")?;
            let properties: Value = row.try_get("properties")?;

            out.push(EventRow {
                event_id,
                event_type,
                event_time_ms: Self::to_ms(event_time),
                properties,
            });
        }

        Ok(out)
    }

    async fn search_embeddings_top_k(
        &self,
        tenant_id: &str,
        modality: Option<&str>,
        embedding: &[f32; 1024],
        k: usize,
    ) -> Result<Vec<EmbeddingRow>> {
        let lim = Self::clamp_limit(k);
        let mut tx = begin_tenant_tx(&self.pool, tenant_id).await?;

        let qv = Self::embedding_to_vector(embedding);

        // Use `<->` (L2 distance) as a simple default.
        // TODO: swap to cosine distance?
        let rows = if let Some(m) = modality {
            sqlx::query(
                r#"
                SELECT id, entity_id, modality, created_at, metadata, (embedding <-> $1) AS score
                FROM entity_embeddings
                WHERE tenant_id = $2
                  AND modality = $3
                ORDER BY embedding <-> $1 ASC
                LIMIT $4
                "#,
            )
            .bind(qv)
            .bind(tenant_id)
            .bind(m)
            .bind(lim)
            .fetch_all(&mut *tx)
            .await
            .context("Failed to ANN search entity_embeddings (tenant+modality)")?
        } else {
            sqlx::query(
                r#"
                SELECT id, entity_id, modality, created_at, metadata, (embedding <-> $1) AS score
                FROM entity_embeddings
                WHERE tenant_id = $2
                ORDER BY embedding <-> $1 ASC
                LIMIT $3
                "#,
            )
            .bind(qv)
            .bind(tenant_id)
            .bind(lim)
            .fetch_all(&mut *tx)
            .await
            .context("Failed to ANN search entity_embeddings (tenant)")?
        };

        tx.commit()
            .await
            .context("Failed to commit embedding search tx")?;

        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let id: Option<String> = row.try_get("id").ok();
            let entity_id: String = row.try_get("entity_id")?;
            let modality: String = row.try_get("modality")?;
            let created_at: sqlx::types::time::OffsetDateTime =
                row.try_get("created_at")?;
            let metadata: Value = row.try_get("metadata")?;
            let score: f64 = row.try_get("score").unwrap_or(0.0);

            out.push(EmbeddingRow {
                id,
                entity_id,
                modality,
                created_at_ms: Self::to_ms(created_at),
                metadata,
                score: score as f32,
            });
        }

        Ok(out)
    }
}

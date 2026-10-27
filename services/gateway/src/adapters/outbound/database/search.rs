//! PostgreSQL implementation of cross-domain search.
//!
//! SECURITY:
//! - Always constrain by tenant_id in SQL.
//! - Callers must still apply SpiceDB/Loth filtering by entity_id.

use anyhow::{Context, Result, bail};
use serde_json::Value;
use sqlx::Row;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::search_store::{
    EmbeddingRow, EventRow, SearchStore,
};

const HARD_LIMIT: usize = 50;

pub struct PgSearchStore {
    database: Database,
}

impl PgSearchStore {
    /// Creates the PostgreSQL cross-domain search adapter.
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Bounds SQL result counts to the public search contract.
    fn clamp_limit(limit: usize) -> i64 {
        (limit.clamp(1, HARD_LIMIT)) as i64
    }

    /// Converts PostgreSQL timestamps to Unix milliseconds.
    fn to_ms(dt: sqlx::types::time::OffsetDateTime) -> i64 {
        dt.unix_timestamp() * 1000 + (dt.nanosecond() as i64 / 1_000_000)
    }

    /// Adapts the fixed request vector to pgvector's owned bind value.
    fn embedding_to_vector(embedding: &[f32; 1024]) -> Vec<f32> {
        // pgvector::Vector owns a Vec<f32>; unavoidable heap allocation for
        // SQL binding. Keep it bounded and deterministic (1024 floats).
        embedding.to_vec()
    }
}

#[async_trait::async_trait]
impl SearchStore for PgSearchStore {
    async fn evidence_page(
        &self,
        tenant: &str,
        filter: &crate::domain::evidence::EvidenceFilter,
        query: Option<&str>,
        offset: u32,
        limit: usize,
    ) -> Result<Vec<crate::domain::evidence::EvidenceRow>> {
        let mut tx = self.database.tenant(tenant).await?;
        let rows=sqlx::query(r#"
            SELECT COALESCE(p.source_id,e.properties->>'record_id',e.event_id) AS source_id,
                   e.event_id,e.event_time,COALESCE(p.unit_id,e.event_id) AS unit_id,
                   COALESCE(p.metadata,e.properties->'source_metadata','{}'::jsonb) AS metadata,
                   COALESCE(p.text,'') AS text,COALESCE(p.facts,'[]'::jsonb) AS facts
            FROM eskg_events e
            LEFT JOIN LATERAL (
                SELECT source_id,unit_id,text,metadata,facts FROM pipeline_evidence pe
                WHERE pe.tenant_id=e.tenant_id AND pe.event_id=e.event_id
                ORDER BY unit_id
                LIMIT CASE WHEN $9::text IS NULL THEN 1 ELSE 128 END
            ) p ON TRUE
            WHERE e.tenant_id=$1 AND e.event_time>=to_timestamp($2::double precision/1000)
              AND e.event_time<to_timestamp($3::double precision/1000)
              AND ($4::text IS NULL OR lower(COALESCE(p.metadata,e.properties->'source_metadata')->>'sender')=$4)
              AND ($5::text IS NULL OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(COALESCE(p.metadata,e.properties->'source_metadata')->'recipients','[]'::jsonb)) address WHERE lower(address)=$5))
              AND ($6::text IS NULL OR COALESCE(p.metadata,e.properties->'source_metadata')->>'thread_id'=$6)
              AND ($7::text IS NULL OR EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(e.properties->'document_evidence','[]'::jsonb)) doc WHERE doc->>'source_id'=$7))
              AND ($8::text IS NULL OR e.properties->>'ontology_id'=$8)
              AND ($9::text IS NULL OR (p.unit_id IS NOT NULL AND (search_text_matches(p.text,$9) OR search_text_matches(p.metadata::text,$9))))
            ORDER BY e.event_time,e.event_id,p.unit_id
            LIMIT $10 OFFSET $11
        "#).bind(tenant).bind(filter.from_ms).bind(filter.until_ms).bind(&filter.sender).bind(&filter.recipient).bind(&filter.thread_id).bind(&filter.document_id).bind(&filter.ontology_id).bind(query).bind(Self::clamp_limit(limit)).bind(i64::from(offset)).fetch_all(&mut *tx).await.context("Evidence candidates unavailable")?;
        let mut results = Vec::with_capacity(rows.len());
        for row in rows {
            let metadata: Value = row.try_get("metadata")?;
            results.push(crate::domain::evidence::EvidenceRow {
                source_id: row.try_get("source_id")?,
                event_id: row.try_get("event_id")?,
                event_time_ms: Self::to_ms(row.try_get("event_time")?),
                unit_id: row.try_get("unit_id")?,
                title: metadata
                    .get("subject")
                    .or_else(|| metadata.get("name"))
                    .and_then(Value::as_str)
                    .unwrap_or("Document")
                    .into(),
                text: row.try_get("text")?,
                metadata,
                facts: serde_json::from_value(row.try_get("facts")?)?,
                score: 0.,
            });
        }
        tx.commit()
            .await
            .context("Evidence transaction unavailable")?;
        Ok(results)
    }

    /// Searches tenant events with parameterized structured constraints.
    async fn search_events(
        &self,
        tenant_id: &str,
        event_type: Option<&str>,
        text: Option<&str>,
        limit: usize,
    ) -> Result<Vec<EventRow>> {
        self.search_events_page(tenant_id, event_type, text, limit, 0)
            .await
    }

    async fn search_events_page(
        &self,
        tenant_id: &str,
        event_type: Option<&str>,
        text: Option<&str>,
        limit: usize,
        offset: u32,
    ) -> Result<Vec<EventRow>> {
        let lim = Self::clamp_limit(limit);

        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = if let Some(et) = event_type {
            if let Some(t) = text {
                sqlx::query(
                    r#"
                    SELECT event_id, event_type, event_time, properties
                    FROM eskg_events
                    WHERE tenant_id = $1
                      AND event_type = $2
                      AND search_text_matches(properties::text, $3)
                    ORDER BY event_time DESC, event_id, event_type
                    LIMIT $4 OFFSET $5
                    "#,
                )
                .bind(tenant_id)
                .bind(et)
                .bind(t)
                .bind(lim)
                .bind(i64::from(offset))
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
                    ORDER BY event_time DESC, event_id, event_type
                    LIMIT $3 OFFSET $4
                    "#,
                )
                .bind(tenant_id)
                .bind(et)
                .bind(lim)
                .bind(i64::from(offset))
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
                  AND search_text_matches(properties::text, $2)
                ORDER BY event_time DESC, event_id, event_type
                LIMIT $3 OFFSET $4
                "#,
            )
            .bind(tenant_id)
            .bind(t)
            .bind(lim)
            .bind(i64::from(offset))
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

    /// Executes a bounded tenant nearest-neighbor search through RLS.
    async fn search_embeddings_top_k(
        &self,
        tenant_id: &str,
        modality: Option<&str>,
        embedding: &[f32; 1024],
        k: usize,
    ) -> Result<Vec<EmbeddingRow>> {
        self.search_embeddings_page(tenant_id, modality, embedding, k, 0)
            .await
    }

    async fn search_embeddings_page(
        &self,
        tenant_id: &str,
        modality: Option<&str>,
        embedding: &[f32; 1024],
        k: usize,
        offset: u32,
    ) -> Result<Vec<EmbeddingRow>> {
        let lim = Self::clamp_limit(k);
        let mut tx = self.database.tenant(tenant_id).await?;

        let qv = Self::embedding_to_vector(embedding);

        // Use `<->` (L2 distance) as a simple default.
        // TODO: swap to cosine distance?
        let rows = if let Some(m) = modality {
            sqlx::query(
                r#"
                SELECT id, entity_id, modality, created_at, metadata, (embedding <-> $1::real[]::vector) AS score
                FROM entity_embeddings
                WHERE tenant_id = $2
                  AND modality = $3
                ORDER BY embedding <-> $1::real[]::vector ASC, id
                LIMIT $4 OFFSET $5
                "#,
            )
            .bind(qv)
            .bind(tenant_id)
            .bind(m)
            .bind(lim)
            .bind(i64::from(offset))
            .fetch_all(&mut *tx)
            .await
            .context("Failed to ANN search entity_embeddings (tenant+modality)")?
        } else {
            sqlx::query(
                r#"
                SELECT id, entity_id, modality, created_at, metadata, (embedding <-> $1::real[]::vector) AS score
                FROM entity_embeddings
                WHERE tenant_id = $2
                ORDER BY embedding <-> $1::real[]::vector ASC, id
                LIMIT $3 OFFSET $4
                "#,
            )
            .bind(qv)
            .bind(tenant_id)
            .bind(lim)
            .bind(i64::from(offset))
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
            let score: f64 = row.try_get("score")?;

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

#[cfg(test)]
mod evidence_tests {
    use testcontainers_modules::postgres::Postgres;
    use testcontainers_modules::testcontainers::ImageExt as _;
    use testcontainers_modules::testcontainers::runners::AsyncRunner;

    use super::*;
    use crate::domain::evidence::EvidenceFilter;

    #[tokio::test]
    async fn evidence_filters_use_canonical_events_and_keep_tenants_separate()
    -> Result<()> {
        let container=Postgres::default().with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app; CREATE EXTENSION pg_trgm;".to_vec()).with_tag("18.6-alpine").start().await?;
        let database = Database::connect(&format!(
            "postgres://galadril_app:galadril_app@{}:{}/postgres",
            container.get_host().await?,
            container.get_host_port_ipv4(5432).await?
        ))
        .await?;
        let mut tx = database.tenant("tenant_a").await?;
        sqlx::query("CREATE TABLE eskg_events(tenant_id text,event_id text,event_time timestamptz,properties jsonb)").execute(&mut *tx).await?;
        for (tenant, event, at) in [
            ("tenant_a", "event-1", 1000_i64),
            ("tenant_a", "event-2", 2000),
            ("tenant_b", "hidden", 1000),
        ] {
            sqlx::query("INSERT INTO eskg_events VALUES($1,$2,to_timestamp($3::double precision/1000),$4)").bind(tenant).bind(event).bind(at).bind(serde_json::json!({"record_id":"mail-1","source_metadata":{"subject":"Examens","sender":"alice@example.org","recipients":["Bob@Example.org"],"thread_id":"thread"},"ontology_id":"legal","document_evidence":[{"source_id":"a".repeat(32),"checksum":"b".repeat(64)}]})).execute(&mut *tx).await?;
        }
        for unit in ["a".repeat(64), "b".repeat(64)] {
            sqlx::query("INSERT INTO pipeline_evidence(tenant_id,source_id,unit_id,event_id,event_time,checksum,text,metadata,facts,profile_id,embedding,pipeline_id,revision_id) VALUES('tenant_a','mail-1',$1,'event-1',to_timestamp(1),$2,'Examens', $3,'[]',$2,ARRAY[0.1::real],'mail',$2)").bind(unit).bind("c".repeat(64)).bind(serde_json::json!({"subject":"Examens","sender":"alice@example.org","recipients":["Bob@Example.org"],"thread_id":"thread"})).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        let store = PgSearchStore::new(database.clone());
        let filter = EvidenceFilter {
            from_ms: 1000,
            until_ms: 2000,
            sender: Some("alice@example.org".into()),
            recipient: Some("bob@example.org".into()),
            thread_id: Some("thread".into()),
            document_id: Some("a".repeat(32)),
            ontology_id: Some("legal".into()),
        };
        let page = store
            .evidence_page("tenant_a", &filter, None, 0, 20)
            .await?;
        anyhow::ensure!(
            page.len() == 1 &&
                page.first()
                    .is_some_and(|row| row.event_id == "event-1" &&
                        row.event_time_ms == 1000)
        );
        let search = store
            .evidence_page("tenant_a", &filter, Some("Examens"), 0, 20)
            .await?;
        anyhow::ensure!(search.len() == 2);
        let mut other = filter.clone();
        other.sender = Some("other@example.org".into());
        anyhow::ensure!(
            store
                .evidence_page("tenant_a", &other, None, 0, 20)
                .await?
                .is_empty()
        );
        anyhow::ensure!(
            store
                .evidence_page("tenant_b", &filter, Some("Examens"), 0, 20)
                .await?
                .is_empty()
        );
        let mut tx = database.tenant("tenant_b").await?;
        let rows = sqlx::query(
            "SELECT unit_id FROM pipeline_evidence WHERE tenant_id='tenant_a'",
        )
        .fetch_all(&mut *tx)
        .await?;
        anyhow::ensure!(rows.is_empty());
        tx.commit().await?;
        Ok(())
    }
}

//! PostgreSQL read adapter for ontology and pipeline control-plane records.

use anyhow::{Context, Result, bail};
use serde_json::Value;
use sqlx::Row;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::control_plane_store::{
    ControlPlaneStore, OntologyCatalogEntry, OntologyPublication,
    PipelineExecution, PipelineOntologyBinding,
};

const HARD_LIMIT: usize = 100;

pub struct PgControlPlaneStore {
    database: Database,
}

impl PgControlPlaneStore {
    /// Creates a control-plane store over the shared RLS-aware database pool.
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Converts PostgreSQL timestamps to Unix milliseconds.
    fn to_ms(value: sqlx::types::time::OffsetDateTime) -> i64 {
        value.unix_timestamp() * 1000 +
            i64::from(value.nanosecond()) / 1_000_000
    }

    /// Converts optional PostgreSQL timestamps to Unix milliseconds.
    fn optional_to_ms(
        value: Option<sqlx::types::time::OffsetDateTime>,
    ) -> Option<i64> {
        value.map(Self::to_ms)
    }

    /// Maps a joined ontology publication row into its domain projection.
    fn publication_from_row(
        row: &sqlx::postgres::PgRow,
    ) -> Result<OntologyPublication> {
        let published_at: sqlx::types::time::OffsetDateTime =
            row.try_get("published_at")?;
        let retired_at: Option<sqlx::types::time::OffsetDateTime> =
            row.try_get("retired_at")?;
        Ok(OntologyPublication {
            publication_id: row.try_get("publication_id")?,
            revision_id: row.try_get("revision_id")?,
            lifecycle: row.try_get("lifecycle")?,
            metadata: row.try_get::<Value, _>("metadata")?,
            base_version: row.try_get("base_version")?,
            base_hash: row.try_get("base_hash")?,
            effective_hash: row.try_get("effective_hash")?,
            author: row.try_get("author")?,
            message: row.try_get("message")?,
            published_at_ms: Self::to_ms(published_at),
            retired_at_ms: Self::optional_to_ms(retired_at),
        })
    }
}

#[async_trait::async_trait]
impl ControlPlaneStore for PgControlPlaneStore {
    /// Publishes one validated materialization in a tenant transaction.
    async fn publish_ontology(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        display_name: &str,
        publication_id: &str,
        revision_id: &str,
        metadata: &Value,
        create_only: bool,
    ) -> Result<OntologyPublication> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let materialized: bool = sqlx::query_scalar(
            r#"
            SELECT EXISTS(
                SELECT 1
                FROM ontology_materializations AS materialization
                JOIN ontology_revisions AS revision
                  ON revision.tenant_id = materialization.tenant_id
                 AND revision.revision_id = materialization.revision_id
                WHERE materialization.tenant_id = $1
                  AND materialization.revision_id = $2
            )
            "#,
        )
        .bind(tenant_id)
        .bind(revision_id)
        .fetch_one(&mut *tx)
        .await
        .context("Failed to verify ontology materialization")?;
        if !materialized {
            bail!("Ontology revision has no validated materialization");
        }
        let catalog = if create_only {
            sqlx::query(
                r#"
                INSERT INTO ontology_catalog (
                    tenant_id, ontology_id, display_name
                ) VALUES ($1, $2, $3)
                "#,
            )
            .bind(tenant_id)
            .bind(ontology_id)
            .bind(display_name)
            .execute(&mut *tx)
            .await
            .context("Failed to create ontology catalog entry")?
        } else {
            sqlx::query(
                r#"
                UPDATE ontology_catalog
                SET display_name = $3
                WHERE tenant_id = $1 AND ontology_id = $2
                "#,
            )
            .bind(tenant_id)
            .bind(ontology_id)
            .bind(display_name)
            .execute(&mut *tx)
            .await
            .context("Failed to update ontology catalog entry")?
        };
        if catalog.rows_affected() != 1 {
            bail!("Ontology catalog lifecycle changed concurrently");
        }
        sqlx::query(
            r#"
            UPDATE ontology_publications
            SET lifecycle = 'retired', retired_at = NOW()
            WHERE tenant_id = $1 AND ontology_id = $2
              AND lifecycle = 'production'
            "#,
        )
        .bind(tenant_id)
        .bind(ontology_id)
        .execute(&mut *tx)
        .await
        .context("Failed to retire prior ontology publication")?;
        sqlx::query(
            r#"
            INSERT INTO ontology_publications (
                tenant_id, ontology_id, publication_id, revision_id,
                lifecycle, metadata
            ) VALUES ($1, $2, $3, $4, 'production', $5)
            "#,
        )
        .bind(tenant_id)
        .bind(ontology_id)
        .bind(publication_id)
        .bind(revision_id)
        .bind(metadata)
        .execute(&mut *tx)
        .await
        .context("Failed to insert ontology publication")?;
        let row = sqlx::query(
            r#"
            SELECT publication.publication_id, publication.revision_id,
                   publication.lifecycle, publication.metadata,
                   publication.published_at, publication.retired_at,
                   materialization.base_version, materialization.base_hash,
                   materialization.effective_hash,
                   revision.author, revision.message
            FROM ontology_publications AS publication
            JOIN ontology_materializations AS materialization
              ON materialization.tenant_id = publication.tenant_id
             AND materialization.revision_id = publication.revision_id
            JOIN ontology_revisions AS revision
              ON revision.tenant_id = publication.tenant_id
             AND revision.revision_id = publication.revision_id
            WHERE publication.tenant_id = $1
              AND publication.ontology_id = $2
              AND publication.publication_id = $3
            "#,
        )
        .bind(tenant_id)
        .bind(ontology_id)
        .bind(publication_id)
        .fetch_one(&mut *tx)
        .await
        .context("Failed to load created ontology publication")?;
        let publication = Self::publication_from_row(&row)?;
        tx.commit()
            .await
            .context("Failed to commit ontology publication")?;
        Ok(publication)
    }

    /// Retires the sole production publication without deleting history.
    async fn retire_ontology(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        publication_id: &str,
        revision_id: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let retired = sqlx::query(
            r#"
            UPDATE ontology_publications
            SET lifecycle = 'retired', retired_at = NOW()
            WHERE tenant_id = $1 AND ontology_id = $2
              AND publication_id = $3 AND revision_id = $4
              AND lifecycle = 'production'
            "#,
        )
        .bind(tenant_id)
        .bind(ontology_id)
        .bind(publication_id)
        .bind(revision_id)
        .execute(&mut *tx)
        .await
        .context("Failed to retire ontology publication")?;
        if retired.rows_affected() != 1 {
            bail!("Ontology production publication changed concurrently");
        }
        tx.commit()
            .await
            .context("Failed to commit ontology retirement")
    }

    /// Checks for one tenant catalog entry through RLS.
    async fn ontology_exists(
        &self,
        tenant_id: &str,
        ontology_id: &str,
    ) -> Result<bool> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let exists = sqlx::query_scalar(
            r#"
            SELECT EXISTS(
                SELECT 1 FROM ontology_catalog
                WHERE tenant_id = $1 AND ontology_id = $2
            )
            "#,
        )
        .bind(tenant_id)
        .bind(ontology_id)
        .fetch_one(&mut *tx)
        .await
        .context("Failed to inspect ontology catalog")?;
        tx.commit().await?;
        Ok(exists)
    }

    /// Lists current catalog entries with their production publication.
    async fn list_ontologies(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<OntologyCatalogEntry>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT catalog.ontology_id, catalog.display_name,
                   publication.publication_id, publication.revision_id,
                   publication.lifecycle, publication.metadata,
                   publication.published_at, publication.retired_at,
                   materialization.base_version, materialization.base_hash,
                   materialization.effective_hash,
                   revision.author, revision.message
            FROM ontology_catalog AS catalog
            LEFT JOIN ontology_publications AS publication
              ON publication.tenant_id = catalog.tenant_id
             AND publication.ontology_id = catalog.ontology_id
             AND publication.lifecycle = 'production'
            LEFT JOIN ontology_materializations AS materialization
              ON materialization.tenant_id = publication.tenant_id
             AND materialization.revision_id = publication.revision_id
            LEFT JOIN ontology_revisions AS revision
              ON revision.tenant_id = publication.tenant_id
             AND revision.revision_id = publication.revision_id
            WHERE catalog.tenant_id = $1
            ORDER BY catalog.ontology_id
            LIMIT $2
            "#,
        )
        .bind(tenant_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list ontology catalog")?;
        tx.commit()
            .await
            .context("Failed to commit ontology catalog read")?;

        let mut entries = Vec::with_capacity(rows.len());
        for row in rows {
            let publication_id: Option<String> =
                row.try_get("publication_id")?;
            let production_publication = if publication_id.is_some() {
                Some(Self::publication_from_row(&row)?)
            } else {
                None
            };
            entries.push(OntologyCatalogEntry {
                ontology_id: row.try_get("ontology_id")?,
                display_name: row.try_get("display_name")?,
                production_publication,
            });
        }
        Ok(entries)
    }

    /// Lists immutable publication history newest first.
    async fn ontology_publication_history(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        limit: usize,
    ) -> Result<Vec<OntologyPublication>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT publication.publication_id, publication.revision_id,
                   publication.lifecycle, publication.metadata,
                   publication.published_at, publication.retired_at,
                   materialization.base_version, materialization.base_hash,
                   materialization.effective_hash,
                   revision.author, revision.message
            FROM ontology_publications AS publication
            JOIN ontology_materializations AS materialization
              ON materialization.tenant_id = publication.tenant_id
             AND materialization.revision_id = publication.revision_id
            JOIN ontology_revisions AS revision
              ON revision.tenant_id = publication.tenant_id
             AND revision.revision_id = publication.revision_id
            WHERE publication.tenant_id = $1
              AND publication.ontology_id = $2
            ORDER BY publication.published_at DESC, publication.publication_id DESC
            LIMIT $3
            "#,
        )
        .bind(tenant_id)
        .bind(ontology_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list ontology publication history")?;
        tx.commit()
            .await
            .context("Failed to commit ontology publication history read")?;

        rows.iter().map(Self::publication_from_row).collect()
    }

    /// Lists current ontology bindings through canonical relational records.
    async fn list_ontology_bindings(
        &self,
        tenant_id: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineOntologyBinding>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT pipeline_id, block_id, ontology_id, resource_ids,
                   resource_kinds, include_dependencies, metadata, updated_at
            FROM pipeline_ontology_bindings
            WHERE tenant_id = $1
              AND ($2::text IS NULL OR pipeline_id = $2)
            ORDER BY pipeline_id, block_id
            LIMIT $3
            "#,
        )
        .bind(tenant_id)
        .bind(pipeline_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list pipeline ontology bindings")?;
        tx.commit()
            .await
            .context("Failed to commit ontology binding read")?;

        let mut bindings = Vec::with_capacity(rows.len());
        for row in rows {
            let updated_at: sqlx::types::time::OffsetDateTime =
                row.try_get("updated_at")?;
            bindings.push(PipelineOntologyBinding {
                pipeline_id: row.try_get("pipeline_id")?,
                block_id: row.try_get("block_id")?,
                ontology_id: row.try_get("ontology_id")?,
                resource_ids: row.try_get("resource_ids")?,
                resource_kinds: row.try_get("resource_kinds")?,
                include_dependencies: row.try_get("include_dependencies")?,
                metadata: row.try_get("metadata")?,
                updated_at_ms: Self::to_ms(updated_at),
            });
        }
        Ok(bindings)
    }

    /// Lists durable pipeline execution records with optional pipeline scope.
    async fn list_pipeline_executions(
        &self,
        tenant_id: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineExecution>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT idempotency_key, command_id, correlation_id, pipeline,
                   step, status, attempt, lease_expires_at, result, error,
                   created_at, updated_at
            FROM pipeline_executions
            WHERE tenant_id = $1
              AND ($2::text IS NULL OR pipeline = $2)
            ORDER BY updated_at DESC, idempotency_key DESC
            LIMIT $3
            "#,
        )
        .bind(tenant_id)
        .bind(pipeline_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list pipeline executions")?;
        tx.commit()
            .await
            .context("Failed to commit pipeline execution read")?;

        let mut executions = Vec::with_capacity(rows.len());
        for row in rows {
            let lease_expires_at: sqlx::types::time::OffsetDateTime =
                row.try_get("lease_expires_at")?;
            let created_at: sqlx::types::time::OffsetDateTime =
                row.try_get("created_at")?;
            let updated_at: sqlx::types::time::OffsetDateTime =
                row.try_get("updated_at")?;
            executions.push(PipelineExecution {
                idempotency_key: row.try_get("idempotency_key")?,
                command_id: row.try_get("command_id")?,
                correlation_id: row.try_get("correlation_id")?,
                pipeline_id: row.try_get("pipeline")?,
                step: row.try_get("step")?,
                status: row.try_get("status")?,
                attempt: row.try_get("attempt")?,
                lease_expires_at_ms: Self::to_ms(lease_expires_at),
                result: row.try_get("result")?,
                error: row.try_get("error")?,
                created_at_ms: Self::to_ms(created_at),
                updated_at_ms: Self::to_ms(updated_at),
            });
        }
        Ok(executions)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_plane_limits_are_bounded() {
        assert_eq!(0usize.clamp(1, HARD_LIMIT), 1);
        assert_eq!(usize::MAX.clamp(1, HARD_LIMIT), HARD_LIMIT);
    }
}

//! PostgreSQL adapter for immutable pipeline revisions and current refs.

use anyhow::{Context, Result, bail};
use serde_json::Value;
use sqlx::FromRow;
use sqlx::types::time::OffsetDateTime;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::pipeline_store::{
    NewPipelineRevision, PipelineDefinition, PipelineStore,
};

#[derive(FromRow)]
struct PipelineRow {
    pipeline_id: String,
    name: String,
    owner_id: String,
    head_revision_id: String,
    published_revision_id: Option<String>,
    definition: Value,
    author_id: String,
    message: String,
    created_at: OffsetDateTime,
    updated_at: OffsetDateTime,
    deleted_at: Option<OffsetDateTime>,
}

/// RLS-scoped PostgreSQL implementation of pipeline authoring persistence.
pub struct PgPipelineStore {
    database: Database,
}

impl PgPipelineStore {
    /// Creates a store over the shared RLS-aware database pool.
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Converts PostgreSQL timestamps to Unix milliseconds.
    fn to_ms(value: OffsetDateTime) -> i64 {
        value.unix_timestamp_nanos() as i64 / 1_000_000
    }

    /// Maps a joined current-head row into the application projection.
    fn map(row: PipelineRow) -> PipelineDefinition {
        PipelineDefinition {
            pipeline_id: row.pipeline_id,
            name: row.name,
            owner_id: row.owner_id,
            head_revision_id: row.head_revision_id,
            published_revision_id: row.published_revision_id,
            definition: row.definition,
            author_id: row.author_id,
            message: row.message,
            created_at_ms: Self::to_ms(row.created_at),
            updated_at_ms: Self::to_ms(row.updated_at),
            deleted_at_ms: row.deleted_at.map(Self::to_ms),
        }
    }

    /// Loads a current pipeline inside an existing RLS transaction.
    async fn load(
        transaction: &mut sqlx::Transaction<'static, sqlx::Postgres>,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<Option<PipelineDefinition>> {
        let row = sqlx::query_as::<_, PipelineRow>(
            r#"
            SELECT definition.pipeline_id, definition.name,
                   definition.owner_id, definition.head_revision_id,
                   definition.published_revision_id, revision.definition,
                   revision.author_id, revision.message,
                   definition.created_at, definition.updated_at,
                   definition.deleted_at
            FROM pipeline_definitions AS definition
            JOIN pipeline_revisions AS revision
              ON revision.tenant_id = definition.tenant_id
             AND revision.pipeline_id = definition.pipeline_id
             AND revision.revision_id = definition.head_revision_id
            WHERE definition.tenant_id = $1
              AND definition.pipeline_id = $2
              AND definition.deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(pipeline_id)
        .fetch_optional(&mut **transaction)
        .await
        .context("Failed to load pipeline definition")?;
        Ok(row.map(Self::map))
    }

    /// Inserts one immutable revision inside an existing transaction.
    async fn insert_revision(
        transaction: &mut sqlx::Transaction<'static, sqlx::Postgres>,
        tenant_id: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO pipeline_revisions (
                tenant_id, pipeline_id, revision_id, parent_revision_id,
                definition, author_id, message
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            "#,
        )
        .bind(tenant_id)
        .bind(revision.pipeline_id)
        .bind(revision.revision_id)
        .bind(revision.parent_revision_id)
        .bind(revision.definition)
        .bind(revision.author_id)
        .bind(revision.message)
        .execute(&mut **transaction)
        .await
        .context("Failed to insert immutable pipeline revision")?;
        Ok(())
    }
}

#[async_trait::async_trait]
impl PipelineStore for PgPipelineStore {
    /// Creates a pipeline and its immutable root revision atomically.
    async fn create(
        &self,
        tenant_id: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<PipelineDefinition> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        sqlx::query(
            r#"
            INSERT INTO pipeline_definitions (
                tenant_id, pipeline_id, name, owner_id,
                head_revision_id
            ) VALUES ($1, $2, $3, $4, $5)
            "#,
        )
        .bind(tenant_id)
        .bind(revision.pipeline_id)
        .bind(revision.name)
        .bind(revision.owner_id)
        .bind(revision.revision_id)
        .execute(&mut *transaction)
        .await
        .context("Failed to create pipeline definition")?;
        Self::insert_revision(&mut transaction, tenant_id, revision).await?;
        let result =
            Self::load(&mut transaction, tenant_id, revision.pipeline_id)
                .await?
                .context("Created pipeline is unavailable")?;
        transaction.commit().await?;
        Ok(result)
    }

    /// Appends a revision only when the expected head still matches.
    async fn update(
        &self,
        tenant_id: &str,
        expected_head_revision_id: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<PipelineDefinition> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        Self::insert_revision(&mut transaction, tenant_id, revision).await?;
        let updated = sqlx::query(
            r#"
            UPDATE pipeline_definitions
            SET name = $4, head_revision_id = $5, updated_at = NOW()
            WHERE tenant_id = $1 AND pipeline_id = $2
              AND head_revision_id = $3 AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(revision.pipeline_id)
        .bind(expected_head_revision_id)
        .bind(revision.name)
        .bind(revision.revision_id)
        .execute(&mut *transaction)
        .await
        .context("Failed to advance pipeline head")?;
        if updated.rows_affected() != 1 {
            bail!("Pipeline head changed concurrently");
        }
        let result =
            Self::load(&mut transaction, tenant_id, revision.pipeline_id)
                .await?
                .context("Updated pipeline is unavailable")?;
        transaction.commit().await?;
        Ok(result)
    }

    /// Lists current, non-deleted pipeline definitions.
    async fn list(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<PipelineDefinition>> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query_as::<_, PipelineRow>(
            r#"
            SELECT definition.pipeline_id, definition.name,
                   definition.owner_id, definition.head_revision_id,
                   definition.published_revision_id, revision.definition,
                   revision.author_id, revision.message,
                   definition.created_at, definition.updated_at,
                   definition.deleted_at
            FROM pipeline_definitions AS definition
            JOIN pipeline_revisions AS revision
              ON revision.tenant_id = definition.tenant_id
             AND revision.pipeline_id = definition.pipeline_id
             AND revision.revision_id = definition.head_revision_id
            WHERE definition.tenant_id = $1
              AND definition.deleted_at IS NULL
            ORDER BY definition.updated_at DESC, definition.pipeline_id
            LIMIT $2
            "#,
        )
        .bind(tenant_id)
        .bind(i64::try_from(limit).context("Pipeline list limit overflow")?)
        .fetch_all(&mut *transaction)
        .await
        .context("Failed to list pipeline definitions")?;
        transaction.commit().await?;
        Ok(rows.into_iter().map(Self::map).collect())
    }

    /// Loads one current pipeline definition.
    async fn get(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<Option<PipelineDefinition>> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let result =
            Self::load(&mut transaction, tenant_id, pipeline_id).await?;
        transaction.commit().await?;
        Ok(result)
    }

    /// Marks the selected head revision as published.
    async fn publish(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<PipelineDefinition> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let updated = sqlx::query(
            r#"
            UPDATE pipeline_definitions
            SET published_revision_id = $3, updated_at = NOW()
            WHERE tenant_id = $1 AND pipeline_id = $2
              AND head_revision_id = $3 AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(pipeline_id)
        .bind(revision_id)
        .execute(&mut *transaction)
        .await
        .context("Failed to publish pipeline revision")?;
        if updated.rows_affected() != 1 {
            bail!("Only the current pipeline head can be published");
        }
        let result = Self::load(&mut transaction, tenant_id, pipeline_id)
            .await?
            .context("Published pipeline is unavailable")?;
        transaction.commit().await?;
        Ok(result)
    }

    /// Soft-deletes a pipeline without erasing revision history.
    async fn delete(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        expected_head_revision_id: &str,
    ) -> Result<()> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let deleted = sqlx::query(
            r#"
            UPDATE pipeline_definitions
            SET deleted_at = NOW(), published_revision_id = NULL,
                updated_at = NOW()
            WHERE tenant_id = $1 AND pipeline_id = $2
              AND head_revision_id = $3 AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(pipeline_id)
        .bind(expected_head_revision_id)
        .execute(&mut *transaction)
        .await
        .context("Failed to delete pipeline definition")?;
        if deleted.rows_affected() != 1 {
            bail!("Pipeline head changed concurrently");
        }
        transaction.commit().await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamp_conversion_preserves_milliseconds() {
        let timestamp =
            OffsetDateTime::from_unix_timestamp_nanos(1_234_000_000);
        assert!(
            matches!(timestamp, Ok(value) if PgPipelineStore::to_ms(value) == 1_234)
        );
    }
}

//! Persistence contracts for versioned tenant pipeline definitions.

use anyhow::Result;
use serde_json::Value;

/// Current pipeline definition with immutable revision provenance.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct PipelineDefinition {
    pub pipeline_id: String,
    pub name: String,
    pub owner_id: String,
    #[serde(skip)]
    pub head_revision_id: String,
    pub published_revision_id: Option<String>,
    pub definition: Value,
    pub author_id: String,
    pub message: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub deleted_at_ms: Option<i64>,
}

/// Input for one immutable pipeline definition revision.
pub struct NewPipelineRevision<'a> {
    pub pipeline_id: &'a str,
    #[cfg_attr(
        not(test),
        expect(
            dead_code,
            reason = "accepted only by the legacy migration adapter"
        )
    )]
    pub revision_id: &'a str,
    #[cfg_attr(
        not(test),
        expect(
            dead_code,
            reason = "accepted only by the legacy migration adapter"
        )
    )]
    pub parent_revision_id: Option<&'a str>,
    pub name: &'a str,
    pub owner_id: &'a str,
    pub definition: &'a Value,
    pub author_id: &'a str,
    pub message: &'a str,
}

/// PostgreSQL operations for optimistic pipeline authoring and publication.
#[async_trait::async_trait]
pub trait PipelineStore: Send + Sync {
    /// Creates a pipeline and its immutable root revision atomically.
    async fn create(
        &self,
        tenant_id: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<PipelineDefinition>;

    /// Appends a revision only when the expected head still matches.
    async fn update(
        &self,
        tenant_id: &str,
        expected_head_revision_id: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<PipelineDefinition>;

    /// Lists current, non-deleted pipeline definitions.
    async fn list(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<PipelineDefinition>>;

    /// Loads one current pipeline definition.
    async fn get(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<Option<PipelineDefinition>>;

    /// Marks the selected head revision as published.
    async fn publish(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<PipelineDefinition>;

    /// Soft-deletes a pipeline without erasing revision history.
    async fn delete(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        expected_head_revision_id: &str,
    ) -> Result<()>;
}

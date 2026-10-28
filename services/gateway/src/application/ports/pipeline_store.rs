//! Persistence contracts for versioned tenant pipeline definitions.

use anyhow::Result;
use serde_json::Value;

/// Current pipeline definition with immutable revision provenance.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct PipelineDefinition {
    #[serde(default)]
    pub authoring: galadril_registry::authoring::AuthoringState,
    #[serde(default)]
    pub previous_revision_id: Option<String>,
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

/// Input for one Registry-authored pipeline revision.
pub struct NewPipelineRevision<'a> {
    pub pipeline_id: &'a str,
    pub name: &'a str,
    pub owner_id: &'a str,
    pub definition: &'a Value,
    pub author_id: &'a str,
    pub message: &'a str,
}

/// Registry operations for optimistic pipeline authoring and publication.
#[async_trait::async_trait]
pub trait PipelineStore: Send + Sync {
    /// Applies an explicit authoring command through Registry's atomic
    /// boundary.
    async fn author(
        &self,
        _tenant_id: &str,
        _expected: Option<&str>,
        _revision: &NewPipelineRevision<'_>,
        _command: &galadril_registry::authoring::AuthoringCommand,
    ) -> Result<PipelineDefinition> {
        anyhow::bail!("Pipeline authoring is unavailable")
    }

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

    /// Reads an exact immutable revision; adapters must explicitly support
    /// pins.
    async fn get_revision(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
        _revision_id: &str,
    ) -> Result<Option<PipelineDefinition>> {
        anyhow::bail!("Pinned pipeline reads are unavailable")
    }

    /// Marks the selected head revision as published.
    async fn publish(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<PipelineDefinition>;

    /// Soft-deletes the active pipeline projection.
    async fn delete(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        expected_head_revision_id: &str,
    ) -> Result<()>;
}

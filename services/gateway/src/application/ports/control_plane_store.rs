//! Read contracts for authoritative ontology and pipeline runtime records.

use anyhow::Result;
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct OntologyCatalogEntry {
    pub ontology_id: String,
    pub display_name: String,
    pub production_publication: Option<OntologyPublication>,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct OntologyPublication {
    pub publication_id: String,
    pub revision_id: String,
    pub lifecycle: String,
    pub metadata: Value,
    pub base_version: String,
    pub base_hash: String,
    pub effective_hash: String,
    pub author: String,
    pub message: String,
    pub published_at_ms: i64,
    pub retired_at_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct PipelineOntologyBinding {
    pub pipeline_id: String,
    pub block_id: String,
    pub ontology_id: String,
    pub resource_ids: Value,
    pub resource_kinds: Value,
    pub include_dependencies: bool,
    pub metadata: Value,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct PipelineExecution {
    pub idempotency_key: String,
    pub command_id: String,
    pub correlation_id: String,
    pub pipeline_id: String,
    pub step: String,
    pub status: String,
    pub attempt: i32,
    pub lease_expires_at_ms: i64,
    pub result: Option<Value>,
    pub error: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

#[async_trait::async_trait]
pub trait ControlPlaneStore: Send + Sync {
    /// Publishes a previously validated materialization as production.
    #[expect(
        clippy::too_many_arguments,
        reason = "publication provenance is an explicit persistence boundary"
    )]
    async fn publish_ontology(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        display_name: &str,
        publication_id: &str,
        revision_id: &str,
        metadata: &Value,
        create_only: bool,
    ) -> Result<OntologyPublication>;

    /// Retires the current production publication without deleting history.
    async fn retire_ontology(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        publication_id: &str,
        revision_id: &str,
    ) -> Result<()>;

    /// Reports whether a tenant ontology catalog entry already exists.
    async fn ontology_exists(
        &self,
        tenant_id: &str,
        ontology_id: &str,
    ) -> Result<bool>;

    /// Lists current ontology catalog entries.
    async fn list_ontologies(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<OntologyCatalogEntry>>;

    /// Lists immutable publication history for one ontology.
    async fn ontology_publication_history(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        limit: usize,
    ) -> Result<Vec<OntologyPublication>>;

    /// Lists production ontology bindings for pipeline blocks.
    async fn list_ontology_bindings(
        &self,
        tenant_id: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineOntologyBinding>>;

    /// Lists durable pipeline execution records.
    async fn list_pipeline_executions(
        &self,
        tenant_id: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineExecution>>;
}

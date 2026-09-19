//! Immutable tenant state reconstructed from lakeFS ontology and pipeline
//! artifacts.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Complete reconstructible tenant projection stored at one lakeFS commit.
pub struct TenantState {
    #[serde(default)]
    /// Current pipeline records keyed by tenant-local pipeline ID.
    pub pipelines: BTreeMap<String, PipelineRecord>,
    #[serde(default)]
    /// Current ontology records keyed by tenant-local ontology ID.
    pub ontologies: BTreeMap<String, OntologyRecord>,
    #[serde(default)]
    /// Pipeline block bindings keyed by an internal composite identity.
    pub bindings: BTreeMap<String, OntologyBindingRecord>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Current pipeline projection embedded in an immutable tenant snapshot.
pub struct PipelineRecord {
    /// Tenant-local stable pipeline identity.
    pub pipeline_id: String,
    /// Human-readable pipeline name.
    pub name: String,
    /// Gateway-authorized owner identity.
    pub owner_id: String,
    /// Immutable lakeFS commit selected for production execution.
    pub published_revision_id: Option<String>,
    /// Validated pipeline configuration document.
    pub definition: Value,
    /// Gateway-authenticated author identity.
    pub author_id: String,
    /// Author-supplied revision message.
    pub message: String,
    /// Creation time in Unix milliseconds.
    pub created_at_ms: i64,
    /// Last mutation time in Unix milliseconds.
    pub updated_at_ms: i64,
    /// Soft-deletion time in Unix milliseconds, when deleted.
    pub deleted_at_ms: Option<i64>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Current ontology projection and optional production pointer.
pub struct OntologyRecord {
    /// Tenant-local stable ontology identity.
    pub ontology_id: String,
    /// Human-readable ontology name.
    pub display_name: String,
    /// Validated canonical ontology document.
    pub ontology: Value,
    /// Gateway-authenticated revision author.
    pub author: String,
    /// Author-supplied revision message.
    pub message: String,
    /// Creation time in Unix milliseconds.
    pub created_at_ms: i64,
    /// Sole active or most recently retired production publication.
    pub production_publication: Option<OntologyPublicationRecord>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Production ontology pointer and reproducibility metadata.
pub struct OntologyPublicationRecord {
    /// Gateway-generated publication identity.
    pub publication_id: String,
    /// Immutable lakeFS commit containing the ontology.
    pub revision_id: String,
    /// Production lifecycle value.
    pub lifecycle: String,
    /// Caller-owned descriptive metadata.
    pub metadata: Value,
    /// Base ontology version represented by the artifact.
    pub base_version: String,
    /// Canonical base ontology content hash.
    pub base_hash: String,
    /// Canonical effective ontology content hash.
    pub effective_hash: String,
    /// Author copied from the immutable ontology revision.
    pub author: String,
    /// Message copied from the immutable ontology revision.
    pub message: String,
    /// Publication time in Unix milliseconds.
    pub published_at_ms: i64,
    /// Retirement time in Unix milliseconds, when retired.
    pub retired_at_ms: Option<i64>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Validated mapping from one pipeline block to an ontology slice.
pub struct OntologyBindingRecord {
    /// Tenant-local pipeline identity.
    pub pipeline_id: String,
    /// Pipeline step receiving the ontology slice.
    pub block_id: String,
    /// Tenant-local published ontology identity.
    pub ontology_id: String,
    /// Explicit ontology resource identities to select.
    pub resource_ids: Vec<String>,
    /// Ontology resource kinds to select.
    pub resource_kinds: Vec<String>,
    /// Whether semantic dependency closure is included.
    pub include_dependencies: bool,
    /// Caller-owned descriptive metadata.
    pub metadata: Value,
    /// Binding update time in Unix milliseconds.
    pub updated_at_ms: i64,
}

impl TenantState {
    /// Constructs the internal collision-free pipeline/block lookup key.
    pub fn binding_key(pipeline_id: &str, block_id: &str) -> String {
        format!("{pipeline_id}\u{1f}{block_id}")
    }
}

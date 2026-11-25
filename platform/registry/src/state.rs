//! Immutable tenant state reconstructed from lakeFS ontology and pipeline
//! artifacts.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Default, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Complete reconstructible tenant projection stored at one lakeFS commit.
pub struct TenantState {
    #[serde(default)]
    /// Current pipeline records keyed by tenant-local pipeline ID.
    pub pipelines: BTreeMap<String, PipelineRecord>,
    #[serde(default)]
    /// Current ontology records keyed by tenant-local ontology ID.
    pub ontologies: BTreeMap<String, GolSourceRecord>,
    #[serde(default)]
    /// Pipeline block bindings keyed by an internal composite identity.
    pub bindings: BTreeMap<String, GolBindingRecord>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Current pipeline projection embedded in an immutable tenant snapshot.
pub struct PipelineRecord {
    /// Branches and review state are never accepted as execution
    /// configuration.
    #[serde(default)]
    pub authoring: crate::authoring::AuthoringState,
    #[serde(default)]
    /// Previous immutable snapshot containing this pipeline before its last
    /// edit.
    pub previous_revision_id: Option<String>,
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

/// Authored GOL package metadata stored at an immutable lakeFS revision.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolSourceRecord {
    /// Tenant-local logical ontology identity.
    pub ontology_id: String,
    /// Author-supplied catalog name.
    pub display_name: String,
    /// Paths whose exact UTF-8 contents are separate lakeFS `.gol` objects.
    pub paths: Vec<String>,
    /// SHA-256 of framed, path-normalized source bytes, independent of
    /// symbols.
    pub source_digest: String,
    /// Syntax-only declared module names, used for operational dependency
    /// guards.
    pub declared_modules: std::collections::BTreeSet<String>,
    /// Syntax-only imports; absence conservatively marks an unfinished draft.
    pub imports: Option<std::collections::BTreeSet<String>>,
    /// Changed source bytes; ordinary metadata loads never fetch these
    /// objects.
    #[serde(skip)]
    pub files: Vec<crate::gol::GolFile>,
    /// Authenticated source author.
    pub author: String,
    /// Creation time in Unix milliseconds.
    pub created_at_ms: i64,
    /// Last source mutation time in Unix milliseconds.
    pub updated_at_ms: i64,
    /// Optimistic source revision fencing a retryable cross-store deletion.
    pub deletion_base: Option<String>,
}

/// A pipeline-owned immutable source/generation selection, without raw IR.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolBindingRecord {
    /// Tenant-local pipeline identity.
    pub pipeline_id: String,
    /// Executable block receiving the contract.
    pub block_id: String,
    /// Logical ontology package identity.
    pub ontology_id: String,
    /// Exact atomically published generation.
    pub generation: String,
    /// Immutable lakeFS source revision behind this generation.
    pub source_revision: String,
    /// Selected logical symbols; empty requests capture all approved
    /// candidates.
    pub symbols: Vec<String>,
    /// Binding update time in Unix milliseconds.
    pub updated_at_ms: i64,
}

impl TenantState {
    /// Constructs the internal collision-free pipeline/block lookup key.
    pub fn binding_key(pipeline_id: &str, block_id: &str) -> String {
        format!("{pipeline_id}\u{1f}{block_id}")
    }
}

//! GOL source transport and immutable pipeline bindings owned by Registry.

use std::collections::BTreeMap;

use anyhow::Result;
pub use galadril_registry::gol::{GolDiagnostic, GolFile};
pub use galadril_registry::gol_service::{
    GolCompileApproval, GolDependencyPreview, GolPackageOrigin,
    GolPublication, GolSummary, GolValidation, GolWorkspace,
};
use serde_json::Value;

use crate::application::usecases::authorization::QueryContext;

/// Source input whose authenticated tenant and actor are supplied separately.
pub struct GolWrite<'a> {
    /// Qualified identity of the package's primary module.
    pub ontology_id: &'a str,
    /// Catalog name supplied by the author.
    pub display_name: &'a str,
    /// Complete relative UTF-8 source package, including unfinished drafts.
    pub files: &'a [GolFile],
    /// Required source snapshot for replacement; absence means creation.
    pub expected_revision: Option<&'a str>,
    /// Origin comes from the deployment inventory during explicit
    /// installation.
    pub origin: Option<GolPackageOrigin>,
}

/// Private transport from the deployed plugin inventory.
#[derive(serde::Deserialize)]
pub struct PluginGolPackage {
    pub id: String,
    pub name: String,
    pub version: String,
    pub plugin_id: String,
    pub plugin_version: String,
    pub files: Vec<GolFile>,
    #[serde(default)]
    pub requires_plugins: BTreeMap<String, String>,
}

impl PluginGolPackage {
    pub fn origin(&self) -> GolPackageOrigin {
        GolPackageOrigin {
            plugin_id: self.plugin_id.clone(),
            plugin_version: self.plugin_version.clone(),
            package_version: self.version.clone(),
        }
    }
}

/// The shared security adapter must admit metadata before it reaches clients.
#[async_trait::async_trait]
pub trait GolDisclosure: Send + Sync {
    /// Admits a new source without granting visibility of existing packages.
    async fn admit_creation(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        ontology: &str,
    ) -> Result<()>;
    /// Applies live GOL checks and IFC floors to a private catalog candidate.
    async fn admit_catalog(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        summary: &GolSummary,
    ) -> Result<bool>;
    /// Applies the conservative approved source-authoring floor and
    /// obligations.
    async fn admit_source(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        ontology: &str,
        source_revision: Option<&str>,
    ) -> Result<()>;
}

/// Pipeline-owned selection; symbol names require independent disclosure
/// checks.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct PipelineOntologyBinding {
    pub pipeline_id: String,
    pub block_id: String,
    pub ontology_id: String,
    pub generation: String,
    pub source_revision: String,
    pub symbols: Vec<String>,
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
    /// Source reads are called only after explicit authoring authorization.
    async fn gol_workspace(
        &self,
        _tenant: &str,
        _id: &str,
    ) -> Result<GolWorkspace> {
        anyhow::bail!("GOL workspace unavailable")
    }
    /// Draft syntax is validated exclusively by Registry's GOL compiler.
    async fn save_gol_ontology(
        &self,
        _tenant: &str,
        _user: &str,
        _input: GolWrite<'_>,
    ) -> Result<GolWorkspace> {
        anyhow::bail!("GOL authoring unavailable")
    }
    /// Diagnostics are bound to an exact source revision.
    async fn preview_gol_dependencies(
        &self,
        _tenant: &str,
        _id: &str,
        _revision: &str,
        _approved: &[String],
    ) -> Result<GolDependencyPreview> {
        anyhow::bail!("GOL dependency admission unavailable")
    }
    /// Detailed compilation requires a fully admitted immutable import plan.
    async fn validate_gol_ontology(
        &self,
        _tenant: &str,
        _id: &str,
        _revision: &str,
        _approval: &GolCompileApproval,
    ) -> Result<GolValidation> {
        anyhow::bail!("GOL validation unavailable")
    }
    /// Activation is performed only by Registry's atomic GOL publication path.
    async fn publish_gol_ontology(
        &self,
        _tenant: &str,
        _user: &str,
        _id: &str,
        _revision: &str,
        _expected: Option<&str>,
        _approval: &GolCompileApproval,
    ) -> Result<GolPublication> {
        anyhow::bail!("GOL publication unavailable")
    }
    /// Removes source membership and retires its active generation.
    async fn delete_gol_ontology(
        &self,
        _tenant: &str,
        _user: &str,
        _id: &str,
        _expected: &str,
    ) -> Result<String> {
        anyhow::bail!("GOL deletion unavailable")
    }
    /// Returns backend catalog candidates; no disclosure decision is implied.
    async fn list_gol_ontologies(
        &self,
        _tenant: &str,
        _limit: usize,
    ) -> Result<Vec<GolSummary>> {
        anyhow::bail!("GOL catalog unavailable")
    }
    /// Captures a published GOL generation in one pipeline authoring commit.
    async fn bind_ontology(
        &self,
        _tenant: &str,
        _user: &str,
        _pipeline: &str,
        _block: &str,
        _ontology: &str,
        _expected: &str,
    ) -> Result<String> {
        anyhow::bail!("GOL bindings unavailable")
    }
    /// Returns private immutable selections requiring source disclosure
    /// checks.
    async fn list_ontology_bindings(
        &self,
        _tenant: &str,
        _pipeline: Option<&str>,
        _limit: usize,
    ) -> Result<Vec<PipelineOntologyBinding>> {
        anyhow::bail!("GOL bindings unavailable")
    }
    /// Lists durable pipeline execution records.
    async fn list_pipeline_executions(
        &self,
        tenant_id: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineExecution>>;
}

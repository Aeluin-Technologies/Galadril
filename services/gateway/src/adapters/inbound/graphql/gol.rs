//! Typed GOL transport preserves source revisions and authorized projections.

use juniper::{FieldError, GraphQLEnum, GraphQLInputObject, GraphQLObject};

use crate::application::ports::control_plane_store as domain;
use crate::application::usecases::gol as runtime;

/// Capability hints never authorize source disclosure or artifact reads.
#[derive(Debug, Clone, Copy, GraphQLEnum)]
#[graphql(name = "GolOntologyAccessAction")]
pub enum AccessAction {
    Create,
    Edit,
    Publish,
    Delete,
}

/// Compatibility profiles remain explicit because constraints cannot be
/// dropped.
#[derive(Debug, Clone, Copy, GraphQLEnum)]
#[graphql(name = "GolSchemaProfile")]
pub enum SchemaProfile {
    Standard,
    OpenAiStrict,
    Outlines,
}

impl From<SchemaProfile> for galadril_ontology::export::SchemaProfile {
    fn from(value: SchemaProfile) -> Self {
        match value {
            SchemaProfile::Standard => Self::Standard,
            SchemaProfile::OpenAiStrict => Self::OpenAiStrict,
            SchemaProfile::Outlines => Self::Outlines,
        }
    }
}

#[derive(GraphQLInputObject)]
#[graphql(name = "GolSourceFileInput")]
pub struct SourceFileInput {
    pub path: String,
    pub text: String,
}

impl From<SourceFileInput> for domain::GolFile {
    fn from(value: SourceFileInput) -> Self {
        Self {
            path: value.path,
            text: value.text,
        }
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "GolSummary")]
pub struct Summary {
    pub ontology_id: String,
    pub display_name: String,
    pub source_revision: String,
    pub active_generation: Option<String>,
    pub active_source_revision: Option<String>,
    pub status: String,
    pub origin: Option<PackageOrigin>,
}

#[derive(GraphQLObject)]
#[graphql(name = "GolPackageOrigin")]
pub struct PackageOrigin {
    pub plugin_id: String,
    pub plugin_version: String,
    pub package_version: String,
}

impl From<domain::GolPackageOrigin> for PackageOrigin {
    fn from(value: domain::GolPackageOrigin) -> Self {
        Self {
            plugin_id: value.plugin_id,
            plugin_version: value.plugin_version,
            package_version: value.package_version,
        }
    }
}

/// Deployment availability is independent of every tenant publication.
#[derive(GraphQLObject)]
#[graphql(name = "GolOntologyPackage")]
pub struct OntologyPackage {
    pub ontology_id: String,
    pub display_name: String,
    pub plugin_id: String,
    pub plugin_version: String,
    pub package_version: String,
    pub required_plugins: Vec<PluginRequirement>,
}

#[derive(GraphQLObject)]
#[graphql(name = "GolPluginRequirement")]
pub struct PluginRequirement {
    pub plugin_id: String,
    pub plugin_version: String,
}

impl From<domain::PluginGolPackage> for OntologyPackage {
    fn from(value: domain::PluginGolPackage) -> Self {
        Self {
            ontology_id: value.id,
            display_name: value.name,
            plugin_id: value.plugin_id,
            plugin_version: value.plugin_version,
            package_version: value.version,
            required_plugins: value
                .requires_plugins
                .into_iter()
                .map(|(plugin_id, plugin_version)| PluginRequirement {
                    plugin_id,
                    plugin_version,
                })
                .collect(),
        }
    }
}

impl From<domain::GolSummary> for Summary {
    fn from(value: domain::GolSummary) -> Self {
        Self {
            ontology_id: value.ontology_id,
            display_name: value.display_name,
            source_revision: value.source_revision,
            active_generation: value.active_generation,
            active_source_revision: value.active_source_revision,
            status: value.status,
            origin: value.origin.map(Into::into),
        }
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "GolSourceFile")]
pub struct SourceFile {
    pub path: String,
    pub text: String,
}

impl From<domain::GolFile> for SourceFile {
    fn from(value: domain::GolFile) -> Self {
        Self {
            path: value.path,
            text: value.text,
        }
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "GolDiagnostic")]
pub struct Diagnostic {
    pub code: String,
    pub message: String,
    pub path: String,
    pub start: i32,
    pub end: i32,
    pub severity: String,
}

impl TryFrom<domain::GolDiagnostic> for Diagnostic {
    type Error = FieldError;

    fn try_from(value: domain::GolDiagnostic) -> Result<Self, Self::Error> {
        Ok(Self {
            code: value.code,
            message: value.message,
            path: value.path,
            start: i32::try_from(value.start).map_err(|_| unavailable())?,
            end: i32::try_from(value.end).map_err(|_| unavailable())?,
            severity: value.severity,
        })
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "GolDependency")]
pub struct Dependency {
    pub ontology_id: String,
    pub generation: String,
    pub source_revision: String,
}

impl From<galadril_registry::gol_service::GolDependency> for Dependency {
    fn from(value: galadril_registry::gol_service::GolDependency) -> Self {
        Self {
            ontology_id: value.ontology_id,
            generation: value.generation,
            source_revision: value.source_revision,
        }
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "GolWorkspace")]
pub struct Workspace {
    pub summary: Summary,
    pub files: Vec<SourceFile>,
    pub diagnostics: Vec<Diagnostic>,
    pub dependencies: Vec<Dependency>,
}

impl TryFrom<domain::GolWorkspace> for Workspace {
    type Error = FieldError;

    fn try_from(value: domain::GolWorkspace) -> Result<Self, Self::Error> {
        Ok(Self {
            summary: value.summary.into(),
            files: value.files.into_iter().map(Into::into).collect(),
            diagnostics: value
                .diagnostics
                .into_iter()
                .map(Diagnostic::try_from)
                .collect::<Result<_, _>>()?,
            dependencies: value
                .dependencies
                .into_iter()
                .map(Into::into)
                .collect(),
        })
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "GolValidation")]
pub struct Validation {
    pub ontology_id: String,
    pub source_revision: String,
    pub status: String,
    pub diagnostics: Vec<Diagnostic>,
    pub dependencies: Vec<Dependency>,
}

impl TryFrom<domain::GolValidation> for Validation {
    type Error = FieldError;

    fn try_from(value: domain::GolValidation) -> Result<Self, Self::Error> {
        Ok(Self {
            ontology_id: value.ontology_id,
            source_revision: value.source_revision,
            status: value.status,
            diagnostics: value
                .diagnostics
                .into_iter()
                .map(Diagnostic::try_from)
                .collect::<Result<_, _>>()?,
            dependencies: value
                .dependencies
                .into_iter()
                .map(Into::into)
                .collect(),
        })
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "GolPublication")]
pub struct Publication {
    pub ontology_id: String,
    pub source_revision: String,
    pub generation: String,
}

impl From<domain::GolPublication> for Publication {
    fn from(value: domain::GolPublication) -> Self {
        Self {
            ontology_id: value.ontology_id,
            source_revision: value.source_revision,
            generation: value.generation,
        }
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "GolDeletion")]
pub struct Deletion {
    pub revision: String,
}

#[derive(GraphQLObject)]
#[graphql(name = "GolProperty")]
pub struct Property {
    pub key: String,
    pub name: String,
    pub type_name: String,
}

#[derive(GraphQLObject)]
#[graphql(name = "GolSymbol")]
pub struct Symbol {
    pub key: String,
    pub name: String,
    pub kind: String,
    pub references: Vec<String>,
    pub properties: Vec<Property>,
}

#[derive(GraphQLObject)]
#[graphql(name = "GolSymbols")]
pub struct Symbols {
    pub generation: String,
    pub symbols: Vec<Symbol>,
}

impl From<runtime::GolSymbols> for Symbols {
    fn from(value: runtime::GolSymbols) -> Self {
        Self {
            generation: value.generation,
            symbols: value
                .symbols
                .into_iter()
                .map(|symbol| Symbol {
                    key: symbol.key,
                    name: symbol.name,
                    kind: symbol.kind,
                    references: symbol.references,
                    properties: symbol
                        .properties
                        .into_iter()
                        .map(|property| Property {
                            key: property.key,
                            name: property.name,
                            type_name: property.type_name,
                        })
                        .collect(),
                })
                .collect(),
        }
    }
}

/// Dependency failures share one envelope because their metadata can be
/// private.
pub fn unavailable() -> FieldError {
    tracing::warn!(event.name = "gol.request.refused");
    FieldError::new(
        "Ontology unavailable or request rejected",
        juniper::graphql_value!({"code": "GOL_UNAVAILABLE"}),
    )
}

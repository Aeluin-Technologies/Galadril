//! Authorized JSON contracts derived from canonical GOL types.
//!
//! Exporting a description does not authorize access to a value. Submit a
//! candidate through the write gate and the host's GOL validator before
//! ingestion. Schemas and candidates are request-scoped.
//!
//! # Examples
//!
//! The caller supplies the projection's actual instance and trusted context.
//! A successful export confers no additional read or execution permission.
//!
//! ```no_run
//! use galadril_ontology::archive::{ArchiveReader, RangeReader};
//! use galadril_ontology::authorization::{
//!     PermissionProvider, TrustedRuntime,
//! };
//! use galadril_ontology::export::{
//!     ExportError, ExportRequest, SchemaDocument,
//! };
//!
//! async fn describe(
//!     archive: &ArchiveReader<impl RangeReader>,
//!     request: &ExportRequest<'_>,
//!     runtime: &impl TrustedRuntime,
//!     spicedb: &impl PermissionProvider,
//! ) -> Result<SchemaDocument, ExportError> {
//!     archive.export_schema(request, runtime, spicedb).await
//! }
//! ```

mod build;
mod model;
mod scalar;
mod source;

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::future::Future;

use serde_json::Value;
#[cfg(feature = "parser")]
pub use source::ProjectExporter;
pub use source::Selection;

use crate::authorization::{
    Access, Operation, PermissionProvider, TrustedRuntime,
};
use crate::{ExternalTypeId, Name, SymbolRef, VersionedName};

/// The concrete compatibility contract to enforce during construction.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum SchemaProfile {
    /// JSON Schema Draft 2020-12, including optional object properties.
    Standard,
    /// Closed objects, required nullable fields, and an object root.
    OpenAiStrict,
    /// Outlines-core 0.2.14's tested local-reference and regex subset.
    Outlines,
}

/// The JSON representation of GOL integer values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IntegerEncoding {
    /// Numeric values whose complete Rust domain fits exact JSON consumers.
    /// Wider domains cause an explicit conversion error.
    SafeNumbers,
    /// Canonical decimal strings with exact, bounded range patterns.
    DecimalStrings,
}

/// A host's explicit precision contract for a registered external encoding.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MappingPrecision {
    /// The registered implementation preserves every value under this schema.
    Exact,
    /// The caller explicitly permits the registered adapter's rounding.
    Rounded,
}

/// Limits and representation choices applied to every export.
#[derive(Debug, Clone)]
pub struct ExportOptions {
    /// Concrete schema compatibility profile.
    pub profile: SchemaProfile,
    /// Representation of all integer fields.
    pub integers: IntegerEncoding,
    /// Explicit acceptance of JSON floating-point rounding.
    pub allow_float_rounding: bool,
    /// Maximum expansion depth through value types.
    pub max_depth: usize,
    /// Maximum number of projected properties and enum values.
    pub max_properties: usize,
    /// Maximum serialized schema or proposed object size.
    pub max_bytes: usize,
}

impl Default for ExportOptions {
    fn default() -> Self {
        Self {
            profile: SchemaProfile::Standard,
            integers: IntegerEncoding::SafeNumbers,
            allow_float_rounding: false,
            max_depth: 12,
            max_properties: 256,
            max_bytes: 128 * 1024,
        }
    }
}

impl ExportOptions {
    /// Rejects limits that would make schema processing unbounded.
    pub fn validate(&self) -> Result<(), ExportError> {
        if !(1..=32).contains(&self.max_depth) ||
            !(1..=1024).contains(&self.max_properties) ||
            !(256..=1024 * 1024).contains(&self.max_bytes)
        {
            return Err(ExportError::Limit);
        }
        Ok(())
    }
}

/// An explicit selection and the separately authorized named types it uses.
pub struct Projection {
    /// The selected root definition, field, or action signature.
    pub root: Selection,
    /// Exact named-type approvals and member selections. Ref targets use
    /// these approvals only to reveal typed IDs, without graph expansion.
    pub definitions: BTreeMap<SymbolRef, Selection>,
}

impl Projection {
    /// Starts a projection with no authority over other named definitions.
    #[must_use]
    pub fn new(root: Selection) -> Self {
        Self {
            root,
            definitions: BTreeMap::new(),
        }
    }
}

/// Which part of the selected declaration to describe.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SchemaPart {
    /// A selected entity, struct, enum, alias, or field.
    Value,
    /// The parameter object of a declarative action.
    ActionInput,
    /// The result of a declarative action.
    ActionOutput,
}

/// Immutable inputs to one schema projection.
pub struct ExportRequest<'a> {
    /// Explicit candidate selection and independent type approvals.
    pub projection: &'a Projection,
    /// The value or action signature component to describe.
    pub part: SchemaPart,
    /// Limits and concrete engine profile.
    pub options: &'a ExportOptions,
    /// Mappings installed at the trusted host boundary.
    pub mappings: &'a ExternalMappings,
}

/// An externally approved JSON adapter, independent of Rust crate discovery.
pub struct ExternalMapping {
    schema: Value,
    contract: VersionedName,
    precision: MappingPrecision,
}

impl ExternalMapping {
    /// Validates a bounded scalar mapping with no external references.
    ///
    /// The registering host attests to the semantic precision contract.
    /// GOL verifies the JSON shape and profile compatibility.
    pub fn new(
        schema: Value,
        contract: VersionedName,
        precision: MappingPrecision,
    ) -> Result<Self, ExportError> {
        scalar::validate_mapping(&schema)?;
        Ok(Self {
            schema,
            contract,
            precision,
        })
    }

    /// Returns the version of the JSON adapter approved by the host.
    #[must_use]
    pub fn contract(&self) -> &VersionedName {
        &self.contract
    }
}

/// Exact semantic type identities mapped to host-approved JSON adapters.
#[derive(Default)]
pub struct ExternalMappings(BTreeMap<ExternalTypeId, ExternalMapping>);

impl ExternalMappings {
    /// Registers an exact type version without replacing an existing mapping.
    pub fn register(
        &mut self,
        id: ExternalTypeId,
        mapping: ExternalMapping,
    ) -> Result<(), ExportError> {
        if self.0.contains_key(&id) {
            return Err(ExportError::Mapping);
        }
        self.0.insert(id, mapping);
        Ok(())
    }
}

/// Error categories that never contain hidden names or schema fragments.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum ExportError {
    /// The target, requested operation, or its metadata is unavailable.
    Unavailable,
    /// The value domain requires an explicitly accepted representation.
    Precision,
    /// The selected type has no supported JSON representation.
    Unsupported,
    /// A mandatory constraint is incompatible with the chosen profile.
    Profile,
    /// A configured schema or value resource bound was exceeded.
    Limit,
    /// An external JSON mapping is missing, invalid, or duplicated.
    Mapping,
    /// Proposed JSON violates the generated structural contract.
    InvalidJson,
    /// Mandatory write permissions were not satisfied.
    WriteDenied,
    /// The host's GOL business validation rejected the proposal.
    Business,
}

impl fmt::Display for ExportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "GOL schema operation failed: {self:?}")
    }
}

impl std::error::Error for ExportError {}

/// A schema compiled from precisely the metadata authorized for this request.
pub struct SchemaDocument {
    schema: Value,
    validator: jsonschema::Validator,
    options: ExportOptions,
    emitted: BTreeSet<Name>,
}

impl SchemaDocument {
    /// Borrows the public schema. Its lifetime confers no permission grant.
    #[must_use]
    pub fn schema(&self) -> &Value {
        &self.schema
    }

    /// Validates JSON shape and enabled format assertions locally.
    ///
    /// This checks the projection's structure. References, invariants, data
    /// freshness, and write permissions need the submission gate.
    pub fn validate_json(&self, value: &Value) -> Result<(), ExportError> {
        scalar::bounded_json(
            value,
            self.options.max_depth + 8,
            self.options.max_bytes,
        )?;
        if !self.validator.is_valid(value) {
            return Err(ExportError::InvalidJson);
        }
        Ok(())
    }
}

/// The trusted backend's result of full GOL validation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GolValidation {
    /// The host checked applicable invariants, references and preconditions.
    Accepted,
    /// A business obligation failed or could not be checked.
    Rejected,
}

/// A host implementation with access to authoritative instance state.
///
/// Implementations validate the proposed instance together with current
/// state, including omitted protected fields, references and applicable GOL
/// invariants. It must fail closed if a required check is unavailable.
/// This validates a proposal, not a transaction; the host must recheck any
/// revocable obligations when it commits the eventual mutation.
pub trait GolValidator {
    /// Checks the complete GOL obligations under the current request.
    fn validate(
        &self,
        target: &Access,
        proposal: &Value,
    ) -> impl Future<Output = GolValidation>;
}

fn access_for(
    access: &Access,
    target: crate::Reference,
    operation: Operation,
) -> Access {
    Access {
        route: crate::authorization::Route::new(target, operation),
        instance: access.instance.clone(),
        destination: access.destination.clone(),
        purpose: access.purpose.clone(),
    }
}

/// Schema description permissions are distinct from runtime value reads.
fn is_description(operation: Operation) -> bool {
    matches!(operation, Operation::Describe | Operation::DescribeTransfer)
}

fn record_error(error: ExportError) -> ExportError {
    tracing::debug!(event.name = "gol.export.unavailable", reason = ?error);
    error
}

#[cfg(feature = "parser")]
impl ProjectExporter<'_> {
    /// Generates a schema after current metadata authorization.
    pub async fn export_schema(
        &self,
        request: &ExportRequest<'_>,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<SchemaDocument, ExportError> {
        build::export(self, request, runtime, provider)
            .await
            .map_err(record_error)
    }

    /// Rebuilds the authorized schema, checks every supplied write field,
    /// and submits the candidate to the host's complete GOL validator.
    pub async fn validate_proposal(
        &self,
        request: &ExportRequest<'_>,
        proposal: &Value,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
        business: &impl GolValidator,
    ) -> Result<(), ExportError> {
        let document = self.export_schema(request, runtime, provider).await?;
        build::submit(
            self,
            &document,
            &request.projection.root,
            proposal,
            runtime,
            provider,
            business,
        )
        .await
        .map_err(record_error)
    }
}

impl<R: crate::archive::RangeReader> crate::archive::ArchiveReader<R> {
    /// Generates a bounded schema from the pinned archive without a parser.
    pub async fn export_schema(
        &self,
        request: &ExportRequest<'_>,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<SchemaDocument, ExportError> {
        build::export(self, request, runtime, provider)
            .await
            .map_err(record_error)
    }

    /// Checks JSON, current write permissions, and the host's GOL validation.
    pub async fn validate_proposal(
        &self,
        request: &ExportRequest<'_>,
        proposal: &Value,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
        business: &impl GolValidator,
    ) -> Result<(), ExportError> {
        let document = self.export_schema(request, runtime, provider).await?;
        build::submit(
            self,
            &document,
            &request.projection.root,
            proposal,
            runtime,
            provider,
            business,
        )
        .await
        .map_err(record_error)
    }
}

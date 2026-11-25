//! Source-only authoring inputs and diagnostics from the sole GOL compiler.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::num::{NonZeroU32, NonZeroU64};

use anyhow::{Result, ensure};
use galadril_ontology::authorization::{
    AuthorityRevision, CedarInput, CedarResource, Flow, Operation,
    RelationshipInput, RelationshipResource, Route, SecurityEnvironment, Sink,
};
use galadril_ontology::semantic::{Environment, Project, analyze_project};
use galadril_ontology::syntax::parse_source;
use galadril_ontology::{
    Declaration, ExternalTypeId, FieldRef, Name, OntologyId, Primitive,
    QualifiedName, Reference, SymbolRef,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const MAX_FILES: usize = 32;
const MAX_SOURCE_BYTES: usize = 1024 * 1024;
const MAX_PROJECT_BYTES: usize = 4 * MAX_SOURCE_BYTES;

/// Shares the injective IAM object-ID encoding across authorized services.
///
/// Canonical identifiers borrow their input. An equals sign is always escaped
/// so an already encoded spelling cannot impersonate another logical ID.
#[must_use]
pub fn authorization_object_id(value: &str) -> Cow<'_, str> {
    const fn supported(byte: u8) -> bool {
        byte.is_ascii_alphanumeric() ||
            matches!(byte, b'/' | b'_' | b'|' | b'-' | b'+')
    }
    const fn hex(nibble: u8) -> u8 {
        if nibble < 10 {
            b'0' + nibble
        } else {
            b'A' + nibble - 10
        }
    }
    if value.bytes().all(supported) {
        return Cow::Borrowed(value);
    }
    let mut encoded = String::with_capacity(value.len().saturating_mul(3));
    for byte in value.bytes() {
        if supported(byte) {
            encoded.push(char::from(byte));
        } else {
            encoded.push('=');
            encoded.push(char::from(hex(byte >> 4)));
            encoded.push(char::from(hex(byte & 15)));
        }
    }
    Cow::Owned(encoded)
}

/// Registers the deployment's bounded UTC temporal representation.
///
/// `gal.time.Instant@1.0.0` denotes four-digit calendar years, UTC, nanosecond
/// precision and no leap-second spelling. Registration loads no Rust crate;
/// an approved adapter remains a separate publication obligation.
pub fn registered_external_types()
-> Result<galadril_ontology::ExternalTypeRegistry> {
    use galadril_ontology::{
        AdapterContract, ExternalSchema, ExternalTypeDescriptor,
        TypeCapability, Version, VersionedName,
    };
    let id = ExternalTypeId::new(
        "gal.time.Instant".parse()?,
        Version::new(1, 0, 0),
    );
    let adapter = AdapterContract::new(
        VersionedName::new(
            "galadril.adapters.time.instant".parse()?,
            Version::new(1, 0, 0),
        ),
        "^1.0".parse()?,
    );
    let mut descriptor = ExternalTypeDescriptor::new(
        adapter,
        VersionedName::new(
            "gal.json.utc_instant".parse()?,
            Version::new(1, 0, 0),
        ),
        NonZeroU64::new(30).ok_or_else(|| {
            anyhow::anyhow!("invalid temporal encoding bound")
        })?,
        NonZeroU32::MIN,
    );
    let schema = serde_json::to_string(&temporal_schema())?;
    let mut external_schema = ExternalSchema::new(
        VersionedName::new("json.schema".parse()?, Version::new(2020, 12, 0)),
        format!("{:x}", Sha256::digest(schema.as_bytes())),
    );
    external_schema.document = Some(schema.into_boxed_str());
    descriptor.schema = Some(external_schema);
    descriptor.capabilities = BTreeSet::from([
        TypeCapability::Validate,
        TypeCapability::Equality,
        TypeCapability::Ordering,
    ]);
    let mut registry = galadril_ontology::ExternalTypeRegistry::default();
    registry.register(id, descriptor)?;
    Ok(registry)
}

/// Supplies the exact same UTC representation to authorized schema exports.
///
/// Profiles unable to express both the calendar format and canonical spelling
/// reject this mapping; constraints are never dropped for compatibility.
pub fn registered_json_mappings()
-> Result<galadril_ontology::export::ExternalMappings> {
    use galadril_ontology::export::{
        ExternalMapping, ExternalMappings, MappingPrecision,
    };
    use galadril_ontology::{Version, VersionedName};
    let mut mappings = ExternalMappings::default();
    mappings.register(
        ExternalTypeId::new(
            "gal.time.Instant".parse()?,
            Version::new(1, 0, 0),
        ),
        ExternalMapping::new(
            temporal_schema(),
            VersionedName::new(
                "galadril.adapters.time.instant_json".parse()?,
                Version::new(1, 0, 0),
            ),
            MappingPrecision::Exact,
        )?,
    )?;
    Ok(mappings)
}

fn temporal_schema() -> serde_json::Value {
    serde_json::json!({"type":"string","format":"date-time","pattern":r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{9}Z$"})
}

/// Explicit deployment approvals shared by compilation and runtime admission.
///
/// Source authors cannot supply these values. Loading no approved authority
/// configuration leaves publication unavailable rather than creating grants.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolAuthorityConfig {
    /// Version and fingerprint of this host routing configuration.
    pub revision: AuthorityRevision,
    /// Approved external relationship schema.
    pub relationship_schema: AuthorityRevision,
    /// Approved mapping from GOL resources to authorization object IDs.
    pub bridge: AuthorityRevision,
    /// Approved contextual schema and policy bundle.
    pub cedar_revision: AuthorityRevision,
    /// Authenticated principal entity type in the approved Cedar schema.
    pub cedar_principal: String,
    /// Approved native Cedar schema fragments, including the principal type.
    pub cedar_schema_fragments: Vec<String>,
    /// Approved native Cedar policies; no permit policy is synthesized.
    pub cedar_policies: String,
    /// Trusted context attributes required by the deployment profile.
    pub cedar_context: BTreeMap<Name, Primitive>,
    /// Explicitly approved resource attributes available to Cedar.
    pub cedar_fields: Vec<GolCedarFields>,
    /// Exact SpiceDB resource type approved by the schema authority.
    pub resource_type: String,
    /// Actual permissions supplied by the approved relationship schema.
    pub relationship_permissions: BTreeSet<Name>,
    /// Required operation bindings; absence never grants an operation.
    pub operations: BTreeMap<Operation, GolOperationConfig>,
    /// Explicit deployment classification floor.
    pub baseline: Flow,
    /// Conservative source and diagnostic metadata label, never user
    /// clearance.
    pub authoring_floor: Flow,
    /// Explicit floors for module metadata and all values derived from it.
    pub module_floors: BTreeMap<OntologyId, Flow>,
    /// Explicit ownership for companion modules bundled by a source package.
    pub module_owners: BTreeMap<OntologyId, OntologyId>,
    /// Exact symbol/property floors independent of module-wide
    /// classification.
    pub target_floors: Vec<GolTargetFloor>,
    /// Exact external adapter identities independently approved for
    /// deployment.
    pub approved_types: BTreeSet<ExternalTypeId>,
    /// Approved receiving destinations and purpose restrictions.
    pub sinks: BTreeMap<QualifiedName, Sink>,
}

/// External obligations required for one exposed GOL operation.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolOperationConfig {
    /// Permission declared by GOL and the approved SpiceDB schema.
    pub permission: Name,
    /// Exact action UID in the approved Cedar bundle.
    pub cedar_action: String,
}

/// Explicit projected attributes for one exact logical resource version.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolCedarFields {
    /// Exact declaration whose properties may be supplied to Cedar.
    pub target: SymbolRef,
    /// Independently approved property names.
    pub fields: BTreeSet<Name>,
}

/// A host-approved floor retaining property-level confidentiality.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolTargetFloor {
    /// Exact logical symbol or field version.
    pub target: Reference,
    /// Classification, compartments and downstream release restrictions.
    pub flow: Flow,
}

impl GolAuthorityConfig {
    /// Constructs compiler inputs from explicit authority configuration.
    ///
    /// Every exposed field receives its own route. Authorization always
    /// remains in the shared GOL gate, including for metadata descriptions.
    pub fn environment(
        &self,
        project: &Project,
    ) -> Result<SecurityEnvironment> {
        ensure!(
            !self.operations.is_empty(),
            "approved GOL operation profile is empty"
        );
        ensure!(
            !self.resource_type.is_empty() &&
                self.resource_type.len() <= 128 &&
                self.resource_type
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() ||
                        matches!(byte, b'_' | b'/')),
            "invalid approved GOL resource type"
        );
        let mut result = SecurityEnvironment::new(
            self.revision.clone(),
            self.baseline.clone(),
        );
        result.sinks.clone_from(&self.sinks);
        result.module_floors = self
            .module_floors
            .iter()
            .filter(|(id, _)| project.modules().contains_key(*id))
            .map(|(id, flow)| (id.clone(), flow.clone()))
            .collect();
        let mut targets = BTreeSet::new();
        for floor in &self.target_floors {
            ensure!(
                targets.insert(&floor.target),
                "duplicate approved target IFC floor"
            );
            let owner = match &floor.target {
                Reference::Symbol(owner) => owner,
                Reference::Field(field) => &field.owner,
                _ => anyhow::bail!("unsupported IFC floor target"),
            };
            if project.modules().contains_key(owner.key().ontology()) {
                result
                    .target_floors
                    .insert(floor.target.clone(), floor.flow.clone());
            }
        }
        result.approved_types.clone_from(&self.approved_types);
        let mut projected = BTreeSet::new();
        ensure!(
            self.cedar_fields
                .iter()
                .all(|item| projected.insert(&item.target)),
            "duplicate approved Cedar resource projection"
        );
        ensure!(
            !self.relationship_permissions.is_empty(),
            "approved relationship permission snapshot is empty"
        );
        let permissions = &self.relationship_permissions;
        let actions = self
            .operations
            .values()
            .map(|operation| {
                operation.cedar_action.parse().map_err(|_| {
                    anyhow::anyhow!("invalid approved Cedar action")
                })
            })
            .collect::<Result<BTreeSet<_>>>()?;
        let mut relationship = RelationshipInput::new(
            self.relationship_schema.clone(),
            self.bridge.clone(),
        );
        let mut cedar = CedarInput::new(
            self.cedar_revision.clone(),
            self.cedar_principal.parse().map_err(|_| {
                anyhow::anyhow!("invalid approved Cedar principal type")
            })?,
            self.cedar_policies.clone(),
        );
        cedar.schemas = self
            .cedar_schema_fragments
            .iter()
            .map(|fragment| fragment.clone().into_boxed_str())
            .collect();
        cedar.context.clone_from(&self.cedar_context);
        for symbol in project.symbols().keys() {
            let declaration = project
                .modules()
                .get(symbol.key().ontology())
                .and_then(|module| module.resolve_local(symbol))
                .ok_or_else(|| {
                    anyhow::anyhow!("GOL symbol is missing its declaration")
                })?;
            if matches!(
                declaration,
                Declaration::IfcLabel(_) |
                    Declaration::Invariant(_) |
                    Declaration::Derive(_)
            ) {
                continue;
            }
            relationship.resources.insert(
                symbol.clone(),
                RelationshipResource::new(
                    self.resource_type.clone(),
                    permissions.clone(),
                ),
            );
            let mut resource = CedarResource::new(
                cedar_resource_type(symbol)?.parse().map_err(|_| {
                    anyhow::anyhow!("invalid generated Cedar resource type")
                })?,
            );
            resource.actions.clone_from(&actions);
            resource.fields = self
                .cedar_fields
                .iter()
                .find(|item| &item.target == symbol)
                .map(|item| item.fields.clone())
                .unwrap_or_default();
            cedar.resources.insert(symbol.clone(), resource);
            let fields = match declaration {
                Declaration::Entity(value) => Some(&value.fields),
                Declaration::Relation(value) => Some(&value.fields),
                Declaration::Struct(value) => Some(&value.fields),
                _ => None,
            };
            let targets = std::iter::once(Reference::Symbol(symbol.clone()))
                .chain(
                    fields.into_iter().flat_map(|fields| fields.iter()).map(
                        |(name, _)| {
                            Reference::Field(FieldRef::new(
                                symbol.clone(),
                                name.clone(),
                            ))
                        },
                    ),
                );
            for target in targets {
                for (operation, rule) in &self.operations {
                    let allowed=match operation {
                        Operation::Describe|Operation::DescribeTransfer=>true,
                        Operation::Execute=>matches!(target,Reference::Symbol(_)) && matches!(declaration,Declaration::Action(_)),
                        Operation::Write=>matches!(declaration,Declaration::Entity(_)|Declaration::Relation(_)),
                        Operation::Read|Operation::Transfer=>match &target {
                            Reference::Field(field)=>fields.and_then(|fields|fields.get(field.name.as_str())).is_some_and(|field|matches!(&field.kind,galadril_ontology::FieldKind::Value(value) if scalar_value(value,project,0))),
                            Reference::Symbol(_)=>matches!(declaration,Declaration::Entity(_)|Declaration::Relation(_)|Declaration::View(_)|Declaration::Derive(_)),
                            _=>false,
                        },
                        _=>false,
                    };
                    if !allowed {
                        continue;
                    }
                    result.routes.insert(
                        Route::new(target.clone(), *operation),
                        BTreeSet::from([rule.permission.clone()]),
                    );
                }
            }
        }
        result.relationship = Some(relationship);
        result.cedar = Some(cedar);
        Ok(result)
    }
}

fn scalar_value(
    value: &galadril_ontology::TypeRef,
    project: &Project,
    depth: usize,
) -> bool {
    use galadril_ontology::TypeRef;
    if depth > 32 {
        return false;
    }
    match value {
        TypeRef::Primitive(_) | TypeRef::External(_) | TypeRef::Entity(_) => {
            true
        },
        TypeRef::Option(value) => scalar_value(value, project, depth + 1),
        TypeRef::List { element, .. } => {
            scalar_value(element, project, depth + 1)
        },
        TypeRef::Named(symbol) => project
            .modules()
            .get(symbol.key().ontology())
            .and_then(|module| module.resolve_local(symbol))
            .is_some_and(|declaration| match declaration {
                Declaration::Enum(_) => true,
                Declaration::TypeAlias(alias) => {
                    scalar_value(&alias.target, project, depth + 1)
                },
                Declaration::Attribute(attribute) => {
                    scalar_value(&attribute.value_type, project, depth + 1)
                },
                _ => false,
            }),
        _ => false,
    }
}

/// Derives a collision-resistant host projection type from logical identity.
///
/// This is an authorization adapter name, never the identity of a GOL symbol.
pub fn cedar_resource_type(symbol: &SymbolRef) -> Result<String> {
    Ok(format!(
        "Gol::R_{:x}",
        Sha256::digest(serde_json::to_vec(symbol)?)
    ))
}

/// One UTF-8 source, addressed relative to its Registry-owned source package.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolFile {
    /// Relative `.gol` source path; never used as a local filesystem path.
    pub path: String,
    /// Source contents, including unfinished drafts.
    pub text: String,
}

/// A compiler diagnostic with UTF-8 byte offsets in one package source.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolDiagnostic {
    /// Stable compiler error category.
    pub code: String,
    /// Human-readable compiler explanation, visible only to source authors.
    pub message: String,
    /// Relative package source path.
    pub path: String,
    /// Inclusive start byte offset.
    pub start: usize,
    /// Exclusive end byte offset.
    pub end: usize,
    /// Diagnostic severity; currently `error`.
    pub severity: String,
}

/// Checks transport budgets before a draft can reach storage or the parser.
///
/// Empty or invalid GOL text remains saveable. Paths and aggregate size are
/// checked independently because draft saves must not require compilation.
pub fn validate_files(files: &[GolFile]) -> Result<()> {
    ensure!(
        !files.is_empty() && files.len() <= MAX_FILES,
        "source package must contain 1 to 32 files"
    );
    let mut paths = BTreeSet::new();
    let mut bytes = 0usize;
    for file in files {
        ensure!(valid_path(&file.path), "invalid GOL source path");
        ensure!(
            paths.insert(file.path.as_str()),
            "duplicate GOL source path"
        );
        ensure!(
            file.text.len() <= MAX_SOURCE_BYTES,
            "GOL source exceeds byte budget"
        );
        bytes = bytes.checked_add(file.text.len()).ok_or_else(|| {
            anyhow::anyhow!("source package exceeds byte budget")
        })?;
        ensure!(
            bytes <= MAX_PROJECT_BYTES,
            "source package exceeds byte budget"
        );
    }
    Ok(())
}

/// Parses and analyzes in-memory sources without opening imports or files.
///
/// The host resolves approved dependencies into this explicit source set.
/// Unknown imports are compiler diagnostics rather than network requests.
pub fn analyze_files(
    files: &[GolFile],
    environment: &Environment<'_>,
) -> Result<Project, Vec<GolDiagnostic>> {
    if let Err(error) = validate_files(files) {
        return Err(vec![GolDiagnostic {
            code: "gol::source_package".into(),
            message: error.to_string(),
            path: String::new(),
            start: 0,
            end: 0,
            severity: "error".into(),
        }]);
    }
    let mut trees = Vec::with_capacity(files.len());
    let mut diagnostics = Vec::new();
    for file in files {
        match parse_source(&file.path, &file.text) {
            Ok(tree) => trees.push(tree),
            Err(error) => diagnostics.push(GolDiagnostic {
                code: error.code().as_str().into(),
                message: error.to_string(),
                path: file.path.clone(),
                start: error.span().start,
                end: error.span().end,
                severity: "error".into(),
            }),
        }
    }
    if !diagnostics.is_empty() {
        return Err(diagnostics);
    }
    analyze_project(&trees, environment).map_err(|errors| {
        errors
            .iter()
            .map(|error| GolDiagnostic {
                code: error.code().as_str().into(),
                message: error.to_string(),
                path: error.location().source.to_string(),
                start: error.location().range.0,
                end: error.location().range.1,
                severity: "error".into(),
            })
            .collect()
    })
}

fn valid_path(path: &str) -> bool {
    !path.is_empty() &&
        path.len() <= 256 &&
        path.ends_with(".gol") &&
        !path.contains('\\') &&
        path.split('/').all(|part| {
            !part.is_empty() &&
                part != "." &&
                part != ".." &&
                part.bytes().all(|byte| {
                    byte.is_ascii_alphanumeric() ||
                        matches!(byte, b'_' | b'-' | b'.')
                })
        })
}

/// Fingerprints framed source bytes without duplicating complete source
/// buffers.
pub(crate) fn source_digest(files: &[GolFile]) -> Result<String> {
    validate_files(files)?;
    let mut ordered = files.iter().collect::<Vec<_>>();
    ordered.sort_unstable_by(|left, right| left.path.cmp(&right.path));
    let mut hash = Sha256::new();
    for file in ordered {
        hash.update(u64::try_from(file.path.len())?.to_le_bytes());
        hash.update(file.path.as_bytes());
        hash.update(u64::try_from(file.text.len())?.to_le_bytes());
        hash.update(file.text.as_bytes());
    }
    Ok(format!("{:x}", hash.finalize()))
}

//! Immutable, version-pinned obligations indexed by logical access route.

use std::collections::{BTreeMap, BTreeSet};

use cedar_policy::{EntityTypeName, EntityUid, PolicySet, Schema};
use semver::Version;

use super::flow::{Flow, Sink};
use crate::{
    AccessBinding, ExternalTypeId, FieldRef, ModuleVersion, Name,
    QualifiedName, Reference, SymbolRef, VersionedName,
};

/// The requested kind of access. Transfers have their own route.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum Operation {
    /// Release an explicitly selected resource or property within the
    /// boundary.
    Read,
    /// Modify a resource or field under its write contract.
    Write,
    /// Invoke a declared action through its effect contract.
    Execute,
    /// Release a value through a registered transfer destination.
    Transfer,
    /// Describe selected schema metadata inside the deployment boundary.
    Describe,
    /// Transfer selected schema metadata to an approved external sink.
    DescribeTransfer,
}

/// The stable identity of one protected operation.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Route {
    /// The protected root or field.
    pub target: Reference,
    /// The exact operation requested.
    pub operation: Operation,
}

impl Route {
    /// Associates a target with an operation without making an access
    /// decision.
    #[must_use]
    pub fn new(target: impl Into<Reference>, operation: Operation) -> Self {
        Self {
            target: target.into(),
            operation,
        }
    }

    pub(super) fn owner(&self) -> &SymbolRef {
        owner(&self.target)
    }
}

pub(super) fn owner(target: &Reference) -> &SymbolRef {
    match target {
        Reference::Symbol(owner) => owner,
        Reference::Field(field) => &field.owner,
    }
}

/// Immutable authority revision and the SHA-256 digest of its approved
/// artifact or manifest.
///
/// The trusted host supplies the digest after verifying the artifact. The
/// compiler retains the digest so a matching version alone cannot authorize
/// a request against different authority inputs.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct AuthorityRevision {
    /// Logical name and version of the authority artifact.
    pub version: VersionedName,
    /// Digest supplied by the authority that approved the artifact.
    pub sha256: [u8; 32],
}

impl AuthorityRevision {
    /// Pins the approved artifact by name, version, and digest.
    #[must_use]
    pub fn new(version: VersionedName, sha256: [u8; 32]) -> Self {
        Self { version, sha256 }
    }
}

/// Static authority pins whose lifetime is independent of live relationships.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Versions {
    /// Exact versions of the modules used to compile GOL.
    pub ontologies: BTreeSet<ModuleVersion>,
    /// Host revision of routes, approved mappings and sink configuration.
    pub contract: AuthorityRevision,
    /// Approved SpiceDB schema revision.
    pub relationship_schema: AuthorityRevision,
    /// Stable instance-to-object mapping revision.
    pub bridge: AuthorityRevision,
    /// Approved Cedar schema and policy bundle revision.
    pub cedar: AuthorityRevision,
    /// IFC rule revision.
    pub ifc: Version,
}

/// Static data required to gate a single route.
pub struct Descriptor {
    pub(super) resource_type: EntityTypeName,
    pub(super) bindings: Vec<Permission>,
    pub(super) flow: Flow,
    pub(super) dependencies: BTreeSet<Route>,
    pub(super) fields: BTreeSet<FieldRef>,
    pub(super) context: BTreeSet<Name>,
    pub(super) control_flow: Flow,
    pub(super) compound: bool,
}

impl Descriptor {
    /// Returns the names of all mandatory local permissions, including
    /// inherited guards.
    pub fn local_permissions(&self) -> impl ExactSizeIterator<Item = &Name> {
        self.bindings.iter().map(|permission| &permission.name)
    }

    /// Returns the locally resolved, externally validated binding metadata.
    pub fn bindings(&self) -> impl ExactSizeIterator<Item = &AccessBinding> {
        self.bindings.iter().map(|permission| &permission.binding)
    }

    /// Returns the effective label, including data and control dependencies.
    #[must_use]
    pub fn flow(&self) -> &Flow {
        &self.flow
    }

    /// Returns the label of resource attributes supplied to policy evaluation.
    #[must_use]
    pub fn control_flow(&self) -> &Flow {
        &self.control_flow
    }

    /// Returns the prerequisite routes that need their own instance evidence.
    #[must_use]
    pub fn dependencies(&self) -> &BTreeSet<Route> {
        &self.dependencies
    }

    /// Returns all projected resource attributes available to Cedar.
    ///
    /// This conservative set includes attributes used by native policies.
    #[must_use]
    pub fn fields(&self) -> &BTreeSet<FieldRef> {
        &self.fields
    }

    /// Returns the context keys required by the compiled schema.
    #[must_use]
    pub fn context(&self) -> &BTreeSet<Name> {
        &self.context
    }

    /// Returns the linked Cedar entity type for the resource.
    #[must_use]
    pub fn cedar_resource_type(&self) -> &EntityTypeName {
        &self.resource_type
    }
}

pub(super) struct Permission {
    pub name: Name,
    pub binding: AccessBinding,
    pub action: EntityUid,
    pub groups: Vec<PolicySet>,
}

/// A static security contract constructed only after complete validation.
pub struct Contract {
    pub(super) authority: Authority,
    pub(super) descriptors: BTreeMap<Route, Descriptor>,
    pub(super) flows: BTreeMap<Reference, Flow>,
    pub(super) adapters: BTreeSet<ExternalTypeId>,
}

pub(super) struct Authority {
    pub versions: Versions,
    pub sinks: BTreeMap<QualifiedName, Sink>,
    pub schema: Schema,
    pub policies: PolicySet,
    pub principal: EntityTypeName,
    #[cfg(all(feature = "archive", feature = "parser"))]
    pub fragments: Vec<Box<str>>,
}

impl Contract {
    /// Returns the exact external descriptors approved for this compilation.
    #[must_use]
    pub fn adapters(&self) -> &BTreeSet<ExternalTypeId> {
        &self.adapters
    }

    /// Returns pins that trusted runtime evidence must match.
    #[must_use]
    pub fn versions(&self) -> &Versions {
        &self.authority.versions
    }

    /// Looks up an approved route and its mandatory obligations.
    #[must_use]
    pub fn descriptor(&self, route: &Route) -> Option<&Descriptor> {
        self.descriptors.get(route)
    }

    /// Iterates over validated routes for future security index construction.
    pub fn routes(
        &self,
    ) -> impl ExactSizeIterator<Item = (&Route, &Descriptor)> {
        self.descriptors.iter()
    }

    /// Looks up an effective label, including labels of unexposed outputs.
    #[must_use]
    pub fn flow(&self, target: &Reference) -> Option<&Flow> {
        self.flows.get(target)
    }
}

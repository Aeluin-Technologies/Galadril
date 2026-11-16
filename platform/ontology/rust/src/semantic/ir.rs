//! Source-independent semantic data alongside the canonical ontology modules.

use std::collections::{BTreeMap, BTreeSet};
use std::num::NonZeroU32;

pub use crate::checked::{Checked, Literal, Value};
use crate::syntax::PolicyKind;
use crate::{
    ExternalTypeId, ExternalTypeRegistry, Name, Named, Ontology, OntologyId,
    Primitive, QualifiedName, Reference, SymbolId, SymbolRef,
};

/// Inputs supplied by the host for one isolated compilation.
pub struct Environment<'a> {
    /// Registered descriptors available for explicitly imported external
    /// types.
    pub external_types: &'a ExternalTypeRegistry,
    /// Types allowed under the reserved contextual root in GOL policies.
    pub context: Named<Primitive>,
    /// The bound assigned to List types without a source-level length.
    pub max_list_length: NonZeroU32,
}

impl<'a> Environment<'a> {
    /// Creates an environment with no context properties and a list bound of
    /// 1024.
    #[must_use]
    pub fn new(external_types: &'a ExternalTypeRegistry) -> Self {
        Self {
            external_types,
            context: Named::new(),
            max_list_length: NonZeroU32::MIN.saturating_add(1023),
        }
    }
}

/// A successfully checked project, independent of source order and positions.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Project {
    pub(super) modules: BTreeMap<OntologyId, Ontology>,
    pub(super) symbols: BTreeMap<SymbolRef, SymbolId>,
    pub(super) expressions: BTreeMap<QualifiedName, Checked>,
    pub(super) security: BTreeMap<Reference, Security>,
    pub(super) labels: BTreeMap<SymbolRef, LabelTransfer>,
    pub(super) dependencies: BTreeSet<Dependency>,
    pub(super) external_imports:
        BTreeMap<OntologyId, BTreeSet<ExternalTypeId>>,
    pub(super) effects: BTreeMap<SymbolRef, BTreeSet<EffectPath>>,
}

impl Project {
    /// Returns the canonical modules including their version-pinned imports.
    #[must_use]
    pub fn modules(&self) -> &BTreeMap<OntologyId, Ontology> {
        &self.modules
    }

    /// Returns dense compilation-local IDs assigned in logical reference
    /// order.
    #[must_use]
    pub fn symbols(&self) -> &BTreeMap<SymbolRef, SymbolId> {
        &self.symbols
    }

    /// Looks up typed bodies by the definition identity in the canonical
    /// model.
    #[must_use]
    pub fn expressions(&self) -> &BTreeMap<QualifiedName, Checked> {
        &self.expressions
    }

    /// Returns static security requirements with explicit external status.
    #[must_use]
    pub fn security(&self) -> &BTreeMap<Reference, Security> {
        &self.security
    }

    /// Returns declared IFC transfer restrictions by resolved label.
    #[must_use]
    pub fn labels(&self) -> &BTreeMap<SymbolRef, LabelTransfer> {
        &self.labels
    }

    /// Returns typed declaration-level links for future index construction.
    #[must_use]
    pub fn dependencies(&self) -> &BTreeSet<Dependency> {
        &self.dependencies
    }

    /// Returns exact imported external descriptor identities by module.
    #[must_use]
    pub fn external_imports(
        &self,
    ) -> &BTreeMap<OntologyId, BTreeSet<ExternalTypeId>> {
        &self.external_imports
    }

    /// Returns effect paths that retain their action parameter roots.
    #[must_use]
    pub fn effects(&self) -> &BTreeMap<SymbolRef, BTreeSet<EffectPath>> {
        &self.effects
    }

    /// Reports pending external linkage without certifying publication.
    #[must_use]
    pub fn validation(&self) -> ValidationState {
        if !self.external_imports.is_empty() ||
            self.security.values().any(|security| {
                security.validation() == ValidationState::PendingExternal
            }) ||
            self.effects
                .values()
                .flatten()
                .any(|effect| matches!(effect, EffectPath::Emit(_)))
        {
            ValidationState::PendingExternal
        } else {
            ValidationState::Local
        }
    }
}

/// The kind of declaration dependency.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum DependencyKind {
    /// A named type used by a declaration.
    Type,
    /// An ontological reference or relation participant type.
    BusinessReference,
    /// An expression input or a declared effect target.
    Expression,
    /// A local security requirement or external authority reference.
    Security,
}

/// The target of a link between declarations or external contracts.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum DependencyTarget {
    /// A resolved declaration or field.
    Local(Reference),
    /// An exact registered external value domain.
    External(ExternalTypeId),
    /// A relationship or contextual policy permission.
    Permission(PermissionRef),
    /// A named host operation requiring external linkage.
    HostEffect(QualifiedName),
}

/// A declaration-level edge to a resolved symbol or a pending external input.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Dependency {
    /// The declaration owning the edge.
    pub owner: SymbolRef,
    /// The reason the target is required.
    pub kind: DependencyKind,
    /// The resolved target or explicitly external requirement.
    pub target: DependencyTarget,
}

/// An effect with its resolved action-relative instance path.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum EffectPath {
    /// A read of the checked parameter or member path.
    Read(Checked),
    /// A write of the checked member path.
    Write(Checked),
    /// A pending host effect contract.
    Emit(QualifiedName),
}

/// A local permission identifier owned by a target declaration.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct PermissionRef {
    /// The declaration protected by the permission.
    pub target: SymbolRef,
    /// The declared permission name.
    pub name: Name,
}

/// The locally checked spelling of external authorization obligations.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    /// SpiceDB permission to link later, when present in the declaration.
    pub spicedb: Option<Name>,
    /// Cedar action reference to link later, when present in the declaration.
    pub cedar: Option<Box<str>>,
}

/// Contextual policy with checked GOL predicates.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Policy {
    /// Permit or forbid as written.
    pub kind: PolicyKind,
    /// A local permission targeted within the resource, if present.
    pub permission: Option<Name>,
    /// The checked applicability predicate.
    pub when: Option<Checked>,
    /// The checked exception predicate.
    pub unless: Option<Checked>,
}

/// Local transfer declarations associated with an IFC label.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LabelTransfer {
    /// Explicit destination restriction, including an explicitly empty set.
    pub destinations: Option<BTreeSet<QualifiedName>>,
    /// Explicit purpose restriction, including an explicitly empty set.
    pub purposes: Option<BTreeSet<QualifiedName>>,
}

/// The validation state of a security entry after successful local analysis.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum ValidationState {
    /// All references within this local contract resolve in GOL.
    Local,
    /// This contract requires linking and verification by an external
    /// authority.
    PendingExternal,
}

/// Requirements on a resolved domain symbol or field.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Security {
    /// The declared external resource type.
    pub resource: Option<Box<str>>,
    /// Named local permissions with their pending external bindings.
    pub permissions: BTreeMap<Name, Binding>,
    /// Required permissions attached to this particular target.
    pub requires: BTreeSet<PermissionRef>,
    /// The resolved confidentiality label, when specified.
    pub label: Option<SymbolRef>,
    /// Checked contextual rules associated with this target.
    pub policies: BTreeSet<Policy>,
}

impl Security {
    /// Local validation never grants a permission.
    #[must_use]
    pub fn validation(&self) -> ValidationState {
        if self.permissions.is_empty() &&
            self.requires.is_empty() &&
            self.policies.is_empty() &&
            self.resource.is_none()
        {
            ValidationState::Local
        } else {
            ValidationState::PendingExternal
        }
    }
}

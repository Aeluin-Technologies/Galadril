//! Logical identities and version pins shared by the canonical model.

use std::borrow::Borrow;
use std::fmt::{self, Display, Formatter};
use std::str::FromStr;
use std::sync::Arc;

use semver::Version;

use crate::error::ModelError;

/// A case-sensitive canonical name segment in `[A-Za-z_][A-Za-z0-9_]*`.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[cfg_attr(feature = "archive", serde(try_from = "String"))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Name(Box<str>);

#[cfg(feature = "archive")]
impl TryFrom<String> for Name {
    type Error = ModelError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        value.parse()
    }
}

impl Name {
    /// Borrows the segment for allocation-free map lookups.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for Name {
    type Err = ModelError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        if valid_segment(value) {
            Ok(Self(value.into()))
        } else {
            Err(ModelError::InvalidName.record())
        }
    }
}

impl Borrow<str> for Name {
    fn borrow(&self) -> &str {
        self.as_str()
    }
}

impl Display for Name {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

fn valid_segment(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || byte == b'_') &&
        bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}

/// One or more canonical name segments separated by dots.
///
/// References can share the namespace spelling across many symbols.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[cfg_attr(feature = "archive", serde(try_from = "String"))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct QualifiedName(Arc<str>);

#[cfg(feature = "archive")]
impl TryFrom<String> for QualifiedName {
    type Error = ModelError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        value.parse()
    }
}

impl QualifiedName {
    /// Borrows the complete canonical spelling.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for QualifiedName {
    type Err = ModelError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        if value.split('.').all(valid_segment) {
            Ok(Self(value.into()))
        } else {
            Err(ModelError::InvalidQualifiedName.record())
        }
    }
}

impl Display for QualifiedName {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// A logical ontology name within the caller's module universe.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct OntologyId(QualifiedName);

impl From<QualifiedName> for OntologyId {
    fn from(name: QualifiedName) -> Self {
        Self(name)
    }
}

impl FromStr for OntologyId {
    type Err = ModelError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        value.parse().map(Self)
    }
}

impl Display for OntologyId {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        Display::fmt(&self.0, formatter)
    }
}

/// A module's declared semantic version.
pub type OntologyVersion = Version;

/// An exact pin for one GOL module.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ModuleVersion {
    ontology: OntologyId,
    version: OntologyVersion,
}

impl ModuleVersion {
    /// Pairs the logical module with the version used to interpret its
    /// symbols.
    #[must_use]
    pub fn new(ontology: OntologyId, version: OntologyVersion) -> Self {
        Self { ontology, version }
    }

    /// Returns the module identity shared by all its versions.
    #[must_use]
    pub fn ontology(&self) -> &OntologyId {
        &self.ontology
    }

    /// Returns the exact declared version.
    #[must_use]
    pub fn version(&self) -> &OntologyVersion {
        &self.version
    }

    /// Pins a reference before or after its declaration enters the module.
    #[must_use]
    pub fn reference(&self, name: Name) -> SymbolRef {
        SymbolRef::new(
            SymbolKey::new(self.ontology.clone(), name),
            self.version.clone(),
        )
    }
}

/// A stable logical symbol key using the module and its local declaration
/// name.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SymbolKey {
    ontology: OntologyId,
    name: Name,
}

impl SymbolKey {
    /// Forms a version-independent key in the shared declaration namespace.
    #[must_use]
    pub fn new(ontology: OntologyId, name: Name) -> Self {
        Self { ontology, name }
    }

    /// Returns the key's module scope.
    #[must_use]
    pub fn ontology(&self) -> &OntologyId {
        &self.ontology
    }

    /// Returns the declaration's name within that scope.
    #[must_use]
    pub fn name(&self) -> &Name {
        &self.name
    }
}

/// A symbol reference pinned to an exact module version.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SymbolRef {
    key: SymbolKey,
    version: OntologyVersion,
}

impl SymbolRef {
    /// Creates a reference that a caller can subsequently resolve.
    #[must_use]
    pub fn new(key: SymbolKey, version: OntologyVersion) -> Self {
        Self { key, version }
    }

    /// Returns the identity used to compare symbols across revisions.
    #[must_use]
    pub fn key(&self) -> &SymbolKey {
        &self.key
    }

    /// Returns the exact version to use at lookup time.
    #[must_use]
    pub fn version(&self) -> &OntologyVersion {
        &self.version
    }
}

/// A dense index assigned and interpreted by its owning compilation table.
///
/// Values are only meaningful within the same table. Canonical declarations
/// use [`SymbolKey`] and [`SymbolRef`] so rebuilding a table preserves
/// identity.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SymbolId(u32);

impl From<u32> for SymbolId {
    fn from(index: u32) -> Self {
        Self(index)
    }
}

impl From<SymbolId> for u32 {
    fn from(id: SymbolId) -> Self {
        id.0
    }
}

/// The kind expected by consumers of a top-level declaration.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub enum SymbolKind {
    /// A business entity.
    Entity,
    /// A business relation.
    Relation,
    /// A named value domain.
    Attribute,
    /// A named value type alias.
    TypeAlias,
    /// A structured value type.
    Struct,
    /// A closed enumeration.
    Enum,
    /// A predicate over admissible state.
    Invariant,
    /// A derived property.
    Derive,
    /// A declarative projection.
    View,
    /// An action contract.
    Action,
    /// An information-flow label.
    IfcLabel,
}

/// An exact version of a named external contract or descriptor.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub struct VersionedName {
    /// The logical name in the supplying registry.
    pub name: QualifiedName,
    /// The exact version expected by the consumer.
    pub version: Version,
}

impl VersionedName {
    /// Pins an external name to an exact version.
    #[must_use]
    pub fn new(name: QualifiedName, version: Version) -> Self {
        Self { name, version }
    }
}

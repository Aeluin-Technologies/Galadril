//! Versioned descriptors for types supplied by the embedding application.

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{self, Display, Formatter};
use std::num::{NonZeroU32, NonZeroU64};

use semver::{Version, VersionReq};

use crate::error::ModelError;
use crate::identity::{QualifiedName, VersionedName};

/// The exact semantic identity of a host-supplied value type.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ExternalTypeId(VersionedName);

impl ExternalTypeId {
    /// Pins a reference to the specified descriptor version.
    #[must_use]
    pub fn new(name: QualifiedName, version: Version) -> Self {
        Self(VersionedName::new(name, version))
    }

    /// Returns the name shared across descriptor versions.
    #[must_use]
    pub fn name(&self) -> &QualifiedName {
        &self.0.name
    }

    /// Returns the version used for exact descriptor lookup.
    #[must_use]
    pub fn version(&self) -> &Version {
        &self.0.version
    }
}

impl Display for ExternalTypeId {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}@{}", self.name(), self.version())
    }
}

/// An independently declared capability of an external type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub enum TypeCapability {
    /// Validates values against the registered contract.
    Validate,
    /// Projects fields with stable types.
    ProjectFields,
    /// Compares values for semantic equality.
    Equality,
    /// Provides a stable ordering.
    Ordering,
    /// Supports indexing under the descriptor's semantics.
    Indexing,
}

/// Schema metadata supplied by the external type registry.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct ExternalSchema {
    /// The schema language and its version.
    pub format: VersionedName,
    /// An opaque, registry-defined fingerprint of the schema.
    pub fingerprint: Box<str>,
    /// Optional schema text, such as JSON Schema or WIT.
    pub document: Option<Box<str>>,
}

impl ExternalSchema {
    /// Identifies a schema whose body can optionally be supplied by the host.
    #[must_use]
    pub fn new(
        format: VersionedName,
        fingerprint: impl Into<Box<str>>,
    ) -> Self {
        Self {
            format,
            fingerprint: fingerprint.into(),
            document: None,
        }
    }
}

/// The required adapter and its compatibility with a host interface.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct AdapterContract {
    /// The exact host-installed implementation or adapter contract version.
    pub implementation: VersionedName,
    /// Versions of the host adapter ABI accepted by the implementation.
    pub host_abi: VersionReq,
}

impl AdapterContract {
    /// Describes compatibility without loading or invoking the implementation.
    #[must_use]
    pub fn new(implementation: VersionedName, host_abi: VersionReq) -> Self {
        Self {
            implementation,
            host_abi,
        }
    }
}

/// The metadata of a registered external type, keyed by [`ExternalTypeId`].
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct ExternalTypeDescriptor {
    /// The required adapter contract.
    pub adapter: AdapterContract,
    /// The canonical value encoding and its version.
    pub encoding: VersionedName,
    /// The optional schema advertised by the host.
    pub schema: Option<ExternalSchema>,
    /// Only capabilities explicitly supplied by the registry owner.
    pub capabilities: BTreeSet<TypeCapability>,
    /// Maximum encoded bytes per value.
    pub max_encoded_bytes: NonZeroU64,
    /// Maximum nesting depth per value.
    pub max_depth: NonZeroU32,
}

impl ExternalTypeDescriptor {
    /// Records a bounded opaque type. Semantic capabilities are opt-in.
    #[must_use]
    pub fn new(
        adapter: AdapterContract,
        encoding: VersionedName,
        max_encoded_bytes: NonZeroU64,
        max_depth: NonZeroU32,
    ) -> Self {
        Self {
            adapter,
            encoding,
            max_encoded_bytes,
            max_depth,
            schema: None,
            capabilities: BTreeSet::new(),
        }
    }
}

/// In-memory descriptors explicitly registered by the host.
///
/// The embedding application supplies the trust boundary for registration.
/// The registry stores metadata and can be shared read-only after assembly.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ExternalTypeRegistry(
    BTreeMap<ExternalTypeId, ExternalTypeDescriptor>,
);

impl ExternalTypeRegistry {
    /// Inserts an exact type version, preserving any previously registered
    /// entry.
    ///
    /// # Errors
    ///
    /// Returns [`ModelError::DuplicateExternalType`] when the identity is
    /// taken.
    pub fn register(
        &mut self,
        id: ExternalTypeId,
        descriptor: ExternalTypeDescriptor,
    ) -> Result<(), ModelError> {
        match self.0.entry(id) {
            Entry::Vacant(entry) => {
                entry.insert(descriptor);
                Ok(())
            },
            Entry::Occupied(entry) => Err(ModelError::DuplicateExternalType {
                external_type: entry.key().clone(),
            }
            .record()),
        }
    }

    /// Looks up precisely the requested semantic version.
    #[must_use]
    pub fn get(&self, id: &ExternalTypeId) -> Option<&ExternalTypeDescriptor> {
        self.0.get(id)
    }

    /// Iterates in canonical identity order.
    pub fn iter(
        &self,
    ) -> impl ExactSizeIterator<
        Item = (&ExternalTypeId, &ExternalTypeDescriptor),
    > + '_ {
        self.0.iter()
    }
}

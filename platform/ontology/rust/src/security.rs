//! Declarative bindings to authorization and information-flow authorities.

use std::collections::BTreeSet;

use semver::Version;

use crate::identity::{Name, QualifiedName, SymbolRef, VersionedName};
use crate::named::Named;

/// A permission to check in a pinned external relationship schema.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct RelationshipPermission {
    /// The relationship schema declaring the resource and permission.
    pub schema: VersionedName,
    /// The versioned mapping from a domain resource to an authorization
    /// object.
    pub bridge: VersionedName,
    /// The backend resource type in its own naming convention.
    pub resource_type: Box<str>,
    /// The permission defined on that resource type.
    pub permission: Name,
}

impl RelationshipPermission {
    /// Declares a relationship lookup contract for a domain resource.
    #[must_use]
    pub fn new(
        schema: VersionedName,
        bridge: VersionedName,
        resource_type: impl Into<Box<str>>,
        permission: Name,
    ) -> Self {
        Self {
            schema,
            bridge,
            resource_type: resource_type.into(),
            permission,
        }
    }
}

/// A contextual policy action to check using a pinned external policy bundle.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct ContextPolicy {
    /// The bundle containing the policies and associated schema.
    pub bundle: VersionedName,
    /// The external action identifier in the policy engine's notation.
    pub action: Box<str>,
}

impl ContextPolicy {
    /// Records an action binding for later validation against the bundle.
    #[must_use]
    pub fn new(bundle: VersionedName, action: impl Into<Box<str>>) -> Self {
        Self {
            bundle,
            action: action.into(),
        }
    }
}

/// Conjunctive relationship and contextual requirements for an operation.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct AccessBinding {
    /// The required relationship permission check, such as a SpiceDB
    /// permission.
    pub relationship: RelationshipPermission,
    /// The required contextual check, such as a Cedar action.
    pub context: ContextPolicy,
}

impl AccessBinding {
    /// Requires both authorities for a protected operation.
    #[must_use]
    pub fn new(
        relationship: RelationshipPermission,
        context: ContextPolicy,
    ) -> Self {
        Self {
            relationship,
            context,
        }
    }
}

/// Confidentiality categories in the initial IFC profile.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum Classification {
    /// Public information in the IFC classification vocabulary.
    Public,
    /// Information restricted to internal use.
    Internal,
    /// Confidential information.
    Confidential,
    /// Information requiring restricted handling.
    Restricted,
}

/// A reusable description of the classification of a value.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct IfcLabel {
    /// The declared confidentiality class.
    pub classification: Classification,
    /// Any additional confidentiality compartments.
    pub compartments: BTreeSet<QualifiedName>,
    /// An optional integrity class from a separate vocabulary.
    pub integrity: Option<QualifiedName>,
}

impl IfcLabel {
    /// Declares a confidentiality label without additional compartment claims.
    #[must_use]
    pub fn new(classification: Classification) -> Self {
        Self {
            classification,
            compartments: BTreeSet::new(),
            integrity: None,
        }
    }
}

/// A declaration of the intended constraints on downstream release.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum TransferConstraint {
    /// Prohibits transfer.
    Denied,
    /// Requires both a listed sink and a listed purpose. An empty set admits
    /// none.
    Restricted {
        /// Admissible destination identifiers.
        destinations: BTreeSet<QualifiedName>,
        /// Admissible purpose identifiers.
        purposes: BTreeSet<QualifiedName>,
    },
}

/// An information-flow obligation retained alongside access bindings.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct FlowContract {
    /// A reference expected to resolve to an IFC label declaration.
    pub label: SymbolRef,
    /// Restrictions to consider when transferring the protected value.
    pub transfer: TransferConstraint,
}

impl FlowContract {
    /// Couples the classification reference with its transfer constraints.
    #[must_use]
    pub fn new(label: SymbolRef, transfer: TransferConstraint) -> Self {
        Self { label, transfer }
    }
}

/// Requirements attached to a business declaration or property by its owner.
///
/// An absent or empty contract carries no authorization decision. The host
/// must validate and enforce composition of type and field requirements.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct SecurityContract {
    /// The declared version of this security contract.
    pub version: Version,
    /// Protected operations and their external bindings.
    pub operations: Named<AccessBinding>,
    /// Information-flow obligations, independent of the access outcome.
    pub flow: Option<FlowContract>,
}

impl SecurityContract {
    /// Starts a versioned declaration of requirements.
    #[must_use]
    pub fn new(version: Version) -> Self {
        Self {
            version,
            operations: Named::new(),
            flow: None,
        }
    }
}

//! Named declarations and the shape of business entities and relations.

use crate::expression::{Action, Derivation, Invariant, View};
use crate::identity::{SymbolKind, SymbolRef};
use crate::named::Named;
use crate::security::{IfcLabel, SecurityContract};
use crate::value_type::{
    Attribute, Cardinality, EnumType, Field, StructType, TypeAlias,
};

/// A module's top-level declaration, stored under a unique local name.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Declaration {
    /// A business entity type.
    Entity(Entity),
    /// A business relation type.
    Relation(Relation),
    /// A named reusable attribute domain.
    Attribute(Attribute),
    /// An alternate name for an existing value type expression.
    TypeAlias(TypeAlias),
    /// A named structured value type.
    Struct(StructType),
    /// A named enumeration.
    Enum(EnumType),
    /// A state predicate.
    Invariant(Invariant),
    /// A derived property.
    Derive(Derivation),
    /// A declarative selection or projection.
    View(View),
    /// A contract for an operation implemented by the host.
    Action(Action),
    /// A reusable classification for information-flow contracts.
    IfcLabel(IfcLabel),
}

impl Declaration {
    /// Identifies the declaration category for semantic linking.
    #[must_use]
    pub fn kind(&self) -> SymbolKind {
        match self {
            Self::Entity(_) => SymbolKind::Entity,
            Self::Relation(_) => SymbolKind::Relation,
            Self::Attribute(_) => SymbolKind::Attribute,
            Self::TypeAlias(_) => SymbolKind::TypeAlias,
            Self::Struct(_) => SymbolKind::Struct,
            Self::Enum(_) => SymbolKind::Enum,
            Self::Invariant(_) => SymbolKind::Invariant,
            Self::Derive(_) => SymbolKind::Derive,
            Self::View(_) => SymbolKind::View,
            Self::Action(_) => SymbolKind::Action,
            Self::IfcLabel(_) => SymbolKind::IfcLabel,
        }
    }
}

/// A business entity schema. References to other entities may form cycles.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Entity {
    /// The entity's key declarations, fields, and owned attributes.
    pub fields: Named<Field>,
    /// Requirements declared at the entity type level.
    pub security: Option<Box<SecurityContract>>,
}

/// An association with independently named participant roles.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Relation {
    /// Role constraints for participants in an instance of the relation.
    pub roles: Named<Role>,
    /// The relation's own keys, fields, and owned attributes.
    pub fields: Named<Field>,
    /// Requirements declared at the relation type level.
    pub security: Option<Box<SecurityContract>>,
}

/// A participant constraint within a relation.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Role {
    /// The participant type, expected to refer to an entity declaration.
    pub entity: SymbolRef,
    /// Bounds on the number of participants in this role per relation.
    pub cardinality: Cardinality,
}

impl Role {
    /// Pairs a participant type with its multiplicity.
    #[must_use]
    pub fn new(entity: SymbolRef, cardinality: Cardinality) -> Self {
        Self {
            entity,
            cardinality,
        }
    }
}

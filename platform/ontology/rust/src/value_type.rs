//! Portable type expressions and user-defined value domains.

use std::collections::BTreeSet;
use std::num::NonZeroU32;

use crate::error::ModelError;
use crate::external_type::ExternalTypeId;
use crate::identity::{Name, SymbolRef};
use crate::named::Named;
use crate::security::SecurityContract;

/// Portable Rust value domains with no architecture-dependent widths.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub enum Primitive {
    /// The unit value, useful for actions without a data result.
    Unit,
    /// A Boolean.
    Bool,
    /// A Unicode scalar value.
    Char,
    /// UTF-8 text.
    String,
    /// An 8-bit signed integer.
    I8,
    /// A 16-bit signed integer.
    I16,
    /// A 32-bit signed integer.
    I32,
    /// A 64-bit signed integer.
    I64,
    /// A 128-bit signed integer.
    I128,
    /// An 8-bit unsigned integer.
    U8,
    /// A 16-bit unsigned integer.
    U16,
    /// A 32-bit unsigned integer.
    U32,
    /// A 64-bit unsigned integer.
    U64,
    /// A 128-bit unsigned integer.
    U128,
    /// Binary32 floating point. Comparison and indexing require a policy.
    F32,
    /// Binary64 floating point. Comparison and indexing require a policy.
    F64,
}

/// A type expression independent of its future encoding.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum TypeRef {
    /// A built-in portable value domain.
    Primitive(Primitive),
    /// A reference to a struct, enum, or type alias declaration.
    Named(SymbolRef),
    /// An explicitly optional value.
    Option(Box<Self>),
    /// An ordered collection with an explicit bound.
    List {
        /// The type shared by all elements.
        element: Box<Self>,
        /// The maximum number of elements in a value.
        max_length: NonZeroU32,
    },
    /// A GOL `Ref<T>` whose target declaration is expected to be an entity.
    Entity(SymbolRef),
    /// An exact identity in the host's external type registry.
    External(ExternalTypeId),
}

impl From<Primitive> for TypeRef {
    fn from(primitive: Primitive) -> Self {
        Self::Primitive(primitive)
    }
}

/// A field with optional, field-specific security requirements.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Field {
    /// The field's value and ownership semantics.
    pub kind: FieldKind,
    /// Requirements specific to access or transfer of this field.
    pub security: Option<Box<SecurityContract>>,
}

impl Field {
    /// Declares the field before attaching any additional requirements.
    #[must_use]
    pub fn new(kind: FieldKind) -> Self {
        Self {
            kind,
            security: None,
        }
    }
}

/// The source of a member's value domain.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum FieldKind {
    /// A field declared as part of the owner's business identity.
    Key(TypeRef),
    /// An ordinary field with its own type expression.
    Value(TypeRef),
    /// A field whose values use a named attribute domain.
    OwnedAttribute {
        /// A reference expected to resolve to an attribute declaration.
        attribute: SymbolRef,
        /// The bounds on the number of values owned.
        cardinality: Cardinality,
    },
}

/// A named type expression that can retain its own security requirements.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct TypeAlias {
    /// The aliased type expression.
    pub target: TypeRef,
    /// Obligations retained when semantic analysis expands the alias.
    pub security: Option<Box<SecurityContract>>,
}

impl TypeAlias {
    /// Declares the target before any alias-specific requirements are
    /// attached.
    #[must_use]
    pub fn new(target: TypeRef) -> Self {
        Self {
            target,
            security: None,
        }
    }
}

/// A reusable named value domain with no instance identity of its own.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Attribute {
    /// The type of a value in this domain.
    pub value_type: TypeRef,
    /// Additional obligations on use of this attribute.
    pub security: Option<Box<SecurityContract>>,
}

impl Attribute {
    /// Declares a domain over the given value type.
    #[must_use]
    pub fn new(value_type: TypeRef) -> Self {
        Self {
            value_type,
            security: None,
        }
    }
}

/// A product type whose members are identified by name.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StructType {
    /// Named, typed fields. A semantic pass checks legal field kinds.
    pub fields: Named<Field>,
    /// Additional obligations on use of the value type.
    pub security: Option<Box<SecurityContract>>,
}

/// A closed set of named, payload-free variants.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct EnumType {
    /// The admitted variant names, with no implicit numeric discriminants.
    pub variants: BTreeSet<Name>,
    /// Additional obligations on use of the value type.
    pub security: Option<Box<SecurityContract>>,
}

impl EnumType {
    /// Declares the complete set of variants.
    #[must_use]
    pub fn new(variants: BTreeSet<Name>) -> Self {
        Self {
            variants,
            security: None,
        }
    }
}

/// An inclusive multiplicity interval for relation roles and owned attributes.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cardinality {
    min: u32,
    max: Option<u32>,
}

impl Cardinality {
    /// Creates an interval with an optional upper bound.
    ///
    /// # Errors
    ///
    /// Returns [`ModelError::InvalidCardinality`] if a finite maximum is
    /// smaller than the minimum.
    pub fn new(min: u32, max: Option<u32>) -> Result<Self, ModelError> {
        if let Some(max) = max &&
            max < min
        {
            return Err(ModelError::InvalidCardinality { min, max }.record());
        }
        Ok(Self { min, max })
    }

    /// Returns the minimum admitted count.
    #[must_use]
    pub fn min(self) -> u32 {
        self.min
    }

    /// Returns the maximum admitted count, if bounded.
    #[must_use]
    pub fn max(self) -> Option<u32> {
        self.max
    }
}

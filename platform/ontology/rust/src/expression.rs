//! Declarative expression signatures and operation contracts.

use std::collections::BTreeSet;

use crate::identity::{Name, QualifiedName, SymbolRef};
use crate::named::Named;
use crate::security::SecurityContract;
use crate::value_type::TypeRef;

/// A named field of a version-pinned declaration.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub struct FieldRef {
    /// The declaration expected to own the field.
    pub owner: SymbolRef,
    /// The field name within its owner.
    pub name: Name,
}

impl FieldRef {
    /// Names a specific member without resolving the owner.
    #[must_use]
    pub fn new(owner: SymbolRef, name: Name) -> Self {
        Self { owner, name }
    }
}

/// A dependency or effect target, with explicit declaration or field scope.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub enum Reference {
    /// A dependency on an entire declaration.
    Symbol(SymbolRef),
    /// A dependency on one named field.
    Field(FieldRef),
}

impl From<SymbolRef> for Reference {
    fn from(symbol: SymbolRef) -> Self {
        Self::Symbol(symbol)
    }
}

impl From<FieldRef> for Reference {
    fn from(field: FieldRef) -> Self {
        Self::Field(field)
    }
}

/// An expression placeholder carrying semantic identity and declared inputs.
///
/// The producer must distinguish different definitions within its scope.
/// This record carries neither an expression body nor proof of dependency
/// completeness. Later phases supply the bounded expression representation.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub struct Expression {
    /// A stable name for this definition within the producing module.
    pub definition: QualifiedName,
    /// Declared data and control dependencies.
    pub dependencies: BTreeSet<Reference>,
}

impl Expression {
    /// Starts a definition with an empty dependency set.
    #[must_use]
    pub fn new(definition: QualifiedName) -> Self {
        Self {
            definition,
            dependencies: BTreeSet::new(),
        }
    }
}

/// An expression contract with a declared result type.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct TypedExpression {
    /// The value domain of the result.
    pub result: TypeRef,
    /// The definition and its declared dependencies.
    pub expression: Expression,
}

impl TypedExpression {
    /// Records the expected result type for later checking.
    #[must_use]
    pub fn new(result: TypeRef, expression: Expression) -> Self {
        Self { result, expression }
    }
}

/// An expression whose expected result is always Boolean.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub struct Predicate {
    /// The definition to be checked against the Boolean contract.
    pub expression: Expression,
}

impl Predicate {
    /// Declares the Boolean expectation without evaluating the definition.
    #[must_use]
    pub fn new(expression: Expression) -> Self {
        Self { expression }
    }
}

/// A named predicate scoped to admissible state of a declaration.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Invariant {
    /// The declaration whose state is constrained.
    pub subject: SymbolRef,
    /// The required condition on that state.
    pub condition: Predicate,
}

impl Invariant {
    /// Records an explicit subject so declaration order cannot affect scope.
    #[must_use]
    pub fn new(subject: SymbolRef, condition: Predicate) -> Self {
        Self { subject, condition }
    }
}

/// A declaration that computes a property's value from declared dependencies.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Derivation {
    /// The field that receives the result.
    pub target: FieldRef,
    /// The expected value and its declared dependencies.
    pub value: TypedExpression,
}

impl Derivation {
    /// Binds a result expression to a specific property.
    #[must_use]
    pub fn new(target: FieldRef, value: TypedExpression) -> Self {
        Self { target, value }
    }
}

/// A declarative projection with an optional selection predicate.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct View {
    /// Named result expressions.
    pub outputs: Named<TypedExpression>,
    /// A selection predicate that may contribute control-flow dependencies.
    pub filter: Option<Predicate>,
    /// Additional requirements on use of the projected result.
    pub security: Option<Box<SecurityContract>>,
}

/// A declaration of observable access or effects for a host action.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum Effect {
    /// Reads domain state.
    Read(Reference),
    /// Mutates domain state.
    Write(Reference),
    /// Emits to a named external effect contract.
    Emit(QualifiedName),
}

/// A retry declaration, including actions requiring host-side deduplication.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub enum Idempotence {
    /// No retry safety has been declared.
    #[default]
    Unspecified,
    /// Repetition preserves the declared result and effects.
    Idempotent,
    /// Repetition may cause additional state changes or effects.
    NonIdempotent,
    /// Safe replay requires the host to atomically enforce a deduplication
    /// key.
    Deduplicated {
        /// The parameter that supplies the key.
        key_parameter: Name,
    },
}

/// The signature and declared obligations of a host-implemented action.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Action {
    /// Logical parameters mapped by name.
    pub parameters: Named<TypeRef>,
    /// The expected result type.
    pub result: TypeRef,
    /// Conditions required before effects.
    pub requires: BTreeSet<Predicate>,
    /// Conditions required on successful completion.
    pub ensures: BTreeSet<Predicate>,
    /// The declared effects. This set conveys no execution order.
    pub effects: BTreeSet<Effect>,
    /// The action's declared retry semantics.
    pub retry: Idempotence,
    /// Additional requirements specific to this operation.
    pub security: Option<Box<SecurityContract>>,
}

impl Action {
    /// Starts with an explicit result and no claimed retry guarantee.
    #[must_use]
    pub fn new(result: TypeRef) -> Self {
        Self {
            parameters: Named::new(),
            result,
            requires: BTreeSet::new(),
            ensures: BTreeSet::new(),
            effects: BTreeSet::new(),
            retry: Idempotence::Unspecified,
            security: None,
        }
    }
}

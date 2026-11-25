//! Source-independent checked expressions shared by compilation and runtime.
//!
//! Decoding a node does not establish semantic validity. Runtime consumers
//! obtain bounded, owner-pinned contracts through the authenticated reader.

use crate::{FieldRef, Name, SymbolRef, TypeRef};

/// A checked expression with a resolved result type.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Checked {
    /// The type of the expression after type and capability checks.
    pub ty: TypeRef,
    /// The source-independent expression structure.
    pub value: Value,
}

/// A normalized, typed literal payload.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum Literal {
    /// A Boolean value.
    Bool(bool),
    /// An integer magnitude, typed by its containing node.
    Integer(u128),
    /// A negative integer's magnitude, including the signed minimum.
    NegativeInteger(u128),
    /// IEEE bits, with binary32 occupying the low 32 bits.
    Float(u64),
    /// Decoded text.
    String(Box<str>),
    /// A Unicode scalar.
    Char(char),
}

/// The checked structure of a declarative expression.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum Value {
    /// A typed literal.
    Literal(Literal),
    /// A parameter of the action containing the expression.
    Parameter(Name),
    /// The result of the action containing the expression.
    Result,
    /// A trusted context property supplied by the host.
    Context(Name),
    /// An explicitly resolved enum value.
    Variant(SymbolRef, Name),
    /// A field reached from a scope or a typed base.
    Field {
        /// The instance expression, when written explicitly.
        base: Option<Box<Checked>>,
        /// Resolved members in access order, including intermediate owners.
        path: Vec<FieldRef>,
    },
    /// A singleton participant reached in a relation's scope.
    Role {
        /// The defining relation.
        relation: SymbolRef,
        /// The declared role.
        role: Name,
    },
    /// Prefix operators in source order and their typed operand.
    Unary(Vec<UnaryOp>, Box<Checked>),
    /// A typed first operand and its same-precedence continuations.
    Binary(Box<Checked>, Vec<(BinaryOp, Checked)>),
}

/// A prefix operator.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum UnaryOp {
    /// Boolean negation.
    Not,
    /// Arithmetic negation.
    Negate,
}

/// An infix operator.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum BinaryOp {
    /// Boolean disjunction.
    Or,
    /// Boolean conjunction.
    And,
    /// Equality.
    Equal,
    /// Inequality.
    NotEqual,
    /// Strict less-than comparison.
    Less,
    /// Inclusive less-than comparison.
    LessEqual,
    /// Strict greater-than comparison.
    Greater,
    /// Inclusive greater-than comparison.
    GreaterEqual,
    /// Addition.
    Add,
    /// Subtraction.
    Subtract,
    /// Multiplication.
    Multiply,
    /// Division.
    Divide,
    /// Remainder.
    Remainder,
}

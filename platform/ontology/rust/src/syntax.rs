//! Parse UTF-8 GOL sources into an ordered, source-located AST.
//!
//! The caller supplies source names and contents. AST nodes borrow names and
//! literals until semantic analysis consumes them.
//!
//! ```
//! use galadril_ontology::syntax::{Declaration, parse_source};
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! let text = r#"ontology mail.core version "1.0.0" {
//!     entity Message { field delivery: Ref<other.Delivery>; }
//! }"#;
//! let tree = parse_source("mail.gol", text)?;
//! let declaration = tree
//!     .modules
//!     .first()
//!     .and_then(|module| module.value.declarations.first());
//! assert!(matches!(
//!     declaration.map(|node| &node.value),
//!     Some(Declaration::Entity(_))
//! ));
//! # Ok(())
//! # }
//! ```

mod ast;
mod diagnostic;
mod expression;
mod limits;
mod parser;
mod security;

pub use ast::{
    Access, AccessMember, Action, ActionClause, Annotation, BinaryOp,
    BinaryTerm, Binding, Cardinality, Declaration, Derivation, Effect,
    EnumVariant, Enumeration, Expression, Field, FieldGuard, FieldKind,
    Import, ImportKind, ImportSelection, Invariant, Label, LabelMember,
    Literal, Member, Name, Node, Ontology, OwnedAttribute, Path, Permission,
    Policy, PolicyKind, Projection, Record, Retry, Role, Source, Span,
    SyntaxTree, Text, Type, TypedName, UnaryOp, UpperBound, View,
};
pub use diagnostic::{ErrorCode, ParseError};
pub use parser::{parse_source, parse_sources};

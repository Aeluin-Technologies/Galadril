//! Source AST for a bounded, declarative GOL grammar.

use std::borrow::Cow;

use crate::security::Classification;
use crate::value_type::Primitive;

/// An identifier with its source range.
pub type Name<'a> = Node<&'a str>;

/// A decoded string literal with the range including its quotes.
pub type Text<'a> = Node<Cow<'a, str>>;

/// A half-open range of UTF-8 bytes within a syntax tree’s source.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Span {
    /// Offset of the first byte.
    pub start: usize,
    /// Offset immediately after the last byte.
    pub end: usize,
}

/// A syntax value with the complete range of its construct.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Node<T> {
    /// The source byte range.
    pub span: Span,
    /// The syntax represented by this node.
    pub value: T,
}

/// A caller-supplied source identity and its UTF-8 contents.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Source<'a> {
    /// A filename or other display identifier supplied by the caller.
    pub name: &'a str,
    /// The contents to parse.
    pub text: &'a str,
}

/// The ordered ontology declarations of one source file.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct SyntaxTree<'a> {
    /// The source that owns all node ranges.
    pub source: Source<'a>,
    /// The modules in source order.
    pub modules: Vec<Node<Ontology<'a>>>,
}

/// An unresolved sequence of qualified name segments.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Path<'a> {
    /// Segments in written order, with their individual ranges.
    pub segments: Vec<Name<'a>>,
}

/// The syntax of an ontology module.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Ontology<'a> {
    /// The qualified module name.
    pub name: Node<Path<'a>>,
    /// The declared version spelling after string escape decoding.
    pub version: Text<'a>,
    /// Declarations in written order.
    pub declarations: Vec<Node<Declaration<'a>>>,
}

/// A declaration at module scope.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Declaration<'a> {
    /// A module or external type import.
    Import(Import<'a>),
    /// A business entity.
    Entity(Record<'a>),
    /// An association with typed roles.
    Relation(Record<'a>),
    /// A named value domain.
    Attribute(TypedName<'a>),
    /// A name for a type expression.
    TypeAlias(TypedName<'a>),
    /// A product type.
    Struct(Record<'a>),
    /// An enumeration.
    Enum(Enumeration<'a>),
    /// A scoped predicate.
    Invariant(Invariant<'a>),
    /// A derived field declaration.
    Derive(Derivation<'a>),
    /// A projection with an optional filter.
    View(View<'a>),
    /// A signature with declarative clauses.
    Action(Action<'a>),
    /// External access requirements for an explicit target.
    Access(Access<'a>),
    /// A scoped contextual rule.
    Policy(Policy<'a>),
    /// An IFC label declaration.
    Label(Label<'a>),
}

/// How an import is marked in source.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum ImportKind {
    /// An ordinary module import.
    Module,
    /// An import introduced by the type modifier.
    ExternalType,
}

/// The symbols selected by an import.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum ImportSelection<'a> {
    /// The target scope without an explicit symbol selection.
    All,
    /// One or more explicitly named symbols.
    Symbols(Vec<Name<'a>>),
}

/// A dependency declaration awaiting resolution.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Import<'a> {
    /// The form of import.
    pub kind: ImportKind,
    /// The target namespace.
    pub module: Node<Path<'a>>,
    /// The requested symbols.
    pub selection: ImportSelection<'a>,
    /// An exact version spelling when supplied.
    pub version: Option<Text<'a>>,
    /// A local spelling for a module or a single selected symbol.
    pub alias: Option<Name<'a>>,
}

/// An entity, relation or struct declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Record<'a> {
    /// The local declaration name.
    pub name: Name<'a>,
    /// The members allowed by the enclosing declaration kind.
    pub members: Vec<Node<Member<'a>>>,
}

/// A member of a record declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Member<'a> {
    /// A typed field or identity key.
    Field(Field<'a>),
    /// An owned attribute domain.
    Owns(OwnedAttribute<'a>),
    /// A relation participant.
    Role(Role<'a>),
}

/// The written kind of a field.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum FieldKind {
    /// A regular field.
    Field,
    /// A field declared as a key.
    Key,
}

/// A typed field and its source annotations.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Field<'a> {
    /// The kind of field declared.
    pub kind: FieldKind,
    /// The field name.
    pub name: Name<'a>,
    /// The written type expression.
    pub ty: Node<Type<'a>>,
    /// Annotations in written order.
    pub annotations: Vec<Node<Annotation<'a>>>,
}

/// A field-level syntax annotation.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Annotation<'a> {
    /// A reference to an IFC label.
    Label(Node<Path<'a>>),
    /// A reference to a named access requirement.
    Requires(Node<Path<'a>>),
}

/// An owned attribute declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct OwnedAttribute<'a> {
    /// The attribute domain referenced by name.
    pub attribute: Node<Path<'a>>,
    /// The declared multiplicity.
    pub cardinality: Node<Cardinality<'a>>,
}

/// A named participant in a relation.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Role<'a> {
    /// The role name.
    pub name: Name<'a>,
    /// The unresolved participant type.
    pub target: Node<Path<'a>>,
    /// The declared multiplicity.
    pub cardinality: Node<Cardinality<'a>>,
}

/// The written bounds of a multiplicity interval.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Cardinality<'a> {
    /// The lower bound as decimal digits.
    pub min: Node<&'a str>,
    /// The upper bound, finite or unbounded.
    pub max: Node<UpperBound<'a>>,
}

/// The upper end of a multiplicity interval.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum UpperBound<'a> {
    /// A finite bound written in decimal.
    Count(&'a str),
    /// An unbounded maximum.
    Unbounded,
}

/// An enumeration with ordered variant declarations.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Enumeration<'a> {
    /// The enumeration name.
    pub name: Name<'a>,
    /// The declared variants.
    pub variants: Vec<Node<EnumVariant<'a>>>,
}

/// A payload-free variant in the current syntax.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct EnumVariant<'a> {
    /// The variant name.
    pub name: Name<'a>,
}

/// A named value with an explicit type expression.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct TypedName<'a> {
    /// The declared name.
    pub name: Name<'a>,
    /// The declared type.
    pub ty: Node<Type<'a>>,
}

/// A written type expression awaiting semantic analysis.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Type<'a> {
    /// A built-in GOL primitive.
    Primitive(Primitive),
    /// An unresolved local, qualified or imported type.
    Named(Path<'a>),
    /// An optional value.
    Option(Box<Node<Self>>),
    /// An ordered collection.
    List(Box<Node<Self>>),
    /// An ontological reference with a named target.
    Ref(Node<Path<'a>>),
}

/// A named predicate over the state of a target.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Invariant<'a> {
    /// The invariant name.
    pub name: Name<'a>,
    /// The explicit state scope.
    pub target: Node<Path<'a>>,
    /// The condition to be checked later.
    pub condition: Node<Expression<'a>>,
}

/// A named derived field declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Derivation<'a> {
    /// The derivation name.
    pub name: Name<'a>,
    /// The unresolved destination field path.
    pub target: Node<Path<'a>>,
    /// The declared result type.
    pub ty: Node<Type<'a>>,
    /// The written derivation expression.
    pub value: Node<Expression<'a>>,
}

/// A declarative projection from one source scope.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct View<'a> {
    /// The view name.
    pub name: Name<'a>,
    /// The source scope.
    pub source: Node<Path<'a>>,
    /// The projected expressions.
    pub projections: Vec<Node<Projection<'a>>>,
    /// The optional selection predicate.
    pub filter: Option<Node<Expression<'a>>>,
}

/// One output of a view declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Projection<'a> {
    /// The projected value.
    pub expression: Node<Expression<'a>>,
    /// The explicit output name, when given.
    pub alias: Option<Name<'a>>,
}

/// The syntax of a host-implemented action contract.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Action<'a> {
    /// The action name.
    pub name: Name<'a>,
    /// Parameters in source order.
    pub parameters: Vec<Node<TypedName<'a>>>,
    /// The explicitly declared result type.
    pub result: Option<Node<Type<'a>>>,
    /// Contract clauses in source order.
    pub clauses: Vec<Node<ActionClause<'a>>>,
}

/// A declared obligation or claim on an action.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum ActionClause<'a> {
    /// A precondition.
    Requires(Node<Expression<'a>>),
    /// A postcondition.
    Ensures(Node<Expression<'a>>),
    /// Declared effects.
    Effects(Vec<Node<Effect<'a>>>),
    /// A declared retry contract.
    Retry(Node<Retry<'a>>),
}

/// A named effect target and its declared operation.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Effect<'a> {
    /// Reads domain state.
    Read(Node<Path<'a>>),
    /// Changes domain state.
    Write(Node<Path<'a>>),
    /// Emits to an external effect contract.
    Emit(Node<Path<'a>>),
}

/// The written retry declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Retry<'a> {
    /// Claims idempotent behavior.
    Idempotent,
    /// Declares that effects may repeat.
    NonIdempotent,
    /// Declares a named deduplication parameter.
    Deduplicated(Name<'a>),
}

/// A set of external access requirements scoped to a target.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Access<'a> {
    /// The declaration this block applies to.
    pub target: Node<Path<'a>>,
    /// The written resources, permissions and guards.
    pub members: Vec<Node<AccessMember<'a>>>,
}

/// An entry in an access declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum AccessMember<'a> {
    /// The resource type spelling in the external system.
    Resource(Text<'a>),
    /// A named set of required checks.
    Permission(Permission<'a>),
    /// A field-specific requirement.
    Field(FieldGuard<'a>),
}

/// An explicit mapping from a field to a named permission.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct FieldGuard<'a> {
    /// The protected field.
    pub field: Node<Path<'a>>,
    /// The required permission.
    pub permission: Node<Path<'a>>,
}

/// A local permission declaration that references external checks.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Permission<'a> {
    /// The permission name.
    pub name: Name<'a>,
    /// Required checks as written.
    pub bindings: Vec<Node<Binding<'a>>>,
}

/// A requirement referencing an external authority.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Binding<'a> {
    /// A SpiceDB permission referenced by name.
    SpiceDb(Text<'a>),
    /// A Cedar action referenced by name.
    Cedar(Text<'a>),
}

/// The mode of a contextual GOL policy.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum PolicyKind {
    /// Declares a permit policy.
    Permit,
    /// Declares a forbid policy.
    Forbid,
}

/// A contextual rule with an explicit, unresolved target.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Policy<'a> {
    /// The rule mode and its keyword location.
    pub kind: Node<PolicyKind>,
    /// The rule’s scope.
    pub target: Node<Path<'a>>,
    /// The applicability condition.
    pub when: Option<Node<Expression<'a>>>,
    /// The applicability exception.
    pub unless: Option<Node<Expression<'a>>>,
}

/// A label definition for information-flow contracts.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Label<'a> {
    /// The declaration name.
    pub name: Name<'a>,
    /// The written label properties.
    pub members: Vec<Node<LabelMember<'a>>>,
}

/// A property of an IFC label declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum LabelMember<'a> {
    /// A confidentiality class.
    Classification(Node<Classification>),
    /// A separate integrity class.
    Integrity(Node<Path<'a>>),
    /// Confidentiality compartments.
    Compartments(Vec<Node<Path<'a>>>),
    /// Declared sink restrictions.
    Destinations(Vec<Node<Path<'a>>>),
    /// Declared purposes.
    Purposes(Vec<Node<Path<'a>>>),
}

/// A literal with original numeric spelling or decoded textual content.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Literal<'a> {
    /// A Boolean literal.
    Bool(bool),
    /// A decimal integer spelling.
    Integer(&'a str),
    /// A decimal floating-point spelling.
    Float(&'a str),
    /// UTF-8 text. Unescaped text borrows the source.
    String(Cow<'a, str>),
    /// One Unicode scalar value.
    Char(char),
}

pub use crate::checked::{BinaryOp, UnaryOp};

/// A continuation at one operator precedence level.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct BinaryTerm<'a> {
    /// The connecting operator.
    pub operator: Node<BinaryOp>,
    /// The right operand.
    pub operand: Node<Expression<'a>>,
}

/// A declarative expression. Same-precedence sequences are stored flat.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Expression<'a> {
    /// A written literal.
    Literal(Literal<'a>),
    /// An unresolved name, optionally qualified with double colons.
    Symbol(Path<'a>),
    /// An explicitly parenthesized expression.
    Grouped(Box<Node<Self>>),
    /// A left-to-right property access chain.
    Member {
        /// Base expression.
        base: Box<Node<Self>>,
        /// Properties in the order they are accessed.
        fields: Vec<Name<'a>>,
    },
    /// One or more prefix operations.
    Unary {
        /// Prefixes in source order. The rightmost prefix binds first.
        operators: Vec<Node<UnaryOp>>,
        /// The expression to which the prefixes apply.
        operand: Box<Node<Self>>,
    },
    /// An infix operation or a left-associative sequence.
    Binary {
        /// The first operand at this precedence level.
        first: Box<Node<Self>>,
        /// Follow-up operands, applied left to right.
        rest: Vec<BinaryTerm<'a>>,
    },
}

impl<T> Node<T> {
    pub(super) fn new(span: Span, value: T) -> Self {
        Self { span, value }
    }
}

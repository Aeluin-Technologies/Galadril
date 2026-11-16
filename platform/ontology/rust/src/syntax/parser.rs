//! Pest entry points and conversion of declaration pairs into the source AST.

use pest::Parser as _;
use pest::error::{ErrorVariant, InputLocation};
use pest::iterators::{Pair, Pairs};

use super::ast::{
    Action, ActionClause, Annotation, Cardinality, Declaration, Derivation,
    Effect, EnumVariant, Enumeration, Field, FieldKind, Import, ImportKind,
    ImportSelection, Invariant, Member, Name, Node, Ontology, OwnedAttribute,
    Path, Projection, Record, Retry, Role, Source, Span, SyntaxTree, Text,
    Type, TypedName, UpperBound, View,
};
use super::diagnostic::{ErrorCode, Failure, ParseError};
use super::{expression, limits, security};
use crate::value_type::Primitive;

#[derive(pest_derive::Parser)]
#[grammar = "src/syntax/gol.pest"]
struct GolParser;

pub(super) type Token<'a> = Pair<'a, Rule>;
pub(super) type Parsed<T> = Result<T, Failure>;

/// Parses a caller-supplied UTF-8 source and retains its written module order.
///
/// # Errors
///
/// Returns a source-located error on a grammar violation or a resource limit.
pub fn parse_source<'a>(
    name: &'a str,
    text: &'a str,
) -> Result<SyntaxTree<'a>, ParseError> {
    parse_file(Source { name, text })
        .map_err(|error| ParseError::new(name, error))
}

/// Parses independent sources on demand, preserving input order and individual
/// successes even when another source fails.
pub fn parse_sources<'a>(
    sources: impl IntoIterator<Item = Source<'a>>,
) -> impl Iterator<Item = Result<SyntaxTree<'a>, ParseError>> {
    sources
        .into_iter()
        .map(|source| parse_source(source.name, source.text))
}

fn parse_file(source: Source<'_>) -> Parsed<SyntaxTree<'_>> {
    limits::check(source.text)?;
    let mut pairs = GolParser::parse(Rule::file, source.text)
        .map_err(|error| syntax_failure(&error, source.text))?;
    let file = pairs.next().ok_or_else(|| {
        Failure::new(ErrorCode::Internal, Span { start: 0, end: 0 })
    })?;
    let modules = file
        .into_inner()
        .filter(|pair| pair.as_rule() != Rule::EOI)
        .map(|pair| node(pair, ontology))
        .collect::<Result<_, _>>()?;
    Ok(SyntaxTree { source, modules })
}

fn syntax_failure(error: &pest::error::Error<Rule>, text: &str) -> Failure {
    let span = match error.location {
        InputLocation::Pos(start) => {
            let width = text
                .get(start..)
                .and_then(|s| s.chars().next())
                .map_or(0, char::len_utf8);
            Span {
                start,
                end: start + width,
            }
        },
        InputLocation::Span((start, end)) => Span { start, end },
    };
    let expected = match &error.variant {
        ErrorVariant::ParsingError { positives, .. } => {
            positives.iter().copied().find_map(expected_construct)
        },
        ErrorVariant::CustomError { .. } => None,
    };
    Failure::syntax(span, expected)
}

fn expected_construct(rule: Rule) -> Option<&'static str> {
    Some(match rule {
        Rule::ontology_decl => "an ontology declaration",
        Rule::ident => "an identifier",
        Rule::path | Rule::symbol => "a name or qualified path",
        Rule::string => "a quoted string",
        Rule::digits => "decimal digits",
        Rule::option_type |
        Rule::list_type |
        Rule::ref_type |
        Rule::primitive => "a GOL type",
        Rule::label_annotation | Rule::requires_annotation => {
            "an annotation or a semicolon"
        },
        Rule::postfix | Rule::unary => "an expression",
        Rule::EOI => "the end of the source",
        _ => return None,
    })
}

pub(super) fn span(pair: &Token<'_>) -> Span {
    let range = pair.as_span();
    Span {
        start: range.start(),
        end: range.end(),
    }
}

pub(super) fn internal(pair: &Token<'_>) -> Failure {
    Failure::new(ErrorCode::Internal, span(pair))
}

pub(super) fn node<'a, T>(
    pair: Token<'a>,
    convert: impl FnOnce(Token<'a>) -> Parsed<T>,
) -> Parsed<Node<T>> {
    let span = span(&pair);
    Ok(Node::new(span, convert(pair)?))
}

pub(super) struct Children<'a> {
    span: Span,
    pairs: Pairs<'a, Rule>,
}

impl<'a> Children<'a> {
    pub(super) fn new(parent: Token<'a>) -> Self {
        Self {
            span: span(&parent),
            pairs: parent.into_inner(),
        }
    }

    pub(super) fn required(&mut self) -> Parsed<Token<'a>> {
        self.pairs
            .next()
            .ok_or_else(|| Failure::new(ErrorCode::Internal, self.span))
    }

    pub(super) fn optional(&mut self, rule: Rule) -> Option<Token<'a>> {
        self.pairs.peek().filter(|pair| pair.as_rule() == rule)?;
        self.pairs.next()
    }

    pub(super) fn rest(self) -> Pairs<'a, Rule> {
        self.pairs
    }

    pub(super) fn unexpected(&self) -> Failure {
        Failure::new(ErrorCode::Internal, self.span)
    }
}

pub(super) fn only_child(pair: Token<'_>) -> Parsed<Token<'_>> {
    Children::new(pair).required()
}

pub(super) fn name(pair: Token<'_>) -> Name<'_> {
    Node::new(span(&pair), pair.as_str())
}

pub(super) fn path_parts(pair: Token<'_>) -> Path<'_> {
    Path {
        segments: pair.into_inner().map(name).collect(),
    }
}

pub(super) fn path(pair: Token<'_>) -> Node<Path<'_>> {
    Node::new(span(&pair), path_parts(pair))
}

pub(super) fn text(pair: Token<'_>) -> Parsed<Text<'_>> {
    node(pair, |pair| expression::decode_text(&pair))
}

fn version(pair: Token<'_>) -> Parsed<Text<'_>> {
    text(only_child(pair)?)
}

fn ontology(pair: Token<'_>) -> Parsed<Ontology<'_>> {
    let mut children = Children::new(pair);
    Ok(Ontology {
        name: path(children.required()?),
        version: version(children.required()?)?,
        declarations: children
            .rest()
            .map(|pair| node(pair, declaration))
            .collect::<Result<_, _>>()?,
    })
}

fn declaration(pair: Token<'_>) -> Parsed<Declaration<'_>> {
    Ok(match pair.as_rule() {
        Rule::import_decl => Declaration::Import(import(pair)?),
        Rule::entity_decl => Declaration::Entity(record(pair)?),
        Rule::relation_decl => Declaration::Relation(record(pair)?),
        Rule::struct_decl => Declaration::Struct(record(pair)?),
        Rule::attribute_decl => Declaration::Attribute(typed_name(pair)?),
        Rule::alias_decl => Declaration::TypeAlias(typed_name(pair)?),
        Rule::enum_decl => Declaration::Enum(enumeration(pair)?),
        Rule::invariant_decl => Declaration::Invariant(invariant(pair)?),
        Rule::derive_decl => Declaration::Derive(derivation(pair)?),
        Rule::view_decl => Declaration::View(view(pair)?),
        Rule::action_decl => Declaration::Action(action(pair)?),
        Rule::access_decl => Declaration::Access(security::access(pair)?),
        Rule::policy_decl => Declaration::Policy(security::policy(pair)?),
        Rule::label_decl => Declaration::Label(security::label(pair)?),
        _ => return Err(internal(&pair)),
    })
}

fn import(pair: Token<'_>) -> Parsed<Import<'_>> {
    let mut children = Children::new(pair);
    let kind = if children.optional(Rule::external_marker).is_some() {
        ImportKind::ExternalType
    } else {
        ImportKind::Module
    };
    let module = path(children.required()?);
    let selection = children.optional(Rule::import_selection).map_or(
        ImportSelection::All,
        |selection| {
            ImportSelection::Symbols(
                selection.into_inner().map(name).collect(),
            )
        },
    );
    let version = children
        .optional(Rule::version_clause)
        .map(version)
        .transpose()?;
    Ok(Import {
        kind,
        module,
        selection,
        version,
        alias: children
            .optional(Rule::import_alias)
            .map(|pair| only_child(pair).map(name))
            .transpose()?,
    })
}

fn record(pair: Token<'_>) -> Parsed<Record<'_>> {
    let mut children = Children::new(pair);
    Ok(Record {
        name: name(children.required()?),
        members: children
            .rest()
            .map(|pair| node(pair, member))
            .collect::<Result<_, _>>()?,
    })
}

fn member(pair: Token<'_>) -> Parsed<Member<'_>> {
    match pair.as_rule() {
        Rule::field_decl => field(pair, FieldKind::Field).map(Member::Field),
        Rule::key_decl => field(pair, FieldKind::Key).map(Member::Field),
        Rule::owns_decl => {
            let mut children = Children::new(pair);
            Ok(Member::Owns(OwnedAttribute {
                attribute: path(children.required()?),
                cardinality: node(children.required()?, cardinality)?,
            }))
        },
        Rule::role_decl => {
            let mut children = Children::new(pair);
            Ok(Member::Role(Role {
                name: name(children.required()?),
                target: path(children.required()?),
                cardinality: node(children.required()?, cardinality)?,
            }))
        },
        _ => Err(internal(&pair)),
    }
}

fn field(pair: Token<'_>, kind: FieldKind) -> Parsed<Field<'_>> {
    let mut children = Children::new(pair);
    Ok(Field {
        kind,
        name: name(children.required()?),
        ty: node(children.required()?, value_type)?,
        annotations: children
            .rest()
            .map(|pair| node(pair, annotation))
            .collect::<Result<_, _>>()?,
    })
}

fn annotation(pair: Token<'_>) -> Parsed<Annotation<'_>> {
    match pair.as_rule() {
        Rule::label_annotation => {
            Ok(Annotation::Label(path(only_child(pair)?)))
        },
        Rule::requires_annotation => {
            Ok(Annotation::Requires(path(only_child(pair)?)))
        },
        _ => Err(internal(&pair)),
    }
}

fn cardinality(pair: Token<'_>) -> Parsed<Cardinality<'_>> {
    let mut children = Children::new(pair);
    let min = name(children.required()?);
    let max = node(children.required()?, |pair| match pair.as_rule() {
        Rule::unbounded => Ok(UpperBound::Unbounded),
        Rule::digits => Ok(UpperBound::Count(pair.as_str())),
        _ => Err(internal(&pair)),
    })?;
    Ok(Cardinality { min, max })
}

fn typed_name(pair: Token<'_>) -> Parsed<TypedName<'_>> {
    let mut children = Children::new(pair);
    Ok(TypedName {
        name: name(children.required()?),
        ty: node(children.required()?, value_type)?,
    })
}

fn enumeration(pair: Token<'_>) -> Parsed<Enumeration<'_>> {
    let mut children = Children::new(pair);
    Ok(Enumeration {
        name: name(children.required()?),
        variants: children
            .rest()
            .map(|pair| {
                node(pair, |pair| {
                    Ok(EnumVariant {
                        name: name(only_child(pair)?),
                    })
                })
            })
            .collect::<Result<_, _>>()?,
    })
}

fn value_type(pair: Token<'_>) -> Parsed<Type<'_>> {
    Ok(match pair.as_rule() {
        Rule::primitive => Type::Primitive(primitive(&pair)?),
        Rule::path => Type::Named(path_parts(pair)),
        Rule::option_type => {
            Type::Option(Box::new(node(only_child(pair)?, value_type)?))
        },
        Rule::list_type => {
            Type::List(Box::new(node(only_child(pair)?, value_type)?))
        },
        Rule::ref_type => Type::Ref(path(only_child(pair)?)),
        _ => return Err(internal(&pair)),
    })
}

fn primitive(pair: &Token<'_>) -> Parsed<Primitive> {
    Ok(match pair.as_str() {
        "Unit" => Primitive::Unit,
        "bool" => Primitive::Bool,
        "char" => Primitive::Char,
        "String" => Primitive::String,
        "i8" => Primitive::I8,
        "i16" => Primitive::I16,
        "i32" => Primitive::I32,
        "i64" => Primitive::I64,
        "i128" => Primitive::I128,
        "u8" => Primitive::U8,
        "u16" => Primitive::U16,
        "u32" => Primitive::U32,
        "u64" => Primitive::U64,
        "u128" => Primitive::U128,
        "f32" => Primitive::F32,
        "f64" => Primitive::F64,
        _ => return Err(internal(pair)),
    })
}

fn invariant(pair: Token<'_>) -> Parsed<Invariant<'_>> {
    let mut children = Children::new(pair);
    Ok(Invariant {
        name: name(children.required()?),
        target: path(children.required()?),
        condition: expression::lower(children.required()?)?,
    })
}

fn derivation(pair: Token<'_>) -> Parsed<Derivation<'_>> {
    let mut children = Children::new(pair);
    Ok(Derivation {
        name: name(children.required()?),
        target: path(children.required()?),
        ty: node(children.required()?, value_type)?,
        value: expression::lower(children.required()?)?,
    })
}

fn view(pair: Token<'_>) -> Parsed<View<'_>> {
    let mut children = Children::new(pair);
    let name = name(children.required()?);
    let source = path(children.required()?);
    let mut projections = Vec::new();
    while let Some(pair) = children.optional(Rule::projection) {
        projections.push(node(pair, projection)?);
    }
    let filter = children
        .optional(Rule::view_filter)
        .map(expression::clause)
        .transpose()?;
    Ok(View {
        name,
        source,
        projections,
        filter,
    })
}

fn projection(pair: Token<'_>) -> Parsed<Projection<'_>> {
    let mut children = Children::new(pair);
    Ok(Projection {
        expression: expression::lower(children.required()?)?,
        alias: children.optional(Rule::ident).map(name),
    })
}

fn action(pair: Token<'_>) -> Parsed<Action<'_>> {
    let mut children = Children::new(pair);
    let name = name(children.required()?);
    let parameters = children
        .required()?
        .into_inner()
        .map(|pair| node(pair, typed_name))
        .collect::<Result<_, _>>()?;
    let result = children
        .optional(Rule::action_result)
        .map(|pair| node(only_child(pair)?, value_type))
        .transpose()?;
    let clauses = children
        .rest()
        .map(|pair| node(pair, action_clause))
        .collect::<Result<_, _>>()?;
    Ok(Action {
        name,
        parameters,
        result,
        clauses,
    })
}

fn action_clause(pair: Token<'_>) -> Parsed<ActionClause<'_>> {
    Ok(match pair.as_rule() {
        Rule::requires_clause => {
            ActionClause::Requires(expression::clause(pair)?)
        },
        Rule::ensures_clause => {
            ActionClause::Ensures(expression::clause(pair)?)
        },
        Rule::effects_clause => ActionClause::Effects(
            pair.into_inner()
                .map(|pair| node(pair, effect))
                .collect::<Result<_, _>>()?,
        ),
        Rule::retry_clause => {
            ActionClause::Retry(node(only_child(pair)?, retry)?)
        },
        _ => return Err(internal(&pair)),
    })
}

fn effect(pair: Token<'_>) -> Parsed<Effect<'_>> {
    Ok(match pair.as_rule() {
        Rule::read_effect => Effect::Read(path(only_child(pair)?)),
        Rule::write_effect => Effect::Write(path(only_child(pair)?)),
        Rule::emit_effect => Effect::Emit(path(only_child(pair)?)),
        _ => return Err(internal(&pair)),
    })
}

fn retry(pair: Token<'_>) -> Parsed<Retry<'_>> {
    Ok(match pair.as_rule() {
        Rule::idempotent => Retry::Idempotent,
        Rule::non_idempotent => Retry::NonIdempotent,
        Rule::deduplicated => Retry::Deduplicated(name(only_child(pair)?)),
        _ => return Err(internal(&pair)),
    })
}

#[cfg(test)]
mod tests {
    use std::borrow::Cow;
    use std::error::Error;

    use pest::Parser as _;

    use super::{GolParser, Rule};
    use crate::syntax::expression::decode_text;

    #[test]
    fn unescaped_text_borrows_the_original_bytes() -> Result<(), Box<dyn Error>>
    {
        let source = r#""café 🔒""#;
        let mut tokens = GolParser::parse(Rule::string, source)?;
        let token = tokens.next().ok_or("missing string token")?;
        let decoded =
            decode_text(&token).map_err(|error| format!("{error:?}"))?;
        let Cow::Borrowed(text) = decoded else {
            return Err("plain text should borrow the input".into());
        };
        let original = source
            .strip_prefix('"')
            .and_then(|s| s.strip_suffix('"'))
            .ok_or("missing quotes")?;
        assert!(std::ptr::eq(text, original));
        Ok(())
    }

    #[test]
    fn escaped_backslashes_and_unicode_decode_once()
    -> Result<(), Box<dyn Error>> {
        let source = r#""\\n \u{10FFFF}""#;
        let mut tokens = GolParser::parse(Rule::string, source)?;
        let token = tokens.next().ok_or("missing string token")?;
        let decoded =
            decode_text(&token).map_err(|error| format!("{error:?}"))?;
        assert_eq!(decoded, "\\n \u{10ffff}");
        Ok(())
    }
}

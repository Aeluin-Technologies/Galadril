//! Expression pairs retain precedence and source spans in a bounded-depth AST.

use std::borrow::Cow;

use super::ast::{
    BinaryOp, BinaryTerm, Expression, Literal, Node, Span, UnaryOp,
};
use super::diagnostic::{ErrorCode, Failure};
use super::parser::{
    Children, Parsed, Rule, Token, internal, name, node, only_child,
    path_parts, span,
};

pub(super) fn clause(pair: Token<'_>) -> Parsed<Node<Expression<'_>>> {
    lower(only_child(pair)?)
}

pub(super) fn lower(pair: Token<'_>) -> Parsed<Node<Expression<'_>>> {
    match pair.as_rule() {
        Rule::disjunction |
        Rule::conjunction |
        Rule::equality |
        Rule::comparison |
        Rule::sum |
        Rule::product => binary(pair),
        Rule::unary => unary(pair),
        Rule::postfix => postfix(pair),
        Rule::grouped => node(pair, |pair| {
            Ok(Expression::Grouped(Box::new(clause(pair)?)))
        }),
        Rule::symbol => {
            node(pair, |pair| Ok(Expression::Symbol(path_parts(pair))))
        },
        Rule::boolean |
        Rule::digits |
        Rule::float |
        Rule::string |
        Rule::character => {
            node(pair, |pair| literal(&pair).map(Expression::Literal))
        },
        _ => Err(internal(&pair)),
    }
}

fn binary(pair: Token<'_>) -> Parsed<Node<Expression<'_>>> {
    let span = span(&pair);
    let mut children = Children::new(pair);
    let first = lower(children.required()?)?;
    let mut terms = Vec::new();
    let mut rest = children.rest();
    while let Some(operator) = rest.next() {
        let operand = rest.next().ok_or_else(|| internal(&operator))?;
        terms.push(BinaryTerm {
            operator: node(operator, |pair| binary_operator(&pair))?,
            operand: lower(operand)?,
        });
    }
    if terms.is_empty() {
        return Ok(first);
    }
    Ok(Node::new(
        span,
        Expression::Binary {
            first: Box::new(first),
            rest: terms,
        },
    ))
}

fn binary_operator(pair: &Token<'_>) -> Parsed<BinaryOp> {
    Ok(match pair.as_str() {
        "||" => BinaryOp::Or,
        "&&" => BinaryOp::And,
        "==" => BinaryOp::Equal,
        "!=" => BinaryOp::NotEqual,
        "<" => BinaryOp::Less,
        "<=" => BinaryOp::LessEqual,
        ">" => BinaryOp::Greater,
        ">=" => BinaryOp::GreaterEqual,
        "+" => BinaryOp::Add,
        "-" => BinaryOp::Subtract,
        "*" => BinaryOp::Multiply,
        "/" => BinaryOp::Divide,
        "%" => BinaryOp::Remainder,
        _ => return Err(internal(pair)),
    })
}

fn unary(pair: Token<'_>) -> Parsed<Node<Expression<'_>>> {
    let span = span(&pair);
    let mut children = Children::new(pair);
    let mut operators = Vec::new();
    while let Some(operator) = children.optional(Rule::unary_op) {
        operators.push(node(operator, |pair| match pair.as_str() {
            "!" => Ok(UnaryOp::Not),
            "-" => Ok(UnaryOp::Negate),
            _ => Err(internal(&pair)),
        })?);
    }
    let operand = lower(children.required()?)?;
    if operators.is_empty() {
        return Ok(operand);
    }
    Ok(Node::new(
        span,
        Expression::Unary {
            operators,
            operand: Box::new(operand),
        },
    ))
}

fn postfix(pair: Token<'_>) -> Parsed<Node<Expression<'_>>> {
    let span = span(&pair);
    let mut children = Children::new(pair);
    let base = lower(children.required()?)?;
    let fields: Vec<_> = children.rest().map(name).collect();
    if fields.is_empty() {
        return Ok(base);
    }
    Ok(Node::new(
        span,
        Expression::Member {
            base: Box::new(base),
            fields,
        },
    ))
}

fn literal<'a>(pair: &Token<'a>) -> Parsed<Literal<'a>> {
    Ok(match pair.as_rule() {
        Rule::boolean => Literal::Bool(pair.as_str() == "true"),
        Rule::digits => Literal::Integer(pair.as_str()),
        Rule::float => Literal::Float(pair.as_str()),
        Rule::string => Literal::String(decode_text(pair)?),
        Rule::character => Literal::Char(
            decode_text(pair)?
                .chars()
                .next()
                .ok_or_else(|| internal(pair))?,
        ),
        _ => return Err(internal(pair)),
    })
}

pub(super) fn decode_text<'a>(pair: &Token<'a>) -> Parsed<Cow<'a, str>> {
    let raw = pair.as_str();
    let body = raw
        .get(1..raw.len().saturating_sub(1))
        .ok_or_else(|| internal(pair))?;
    if !body.contains('\\') {
        return Ok(Cow::Borrowed(body));
    }
    let mut decoded = String::with_capacity(body.len());
    let mut tail = body;
    while let Some(offset) = tail.find('\\') {
        let (prefix, escape) = tail.split_at(offset);
        decoded.push_str(prefix);
        let start = span(pair).start + 1 + body.len() - escape.len();
        let (value, rest) = decode_escape(escape, start, pair)?;
        decoded.push(value);
        tail = rest;
    }
    decoded.push_str(tail);
    Ok(Cow::Owned(decoded))
}

fn decode_escape<'a>(
    escape: &'a str,
    start: usize,
    pair: &Token<'_>,
) -> Parsed<(char, &'a str)> {
    if let Some(hex) = escape.strip_prefix("\\u{") {
        let (digits, rest) =
            hex.split_once('}').ok_or_else(|| internal(pair))?;
        let value = u32::from_str_radix(digits, 16)
            .ok()
            .and_then(char::from_u32)
            .ok_or_else(|| {
                Failure::new(
                    ErrorCode::InvalidEscape,
                    Span {
                        start,
                        end: start + escape.len() - rest.len(),
                    },
                )
            })?;
        return Ok((value, rest));
    }
    let value = match escape.as_bytes().get(1) {
        Some(b'"') => '"',
        Some(b'\'') => '\'',
        Some(b'\\') => '\\',
        Some(b'n') => '\n',
        Some(b'r') => '\r',
        Some(b't') => '\t',
        Some(b'0') => '\0',
        _ => return Err(internal(pair)),
    };
    let rest = escape.get(2..).ok_or_else(|| internal(pair))?;
    Ok((value, rest))
}

//! Conversion of scoped security syntax into declarative AST records.

use super::ast::{
    Access, AccessMember, Binding, FieldGuard, Label, LabelMember, Permission,
    Policy, PolicyKind,
};
use super::expression;
use super::parser::{
    Children, Parsed, Rule, Token, internal, name, node, only_child, path,
    text,
};
use crate::security::Classification;

pub(super) fn access(pair: Token<'_>) -> Parsed<Access<'_>> {
    let mut children = Children::new(pair);
    Ok(Access {
        target: path(children.required()?),
        members: children
            .rest()
            .map(|pair| node(pair, access_member))
            .collect::<Result<_, _>>()?,
    })
}

fn access_member(pair: Token<'_>) -> Parsed<AccessMember<'_>> {
    let rule = pair.as_rule();
    let mut children = Children::new(pair);
    Ok(match rule {
        Rule::resource => AccessMember::Resource(text(children.required()?)?),
        Rule::permission => AccessMember::Permission(Permission {
            name: name(children.required()?),
            bindings: children
                .rest()
                .map(|pair| node(pair, binding))
                .collect::<Result<_, _>>()?,
        }),
        Rule::field_guard => AccessMember::Field(FieldGuard {
            field: path(children.required()?),
            permission: path(children.required()?),
        }),
        _ => return Err(children.unexpected()),
    })
}

fn binding(pair: Token<'_>) -> Parsed<Binding<'_>> {
    match pair.as_rule() {
        Rule::spicedb_binding => text(only_child(pair)?).map(Binding::SpiceDb),
        Rule::cedar_binding => text(only_child(pair)?).map(Binding::Cedar),
        _ => Err(internal(&pair)),
    }
}

pub(super) fn policy(pair: Token<'_>) -> Parsed<Policy<'_>> {
    let mut children = Children::new(pair);
    let kind = node(children.required()?, |pair| match pair.as_rule() {
        Rule::permit => Ok(PolicyKind::Permit),
        Rule::forbid => Ok(PolicyKind::Forbid),
        _ => Err(internal(&pair)),
    })?;
    Ok(Policy {
        kind,
        target: path(children.required()?),
        when: children
            .optional(Rule::when_clause)
            .map(expression::clause)
            .transpose()?,
        unless: children
            .optional(Rule::unless_clause)
            .map(expression::clause)
            .transpose()?,
    })
}

pub(super) fn label(pair: Token<'_>) -> Parsed<Label<'_>> {
    let mut children = Children::new(pair);
    Ok(Label {
        name: name(children.required()?),
        members: children
            .rest()
            .map(|pair| node(pair, label_member))
            .collect::<Result<_, _>>()?,
    })
}

fn label_member(pair: Token<'_>) -> Parsed<LabelMember<'_>> {
    Ok(match pair.as_rule() {
        Rule::classification_clause => LabelMember::Classification(node(
            only_child(pair)?,
            classification,
        )?),
        Rule::integrity => LabelMember::Integrity(path(only_child(pair)?)),
        Rule::compartments => LabelMember::Compartments(
            only_child(pair)?.into_inner().map(path).collect(),
        ),
        Rule::destinations => LabelMember::Destinations(
            only_child(pair)?.into_inner().map(path).collect(),
        ),
        Rule::purposes => LabelMember::Purposes(
            only_child(pair)?.into_inner().map(path).collect(),
        ),
        _ => return Err(internal(&pair)),
    })
}

fn classification(pair: Token<'_>) -> Parsed<Classification> {
    match pair.as_str() {
        "Public" => Ok(Classification::Public),
        "Internal" => Ok(Classification::Internal),
        "Confidential" => Ok(Classification::Confidential),
        "Restricted" => Ok(Classification::Restricted),
        _ => Err(internal(&pair)),
    }
}

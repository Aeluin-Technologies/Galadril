//! Resolve signatures before checking dependent expression bodies.

use std::collections::{BTreeMap, BTreeSet};
use std::num::NonZeroU32;

use super::diagnostic::{CheckedResult, Code, Diagnostic};
use super::index::{Index, Scope, Target, resolve_symbol, spelling};
use crate::syntax::{self as ast, Declaration as Ast};
use crate::{
    Action, Attribute, Cardinality, Declaration, Entity, EnumType, Field,
    FieldKind, FieldRef, Named, Primitive, Relation, Role, StructType,
    SymbolKind, SymbolRef, TypeAlias, TypeRef,
};

pub(super) type Facts = BTreeMap<SymbolRef, Declaration>;

pub(super) fn signatures(
    index: &Index<'_>,
    scopes: &BTreeMap<crate::OntologyId, Scope<'_>>,
    max_length: NonZeroU32,
    errors: &mut Vec<Diagnostic>,
) -> Facts {
    let mut facts = Facts::new();
    for (reference, item) in &index.items {
        if let Some(scope) = scopes.get(reference.key().ontology()) {
            match signature(scope, index, &item.node.value, max_length) {
                Ok(Some(signature)) => {
                    facts.insert(reference.clone(), signature);
                },
                Ok(None) => {},
                Err(error) => errors.push(error),
            }
        }
    }
    facts
}

fn signature(
    scope: &Scope<'_>,
    index: &Index<'_>,
    declaration: &Ast<'_>,
    max_length: NonZeroU32,
) -> CheckedResult<Option<Declaration>> {
    Ok(Some(match declaration {
        Ast::Entity(record) => {
            let (fields, _) = members(scope, index, record, max_length)?;
            Declaration::Entity(Entity {
                fields,
                ..Entity::default()
            })
        },
        Ast::Relation(record) => {
            let (fields, roles) = members(scope, index, record, max_length)?;
            if roles.is_empty() {
                return Err(scope.error(
                    record.name.span,
                    Code::InvalidRelationRole,
                    "A relation must declare at least one participant role",
                ));
            }
            Declaration::Relation(Relation {
                fields,
                roles,
                ..Relation::default()
            })
        },
        Ast::Struct(record) => {
            let (fields, _) = members(scope, index, record, max_length)?;
            Declaration::Struct(StructType {
                fields,
                ..StructType::default()
            })
        },
        Ast::Attribute(value) => Declaration::Attribute(Attribute::new(
            resolve_type(scope, index, &value.ty, max_length)?,
        )),
        Ast::TypeAlias(value) => Declaration::TypeAlias(TypeAlias::new(
            resolve_type(scope, index, &value.ty, max_length)?,
        )),
        Ast::Enum(value) => {
            let mut variants = BTreeSet::new();
            for variant in &value.variants {
                if !variants.insert(scope.name(&variant.value.name)?) {
                    return Err(scope.error(
                        variant.span,
                        Code::DuplicateSymbol,
                        "Duplicate enum variant",
                    ));
                }
            }
            Declaration::Enum(EnumType::new(variants))
        },
        Ast::Action(value) => {
            let result = value
                .result
                .as_ref()
                .map(|ty| resolve_type(scope, index, ty, max_length))
                .transpose()?
                .unwrap_or(Primitive::Unit.into());
            let mut action = Action::new(result);
            for parameter in &value.parameters {
                let name = scope.name(&parameter.value.name)?;
                if name.as_str() == "result" {
                    return Err(scope.error(
                        parameter.span,
                        Code::InvalidActionContract,
                        "The result name is reserved for postconditions",
                    ));
                }
                let ty = resolve_type(
                    scope,
                    index,
                    &parameter.value.ty,
                    max_length,
                )?;
                insert(
                    scope,
                    &mut action.parameters,
                    name,
                    ty,
                    parameter.span,
                )?;
            }
            Declaration::Action(action)
        },
        _ => return Ok(None),
    }))
}

fn members(
    scope: &Scope<'_>,
    index: &Index<'_>,
    record: &ast::Record<'_>,
    max_length: NonZeroU32,
) -> CheckedResult<(Named<Field>, Named<Role>)> {
    let mut fields = Named::new();
    let mut roles = Named::new();
    for member in &record.members {
        match &member.value {
            ast::Member::Field(field) => {
                let ty = resolve_type(scope, index, &field.ty, max_length)?;
                let field_kind = match field.kind {
                    ast::FieldKind::Field => FieldKind::Value(ty),
                    ast::FieldKind::Key => FieldKind::Key(ty),
                };
                insert(
                    scope,
                    &mut fields,
                    scope.name(&field.name)?,
                    Field::new(field_kind),
                    member.span,
                )?;
            },
            ast::Member::Owns(owned) => {
                let attribute = resolve_symbol(
                    scope,
                    index,
                    &owned.attribute.value.segments,
                    owned.attribute.span,
                    Code::UnknownType,
                    |kind| kind == SymbolKind::Attribute,
                )?;
                let name = attribute.key().name().clone();
                let cardinality = cardinality(scope, &owned.cardinality)?;
                insert(
                    scope,
                    &mut fields,
                    name,
                    Field::new(FieldKind::OwnedAttribute {
                        attribute,
                        cardinality,
                    }),
                    member.span,
                )?;
            },
            ast::Member::Role(role) => {
                let name = scope.name(&role.name)?;
                let entity = resolve_symbol(
                    scope,
                    index,
                    &role.target.value.segments,
                    role.target.span,
                    Code::InvalidRelationRole,
                    |kind| kind == SymbolKind::Entity,
                )?;
                if roles.get(name.as_str()).is_some() {
                    return Err(scope.error(
                        role.name.span,
                        Code::InvalidRelationRole,
                        "Duplicate relation role",
                    ));
                }
                let cardinality = cardinality(scope, &role.cardinality)?;
                insert(
                    scope,
                    &mut roles,
                    name,
                    Role::new(entity, cardinality),
                    member.span,
                )?;
            },
        }
    }
    Ok((fields, roles))
}

pub(super) fn insert<T>(
    scope: &Scope<'_>,
    named: &mut Named<T>,
    name: crate::Name,
    value: T,
    span: ast::Span,
) -> CheckedResult<()> {
    named.insert(name, value).map_err(|error| {
        scope.error(span, Code::DuplicateSymbol, error.to_string())
    })
}

fn cardinality(
    scope: &Scope<'_>,
    node: &ast::Node<ast::Cardinality<'_>>,
) -> CheckedResult<Cardinality> {
    let bounds = &node.value;
    let parse = |text: &str, span| {
        text.parse::<u32>().map_err(|_| {
            scope.error(
                span,
                Code::InvalidCardinality,
                "Cardinality bound does not fit in u32",
            )
        })
    };
    let min = parse(bounds.min.value, bounds.min.span)?;
    let max = match bounds.max.value {
        ast::UpperBound::Count(value) => Some(parse(value, bounds.max.span)?),
        ast::UpperBound::Unbounded => None,
    };
    Cardinality::new(min, max).map_err(|error| {
        scope.error(node.span, Code::InvalidCardinality, error.to_string())
    })
}

pub(super) fn resolve_type(
    scope: &Scope<'_>,
    index: &Index<'_>,
    node: &ast::Node<ast::Type<'_>>,
    max_length: NonZeroU32,
) -> CheckedResult<TypeRef> {
    Ok(match &node.value {
        ast::Type::Primitive(ty) => (*ty).into(),
        ast::Type::Named(path) => match scope.find(&path.segments) {
            Some(Target::External(id)) => TypeRef::External(id.clone()),
            _ => TypeRef::Named(resolve_symbol(
                scope,
                index,
                &path.segments,
                node.span,
                Code::UnknownType,
                |kind| {
                    matches!(
                        kind,
                        SymbolKind::TypeAlias |
                            SymbolKind::Struct |
                            SymbolKind::Enum
                    )
                },
            )?),
        },
        ast::Type::Ref(path) => TypeRef::Entity(resolve_symbol(
            scope,
            index,
            &path.value.segments,
            path.span,
            Code::InvalidGenericArgument,
            |kind| kind == SymbolKind::Entity,
        )?),
        ast::Type::Option(inner) => TypeRef::Option(Box::new(resolve_type(
            scope, index, inner, max_length,
        )?)),
        ast::Type::List(inner) => TypeRef::List {
            element: Box::new(resolve_type(scope, index, inner, max_length)?),
            max_length,
        },
    })
}

pub(super) fn named_fields(
    declaration: &Declaration,
) -> Option<&Named<Field>> {
    match declaration {
        Declaration::Entity(entity) => Some(&entity.fields),
        Declaration::Relation(relation) => Some(&relation.fields),
        Declaration::Struct(record) => Some(&record.fields),
        _ => None,
    }
}

pub(super) fn declared_field<'a>(
    facts: &'a Facts,
    reference: &FieldRef,
) -> Option<&'a Field> {
    named_fields(facts.get(&reference.owner)?)?.get(reference.name.as_str())
}

pub(super) fn field_target(
    scope: &Scope<'_>,
    index: &Index<'_>,
    facts: &Facts,
    path: &ast::Node<ast::Path<'_>>,
    code: Code,
) -> CheckedResult<FieldRef> {
    let names = &path.value.segments;
    if let Some((name, owner)) = names.split_last() {
        let reference =
            resolve_symbol(scope, index, owner, path.span, code, |kind| {
                matches!(
                    kind,
                    SymbolKind::Entity |
                        SymbolKind::Relation |
                        SymbolKind::Struct
                )
            })?;
        let field = FieldRef::new(reference, scope.name(name)?);
        if declared_field(facts, &field).is_some() {
            return Ok(field);
        }
    }
    Err(scope.error(
        path.span,
        code,
        format!("Unknown field {}", spelling(names)),
    ))
}

pub(super) fn field_type(facts: &Facts, field: &Field) -> Option<TypeRef> {
    match &field.kind {
        FieldKind::Key(ty) | FieldKind::Value(ty) => Some(ty.clone()),
        FieldKind::OwnedAttribute {
            attribute,
            cardinality,
        } => {
            let Declaration::Attribute(attribute) = facts.get(attribute)?
            else {
                return None;
            };
            match (cardinality.min(), cardinality.max()) {
                (1, Some(1)) => Some(attribute.value_type.clone()),
                (0, Some(1)) => Some(TypeRef::Option(Box::new(
                    attribute.value_type.clone(),
                ))),
                (_, Some(maximum)) => Some(TypeRef::List {
                    element: Box::new(attribute.value_type.clone()),
                    max_length: NonZeroU32::new(maximum)?,
                }),
                _ => None,
            }
        },
    }
}

pub(super) fn peel<'a>(facts: &'a Facts, mut ty: &'a TypeRef) -> &'a TypeRef {
    while let TypeRef::Named(reference) = ty &&
        let Some(Declaration::TypeAlias(alias)) = facts.get(reference)
    {
        ty = &alias.target;
    }
    ty
}

/// Compares nested wrapper types without expanding aliases into owned trees.
pub(super) fn compatible<'a>(
    facts: &'a Facts,
    mut left: &'a TypeRef,
    mut right: &'a TypeRef,
) -> bool {
    loop {
        left = peel(facts, left);
        right = peel(facts, right);
        match (left, right) {
            (TypeRef::Option(a), TypeRef::Option(b)) => {
                left = a;
                right = b;
            },
            (
                TypeRef::List {
                    element: a,
                    max_length: max_a,
                },
                TypeRef::List {
                    element: b,
                    max_length: max_b,
                },
            ) if max_a == max_b => {
                left = a;
                right = b;
            },
            _ => return left == right,
        }
    }
}

/// Only by-value definitions participate in layout cycles. A Ref stores an
/// identity, so mutually referring entity types remain valid.
pub(super) fn type_cycle(facts: &Facts) -> Option<&SymbolRef> {
    let mut graph = BTreeMap::<&SymbolRef, BTreeSet<&SymbolRef>>::new();
    for (reference, declaration) in facts {
        match declaration {
            Declaration::TypeAlias(alias) => add_value_edge(
                graph.entry(reference).or_default(),
                &alias.target,
            ),
            Declaration::Struct(record) => {
                let edges = graph.entry(reference).or_default();
                for (_, field) in record.fields.iter() {
                    if let FieldKind::Value(ty) = &field.kind {
                        add_value_edge(edges, ty);
                    }
                }
            },
            _ => continue,
        }
    }
    cycle(&graph)
}

fn add_value_edge<'a>(
    edges: &mut BTreeSet<&'a SymbolRef>,
    mut ty: &'a TypeRef,
) {
    loop {
        match ty {
            TypeRef::Option(inner) | TypeRef::List { element: inner, .. } => {
                ty = inner
            },
            TypeRef::Named(target) => {
                edges.insert(target);
                return;
            },
            _ => return,
        }
    }
}

/// An explicit DFS stack bounds call-stack use for long chains of aliases.
pub(super) fn cycle<'a, T: Ord>(
    graph: &BTreeMap<&'a T, BTreeSet<&'a T>>,
) -> Option<&'a T> {
    let mut complete = BTreeSet::new();
    let mut active = BTreeSet::new();
    for &start in graph.keys() {
        if complete.contains(start) {
            continue;
        }
        active.insert(start);
        let mut stack =
            vec![(start, graph.get(start).into_iter().flatten().copied())];
        while let Some((owner, edges)) = stack.last_mut() {
            if let Some(target) = edges.next() {
                if complete.contains(target) {
                    continue;
                }
                if !active.insert(target) {
                    return Some(target);
                }
                stack.push((
                    target,
                    graph.get(target).into_iter().flatten().copied(),
                ));
            } else {
                complete.insert(*owner);
                active.remove(*owner);
                stack.pop();
            }
        }
    }
    None
}

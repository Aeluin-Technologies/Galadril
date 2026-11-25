//! Build owner indexes without changing the compiler's existing expression IR.

use std::collections::BTreeMap;

use super::{Budget, Contract, Error, Limits, Predicate, preflight, visit};
use crate::checked::{Checked, Value};
use crate::semantic::Project;
use crate::{Declaration, SymbolRef, TypeRef};

pub(crate) fn contracts(
    project: &Project,
) -> Result<BTreeMap<SymbolRef, Contract>, Error> {
    let mut contracts = BTreeMap::new();
    for owner in project.symbols().keys() {
        let declaration = project
            .modules()
            .get(owner.key().ontology())
            .and_then(|module| module.resolve_local(owner))
            .ok_or(Error::Invalid)?;
        if matches!(
            declaration,
            Declaration::Entity(_) |
                Declaration::Struct(_) |
                Declaration::Relation(_)
        ) {
            contracts.insert(
                owner.clone(),
                Contract {
                    owner: owner.clone(),
                    predicates: Vec::new(),
                },
            );
        }
    }
    for module in project.modules().values() {
        for (_, declaration) in module.declarations().iter() {
            let Declaration::Invariant(invariant) = declaration else {
                continue;
            };
            let contract = contracts
                .get_mut(&invariant.subject)
                .ok_or(Error::Invalid)?;
            let name = &invariant.condition.expression.definition;
            let body =
                project.expressions().get(name).ok_or(Error::Invalid)?;
            visit(body, &mut Budget::new(Limits::default()), 0)?;
            contract.predicates.push(Predicate {
                name: name.clone(),
                body: normalize(body, project, 0)?,
            });
        }
    }
    for contract in contracts.values_mut() {
        contract
            .predicates
            .sort_by(|left, right| left.name.cmp(&right.name));
        contract.structure(Limits::default())?;
    }
    Ok(contracts)
}

impl Contract {
    pub(crate) fn encode(&self) -> Result<Vec<u8>, Error> {
        let bytes = serde_json::to_vec(self).map_err(|_| Error::Invalid)?;
        preflight(&bytes, Limits::default())?;
        Ok(bytes)
    }
}

fn normalize(
    expression: &Checked,
    project: &Project,
    depth: usize,
) -> Result<Checked, Error> {
    if depth > Limits::default().max_depth {
        return Err(Error::Budget);
    }
    let value = match &expression.value {
        Value::Field { base, path } => Value::Field {
            base: base
                .as_deref()
                .map(|base| normalize(base, project, depth + 1).map(Box::new))
                .transpose()?,
            path: path.clone(),
        },
        Value::Unary(operators, operand) => Value::Unary(
            operators.clone(),
            Box::new(normalize(operand, project, depth + 1)?),
        ),
        Value::Binary(first, rest) => Value::Binary(
            Box::new(normalize(first, project, depth + 1)?),
            rest.iter()
                .map(|(operator, right)| {
                    Ok((*operator, normalize(right, project, depth + 1)?))
                })
                .collect::<Result<_, Error>>()?,
        ),
        value => value.clone(),
    };
    Ok(Checked {
        ty: normalize_type(&expression.ty, project, 0)?,
        value,
    })
}

fn normalize_type(
    ty: &TypeRef,
    project: &Project,
    depth: usize,
) -> Result<TypeRef, Error> {
    if depth > Limits::default().max_depth {
        return Err(Error::Budget);
    }
    Ok(match ty {
        TypeRef::Named(owner) => {
            let declaration = project
                .modules()
                .get(owner.key().ontology())
                .and_then(|module| module.resolve_local(owner))
                .ok_or(Error::Invalid)?;
            match declaration {
                Declaration::TypeAlias(alias) => {
                    return normalize_type(&alias.target, project, depth + 1);
                },
                _ => ty.clone(),
            }
        },
        TypeRef::Option(inner) => TypeRef::Option(Box::new(normalize_type(
            inner,
            project,
            depth + 1,
        )?)),
        TypeRef::List {
            element,
            max_length,
        } => TypeRef::List {
            element: Box::new(normalize_type(element, project, depth + 1)?),
            max_length: *max_length,
        },
        _ => ty.clone(),
    })
}

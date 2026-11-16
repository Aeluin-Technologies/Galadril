//! Link domain-side security targets while retaining external obligations.

use std::collections::{BTreeMap, BTreeSet};

use super::diagnostic::{CheckedResult, Code, Diagnostic};
use super::expression::Checker;
use super::index::{Index, Scope, resolve_symbol, spelling};
use super::ir::{
    Binding, Environment, LabelTransfer, PermissionRef, Policy, Project,
    Security,
};
use super::types::{Facts, declared_field};
use crate::syntax::{self as ast, Declaration as Ast};
use crate::{
    FieldRef, IfcLabel, Name, Primitive, QualifiedName, Reference, SymbolKind,
    SymbolRef,
};

pub(super) fn label(
    scope: &Scope<'_>,
    label: &ast::Label<'_>,
) -> CheckedResult<(IfcLabel, LabelTransfer)> {
    let mut classification = None;
    let mut compartments = None;
    let mut integrity = None;
    let mut destinations = None;
    let mut purposes = None;
    for member in &label.members {
        let previous = match &member.value {
            ast::LabelMember::Classification(value) => {
                classification.replace(value.value).is_some()
            },
            ast::LabelMember::Compartments(paths) => {
                compartments.replace(names(scope, paths)?).is_some()
            },
            ast::LabelMember::Integrity(path) => {
                integrity.replace(qualified(scope, path)?).is_some()
            },
            ast::LabelMember::Destinations(paths) => {
                destinations.replace(names(scope, paths)?).is_some()
            },
            ast::LabelMember::Purposes(paths) => {
                purposes.replace(names(scope, paths)?).is_some()
            },
        };
        if previous {
            return Err(scope.error(
                member.span,
                Code::InvalidSecurityTarget,
                "Duplicate label property",
            ));
        }
    }
    let classification = classification.ok_or_else(|| {
        scope.error(
            label.name.span,
            Code::InvalidSecurityTarget,
            "An IFC label requires an explicit classification",
        )
    })?;
    let mut definition = IfcLabel::new(classification);
    definition.compartments = compartments.unwrap_or_default();
    definition.integrity = integrity;
    Ok((
        definition,
        LabelTransfer {
            destinations,
            purposes,
        },
    ))
}

fn names(
    scope: &Scope<'_>,
    paths: &[ast::Node<ast::Path<'_>>],
) -> CheckedResult<BTreeSet<QualifiedName>> {
    paths.iter().map(|path| qualified(scope, path)).collect()
}

pub(super) fn qualified(
    scope: &Scope<'_>,
    path: &ast::Node<ast::Path<'_>>,
) -> CheckedResult<QualifiedName> {
    spelling(&path.value.segments).parse().map_err(|_| {
        scope.error(path.span, Code::InvalidName, "Invalid contract name")
    })
}

pub(super) fn validate(
    index: &Index<'_>,
    scopes: &BTreeMap<crate::OntologyId, Scope<'_>>,
    facts: &Facts,
    environment: &Environment<'_>,
    project: &mut Project,
    errors: &mut Vec<Diagnostic>,
) {
    // Declare permissions before resolving field guards and policies. This
    // also makes a guard independent of where its access block appears in
    // a source.
    let mut written = BTreeMap::new();
    for scope in scopes.values() {
        for node in &scope.module.ast.value.declarations {
            if let Ast::Access(access) = &node.value &&
                let Err(error) = access_bindings(
                    scope,
                    index,
                    access,
                    &mut project.security,
                    &mut written,
                )
            {
                errors.push(error);
            }
        }
    }
    if !errors.is_empty() {
        return;
    }
    for scope in scopes.values() {
        for node in &scope.module.ast.value.declarations {
            let result = match &node.value {
                Ast::Access(access) => {
                    guards(scope, index, facts, access, &mut project.security)
                },
                Ast::Policy(policy) => check_policy(
                    scope,
                    index,
                    facts,
                    environment,
                    policy,
                    &mut project.security,
                ),
                Ast::Entity(record) |
                Ast::Relation(record) |
                Ast::Struct(record) => {
                    annotations(
                        scope,
                        index,
                        record,
                        &mut project.security,
                        errors,
                    );
                    Ok(())
                },
                _ => Ok(()),
            };
            if let Err(error) = result {
                errors.push(error);
            }
        }
    }
}

fn resource(
    scope: &Scope<'_>,
    index: &Index<'_>,
    path: &ast::Node<ast::Path<'_>>,
) -> CheckedResult<SymbolRef> {
    resolve_symbol(
        scope,
        index,
        &path.value.segments,
        path.span,
        Code::InvalidSecurityTarget,
        protectable,
    )
}

fn protectable(kind: SymbolKind) -> bool {
    matches!(
        kind,
        SymbolKind::Entity |
            SymbolKind::Relation |
            SymbolKind::Struct |
            SymbolKind::View |
            SymbolKind::Action |
            SymbolKind::Enum |
            SymbolKind::TypeAlias |
            SymbolKind::Attribute
    )
}

fn access_bindings<'a>(
    scope: &Scope<'a>,
    index: &Index<'_>,
    access: &ast::Access<'_>,
    contracts: &mut BTreeMap<Reference, Security>,
    written: &mut BTreeMap<SymbolRef, (&'a str, ast::Span)>,
) -> CheckedResult<()> {
    let target = resource(scope, index, &access.target)?;
    if let Some((source, span)) = written.get(&target) {
        return Err(scope
            .error(
                access.target.span,
                Code::InvalidSecurityTarget,
                "Duplicate access block",
            )
            .with_related(source, *span));
    }
    let mut contract = Security::default();
    for member in &access.members {
        match &member.value {
            ast::AccessMember::Resource(resource) => {
                let valid = !resource.value.is_empty() &&
                    resource
                        .value
                        .split('/')
                        .all(|part| part.parse::<Name>().is_ok());
                if !valid ||
                    contract
                        .resource
                        .replace(resource.value.as_ref().into())
                        .is_some()
                {
                    return Err(scope.error(
                        resource.span,
                        Code::InvalidSecurityTarget,
                        "Invalid or duplicate external resource type",
                    ));
                }
            },
            ast::AccessMember::Permission(permission) => {
                let name = scope.name(&permission.name)?;
                let binding = external_binding(scope, permission)?;
                if contract.permissions.insert(name, binding).is_some() {
                    return Err(scope.error(
                        permission.name.span,
                        Code::InvalidSecurityTarget,
                        "Duplicate local permission",
                    ));
                }
            },
            ast::AccessMember::Field(_) => {},
        }
    }
    if contract.resource.is_none() &&
        contract
            .permissions
            .values()
            .any(|permission| permission.spicedb.is_some())
    {
        return Err(scope.error(
            access.target.span,
            Code::InvalidSecurityTarget,
            "A SpiceDB permission requires an external resource type",
        ));
    }
    written.insert(target.clone(), (scope.module.file, access.target.span));
    contracts.insert(target.into(), contract);
    Ok(())
}

fn external_binding(
    scope: &Scope<'_>,
    permission: &ast::Permission<'_>,
) -> CheckedResult<Binding> {
    let mut spicedb = None;
    let mut cedar = None;
    for binding in &permission.bindings {
        let repeated = match &binding.value {
            ast::Binding::SpiceDb(value) => {
                let name = value.value.parse().map_err(|_| {
                    scope.error(
                        value.span,
                        Code::InvalidSecurityTarget,
                        "Invalid SpiceDB permission identifier",
                    )
                })?;
                spicedb.replace(name).is_some()
            },
            ast::Binding::Cedar(value) => {
                if value.value.trim().is_empty() ||
                    value.value.chars().any(char::is_control)
                {
                    return Err(scope.error(
                        value.span,
                        Code::InvalidSecurityTarget,
                        "Invalid Cedar action reference",
                    ));
                }
                cedar.replace(value.value.as_ref().into()).is_some()
            },
        };
        if repeated {
            return Err(scope.error(
                binding.span,
                Code::InvalidSecurityTarget,
                "Duplicate external binding for this permission",
            ));
        }
    }
    Ok(Binding { spicedb, cedar })
}

fn has_field(facts: &Facts, field: &FieldRef) -> bool {
    declared_field(facts, field).is_some() ||
        matches!(facts.get(&field.owner), Some(crate::Declaration::View(view)) if view.outputs.get(field.name.as_str()).is_some())
}

fn guards(
    scope: &Scope<'_>,
    index: &Index<'_>,
    facts: &Facts,
    access: &ast::Access<'_>,
    contracts: &mut BTreeMap<Reference, Security>,
) -> CheckedResult<()> {
    let target = resource(scope, index, &access.target)?;
    for member in &access.members {
        if let ast::AccessMember::Field(guard) = &member.value {
            let [name] = guard.field.value.segments.as_slice() else {
                return Err(scope.error(
                    guard.field.span,
                    Code::InvalidSecurityTarget,
                    "Use a field name relative to the access target",
                ));
            };
            let field = FieldRef::new(target.clone(), scope.name(name)?);
            if !has_field(facts, &field) {
                return Err(scope.error(
                    guard.field.span,
                    Code::InvalidSecurityTarget,
                    "The protected field does not exist on this target",
                ));
            }
            apply_guard(scope, index, field, &guard.permission, contracts)?;
        }
    }
    Ok(())
}

fn apply_guard(
    scope: &Scope<'_>,
    index: &Index<'_>,
    field: FieldRef,
    path: &ast::Node<ast::Path<'_>>,
    contracts: &mut BTreeMap<Reference, Security>,
) -> CheckedResult<()> {
    let Some((name, prefix)) = path.value.segments.split_last() else {
        return Err(scope.error(
            path.span,
            Code::InvalidSecurityTarget,
            "Missing permission name",
        ));
    };
    if !prefix.is_empty() {
        let owner = resolve_symbol(
            scope,
            index,
            prefix,
            path.span,
            Code::InvalidSecurityTarget,
            protectable,
        )?;
        if owner != field.owner {
            return Err(scope.error(
                path.span,
                Code::InvalidSecurityTarget,
                "A field guard must name a permission on its owner",
            ));
        }
    }
    let name = scope.name(name)?;
    let owner = field.owner.clone();
    if !permission_exists(contracts, &owner, name.as_str()) {
        return Err(scope.error(
            path.span,
            Code::InvalidSecurityTarget,
            "Unknown permission on the protected resource",
        ));
    }
    contracts.entry(field.into()).or_default().requires.insert(
        PermissionRef {
            target: owner,
            name,
        },
    );
    Ok(())
}

fn permission_exists(
    contracts: &BTreeMap<Reference, Security>,
    owner: &SymbolRef,
    name: &str,
) -> bool {
    contracts
        .get(&owner.clone().into())
        .is_some_and(|contract| contract.permissions.contains_key(name))
}

fn annotations(
    scope: &Scope<'_>,
    index: &Index<'_>,
    record: &ast::Record<'_>,
    contracts: &mut BTreeMap<Reference, Security>,
    errors: &mut Vec<Diagnostic>,
) {
    let Some(owner) = scope.module.exports.get(record.name.value) else {
        return;
    };
    for member in &record.members {
        if let ast::Member::Field(field) = &member.value &&
            !field.annotations.is_empty() &&
            let Err(error) =
                annotate_field(scope, index, owner, field, contracts)
        {
            errors.push(error);
        }
    }
}

fn annotate_field(
    scope: &Scope<'_>,
    index: &Index<'_>,
    owner: &SymbolRef,
    field: &ast::Field<'_>,
    contracts: &mut BTreeMap<Reference, Security>,
) -> CheckedResult<()> {
    let reference = FieldRef::new(owner.clone(), scope.name(&field.name)?);
    for annotation in &field.annotations {
        match &annotation.value {
            ast::Annotation::Requires(path) => {
                apply_guard(scope, index, reference.clone(), path, contracts)?
            },
            ast::Annotation::Label(path) => {
                let label = resolve_symbol(
                    scope,
                    index,
                    &path.value.segments,
                    path.span,
                    Code::InvalidSecurityTarget,
                    |kind| kind == SymbolKind::IfcLabel,
                )?;
                let contract =
                    contracts.entry(reference.clone().into()).or_default();
                if contract.label.replace(label).is_some() {
                    return Err(scope.error(
                        annotation.span,
                        Code::InvalidSecurityTarget,
                        "A field can specify only one IFC label",
                    ));
                }
            },
        }
    }
    Ok(())
}

fn check_policy(
    scope: &Scope<'_>,
    index: &Index<'_>,
    facts: &Facts,
    environment: &Environment<'_>,
    policy: &ast::Policy<'_>,
    contracts: &mut BTreeMap<Reference, Security>,
) -> CheckedResult<()> {
    let (target, permission) =
        policy_target(scope, index, facts, &policy.target, contracts)?;
    let owner = match &target {
        Reference::Symbol(owner) => owner,
        Reference::Field(field) => &field.owner,
    };
    let action = match facts.get(owner) {
        Some(crate::Declaration::Action(action)) => Some(action),
        _ => None,
    };
    let checker = Checker {
        scope,
        facts,
        environment,
        subject: Some(owner),
        action,
        allow_result: false,
        allow_context: true,
    };
    let check = |expr| checker.require(expr, &Primitive::Bool.into());
    let when = policy.when.as_ref().map(check).transpose()?;
    let unless = policy.unless.as_ref().map(check).transpose()?;
    contracts
        .entry(target)
        .or_default()
        .policies
        .insert(Policy {
            kind: policy.kind.value,
            permission,
            when,
            unless,
        });
    Ok(())
}

fn policy_target(
    scope: &Scope<'_>,
    index: &Index<'_>,
    facts: &Facts,
    path: &ast::Node<ast::Path<'_>>,
    contracts: &BTreeMap<Reference, Security>,
) -> CheckedResult<(Reference, Option<Name>)> {
    let segments = &path.value.segments;
    if let Some(super::index::Target::Symbol(reference)) = scope.find(segments) &&
        index
            .items
            .get(reference)
            .is_some_and(|item| protectable(item.kind))
    {
        return Ok((reference.clone().into(), None));
    }
    if let Some((name, prefix)) = segments.split_last() &&
        !prefix.is_empty()
    {
        let owner = resolve_symbol(
            scope,
            index,
            prefix,
            path.span,
            Code::InvalidSecurityTarget,
            protectable,
        )?;
        let field = FieldRef::new(owner.clone(), scope.name(name)?);
        match (
            has_field(facts, &field),
            permission_exists(contracts, &owner, name.value),
        ) {
            (true, false) => return Ok((field.into(), None)),
            (false, true) => return Ok((owner.into(), Some(field.name))),
            (true, true) => {
                return Err(scope.error(
                    path.span,
                    Code::InvalidSecurityTarget,
                    "Policy target is both a field and a permission",
                ));
            },
            (false, false) => {},
        }
    }
    Err(scope.error(
        path.span,
        Code::InvalidSecurityTarget,
        format!("Unknown security target {}", spelling(segments)),
    ))
}

//! Orchestrate the semantic passes and commit only a fully checked project.

use std::collections::{BTreeMap, BTreeSet};

use super::contracts;
use super::diagnostic::{CheckedResult, Code, Diagnostic, Diagnostics};
use super::expression::{self, Checker};
use super::index::{Index, Scope, resolve_symbol};
use super::ir::{
    Checked, Dependency, DependencyKind, DependencyTarget, EffectPath,
    Environment, Project, Value,
};
use super::types::{
    self, Facts, compatible, declared_field, field_target, peel,
};
use crate::syntax::{self as ast, Declaration as Ast, SyntaxTree};
use crate::{
    Action, Declaration, Derivation, Effect, Expression, FieldKind, FieldRef,
    Idempotence, Invariant, Predicate, Primitive, QualifiedName, Reference,
    SymbolId, SymbolKind, SymbolRef, TypeRef, TypedExpression, View,
};

/// Resolves and checks a project against the caller's local environment.
///
/// One version of each ontology may be supplied. The returned modules and
/// metadata are normalized by logical keys, so source positions remain
/// exclusive to the diagnostics.
///
/// # Errors
///
/// Returns sorted semantic diagnostics. A failed pass does not expose its
/// partial project or assume the outcome of subsequent external validation.
///
/// ```
/// use galadril_ontology::semantic::{Environment, analyze_project};
/// use galadril_ontology::syntax::parse_source;
/// use galadril_ontology::{Declaration, ExternalTypeRegistry};
///
/// # fn main() -> Result<(), Box<dyn std::error::Error>> {
/// let source = parse_source(
///     "assets.gol",
///     r#"
///     ontology assets version "1.0.0" {
///         entity Machine { field spare: Ref<Part>; }
///         entity Part {}
///     }
/// "#,
/// )?;
/// let registry = ExternalTypeRegistry::default();
/// let project = analyze_project(&[source], &Environment::new(&registry))?;
/// let modules = project.modules();
/// let assets = modules.get(&"assets".parse()?).ok_or("missing module")?;
/// assert!(matches!(
///     assets.declarations().get("Machine"),
///     Some(Declaration::Entity(_))
/// ));
/// # Ok(())
/// # }
/// ```
pub fn analyze_project(
    sources: &[SyntaxTree<'_>],
    environment: &Environment<'_>,
) -> Result<Project, Diagnostics> {
    let mut errors = Vec::new();
    let index = Index::collect(sources, &mut errors);
    let scopes = index.scopes(environment, &mut errors);
    stop_on_errors(&mut errors)?;

    // Resolve every signature first. Checking bodies against a fixed snapshot
    // avoids declaration-order dependence even across mutually importing
    // modules.
    let mut facts = types::signatures(
        &index,
        &scopes,
        environment.max_list_length,
        &mut errors,
    );
    if let Some(target) = types::type_cycle(&facts) &&
        let Some(item) = index.items.get(target)
    {
        errors.push(Diagnostic::new(
            item.file,
            item.node.span,
            Code::Cycle,
            "Recursive value type or alias",
        ));
    }
    stop_on_errors(&mut errors)?;

    let mut project = Project::default();
    let mut bodies = BTreeMap::new();
    for (owner, item) in &index.items {
        let Some(scope) = scopes.get(owner.key().ontology()) else {
            continue;
        };
        let checker = Checker {
            scope,
            facts: &facts,
            environment,
            subject: None,
            action: None,
            allow_result: false,
            allow_context: false,
        };
        match lower_body(
            &checker,
            &index,
            owner,
            &item.node.value,
            &mut project,
        ) {
            Ok(Some(declaration)) => {
                bodies.insert(owner.clone(), declaration);
            },
            Ok(None) => {},
            Err(error) => errors.push(error),
        }
    }
    facts.extend(bodies);
    stop_on_errors(&mut errors)?;
    validate_derivations(&facts, &index, &project, &mut errors);

    contracts::validate(
        &index,
        &scopes,
        &facts,
        environment,
        &mut project,
        &mut errors,
    );
    stop_on_errors(&mut errors)?;

    assemble(&index, scopes, facts, &mut project)
        .map_err(|error| Diagnostics::new(vec![error]))?;
    Ok(project)
}

fn stop_on_errors(errors: &mut Vec<Diagnostic>) -> Result<(), Diagnostics> {
    if errors.is_empty() {
        Ok(())
    } else {
        Err(Diagnostics::new(std::mem::take(errors)))
    }
}

fn lower_body(
    checker: &Checker<'_, '_>,
    index: &Index<'_>,
    owner: &SymbolRef,
    declaration: &Ast<'_>,
    project: &mut Project,
) -> CheckedResult<Option<Declaration>> {
    let scope = checker.scope;
    Ok(Some(match declaration {
        Ast::Invariant(invariant) => {
            let subject = record_target(scope, index, &invariant.target)?;
            let checked = Checker {
                subject: Some(&subject),
                ..*checker
            }
            .require(&invariant.condition, &Primitive::Bool.into())?;
            let typed = store_expression(
                scope,
                owner,
                "condition",
                invariant.condition.span,
                checked,
                project,
            )?;
            Declaration::Invariant(Invariant::new(
                subject,
                Predicate::new(typed.expression),
            ))
        },
        Ast::Derive(derive) => Declaration::Derive(derivation(
            checker, index, owner, derive, project,
        )?),
        Ast::View(view) => {
            Declaration::View(view_body(checker, index, owner, view, project)?)
        },
        Ast::Action(action) => {
            Declaration::Action(action_body(checker, owner, action, project)?)
        },
        Ast::Label(label) => {
            let (label, transfer) = contracts::label(scope, label)?;
            project.labels.insert(owner.clone(), transfer);
            Declaration::IfcLabel(label)
        },
        _ => return Ok(None),
    }))
}

fn record_target(
    scope: &Scope<'_>,
    index: &Index<'_>,
    target: &ast::Node<ast::Path<'_>>,
) -> CheckedResult<SymbolRef> {
    resolve_symbol(
        scope,
        index,
        &target.value.segments,
        target.span,
        Code::UnresolvedSymbol,
        |kind| {
            matches!(
                kind,
                SymbolKind::Entity | SymbolKind::Relation | SymbolKind::Struct
            )
        },
    )
}

fn derivation(
    checker: &Checker<'_, '_>,
    index: &Index<'_>,
    owner: &SymbolRef,
    derive: &ast::Derivation<'_>,
    project: &mut Project,
) -> CheckedResult<Derivation> {
    let scope = checker.scope;
    let target = field_target(
        scope,
        index,
        checker.facts,
        &derive.target,
        Code::InvalidPropertyAccess,
    )?;
    let Some(FieldKind::Value(field_type)) =
        declared_field(checker.facts, &target).map(|field| &field.kind)
    else {
        return Err(scope.error(
            derive.target.span,
            Code::UnsupportedSemanticConstruct,
            "A derivation must target a declared value field",
        ));
    };
    let result = types::resolve_type(
        scope,
        index,
        &derive.ty,
        checker.environment.max_list_length,
    )?;
    if !compatible(checker.facts, field_type, &result) {
        return Err(scope.error(
            derive.ty.span,
            Code::TypeMismatch,
            "The derivation type must match its target field",
        ));
    }
    let checked = Checker {
        subject: Some(&target.owner),
        ..*checker
    }
    .require(&derive.value, &result)?;
    let typed = store_expression(
        scope,
        owner,
        "value",
        derive.value.span,
        checked,
        project,
    )?;
    Ok(Derivation::new(
        target,
        TypedExpression::new(result, typed.expression),
    ))
}

fn view_body(
    checker: &Checker<'_, '_>,
    index: &Index<'_>,
    owner: &SymbolRef,
    view: &ast::View<'_>,
    project: &mut Project,
) -> CheckedResult<View> {
    let scope = checker.scope;
    let subject = record_target(scope, index, &view.source)?;
    let checker = Checker {
        subject: Some(&subject),
        ..*checker
    };
    let mut compiled = View::default();
    link(
        project,
        owner,
        DependencyKind::Expression,
        DependencyTarget::Local(subject.clone().into()),
    );
    for projection in &view.projections {
        let checked = checker.check(&projection.value.expression, None)?;
        let name = match &projection.value.alias {
            Some(name) => scope.name(name)?,
            None => {
                projection_name(&checked.value).cloned().ok_or_else(|| {
                    scope.error(
                projection.span,
                Code::UnsupportedSemanticConstruct,
                "A computed projection requires an explicit output name",
            )
                })?
            },
        };
        let typed = store_expression(
            scope,
            owner,
            &format!("projection_{name}"),
            projection.span,
            checked,
            project,
        )?;
        types::insert(
            scope,
            &mut compiled.outputs,
            name,
            typed,
            projection.span,
        )?;
    }
    if let Some(filter) = &view.filter {
        let checked = checker.require(filter, &Primitive::Bool.into())?;
        let typed = store_expression(
            scope,
            owner,
            "filter",
            filter.span,
            checked,
            project,
        )?;
        compiled.filter = Some(Predicate::new(typed.expression));
    }
    Ok(compiled)
}

fn projection_name(value: &Value) -> Option<&crate::Name> {
    match value {
        Value::Field { path, .. } => path.last().map(|field| &field.name),
        Value::Role { role, .. } => Some(role),
        _ => None,
    }
}

fn action_body(
    checker: &Checker<'_, '_>,
    owner: &SymbolRef,
    action: &ast::Action<'_>,
    project: &mut Project,
) -> CheckedResult<Action> {
    let scope = checker.scope;
    let Some(Declaration::Action(signature)) = checker.facts.get(owner) else {
        return Err(scope.error(
            action.name.span,
            Code::InvalidActionContract,
            "Action signature is missing",
        ));
    };
    let checker = Checker {
        action: Some(signature),
        ..*checker
    };
    let mut compiled = signature.clone();
    let mut requires = BTreeSet::new();
    let mut ensures = BTreeSet::new();
    let mut paths = BTreeSet::new();
    let mut retry_specified = false;
    for clause in &action.clauses {
        match &clause.value {
            ast::ActionClause::Requires(node) => {
                requires
                    .insert(checker.require(node, &Primitive::Bool.into())?);
            },
            ast::ActionClause::Ensures(node) => {
                let postcondition = Checker {
                    allow_result: true,
                    ..checker
                };
                ensures.insert(
                    postcondition.require(node, &Primitive::Bool.into())?,
                );
            },
            ast::ActionClause::Effects(effects) => {
                for effect in effects {
                    let (summary, path) = action_effect(&checker, effect)?;
                    compiled.effects.insert(summary);
                    paths.insert(path);
                }
            },
            ast::ActionClause::Retry(retry) => {
                if retry_specified {
                    return Err(scope.error(
                        retry.span,
                        Code::InvalidActionContract,
                        "An action may declare only one retry contract",
                    ));
                }
                compiled.retry = check_retry(scope, signature, retry)?;
                retry_specified = true;
            },
        }
    }
    for (name, conditions, predicates) in [
        ("requires", requires, &mut compiled.requires),
        ("ensures", ensures, &mut compiled.ensures),
    ] {
        // Assign slot names after ordering the checked predicates, so
        // equivalent clause permutations preserve both the summaries
        // and body identities.
        for (index, checked) in conditions.into_iter().enumerate() {
            let typed = store_expression(
                scope,
                owner,
                &format!("{name}_{index}"),
                action.name.span,
                checked,
                project,
            )?;
            predicates.insert(Predicate::new(typed.expression));
        }
    }
    if !paths.is_empty() {
        project.effects.insert(owner.clone(), paths);
    }
    Ok(compiled)
}

fn check_retry(
    scope: &Scope<'_>,
    action: &Action,
    node: &ast::Node<ast::Retry<'_>>,
) -> CheckedResult<Idempotence> {
    Ok(match &node.value {
        ast::Retry::Idempotent => Idempotence::Idempotent,
        ast::Retry::NonIdempotent => Idempotence::NonIdempotent,
        ast::Retry::Deduplicated(name) => {
            if action.parameters.get(name.value).is_none() {
                return Err(scope.error(
                    name.span,
                    Code::InvalidActionContract,
                    "Unknown deduplication parameter",
                ));
            }
            Idempotence::Deduplicated {
                key_parameter: scope.name(name)?,
            }
        },
    })
}

fn action_effect(
    checker: &Checker<'_, '_>,
    node: &ast::Node<ast::Effect<'_>>,
) -> CheckedResult<(Effect, EffectPath)> {
    let (path, write) = match &node.value {
        ast::Effect::Read(path) => (path, false),
        ast::Effect::Write(path) => (path, true),
        ast::Effect::Emit(name) => {
            let name = contracts::qualified(checker.scope, name)?;
            return Ok((Effect::Emit(name.clone()), EffectPath::Emit(name)));
        },
    };
    let checked = checker.effect_path(path)?;
    let target = match (&checked.value, peel(checker.facts, &checked.ty)) {
        (Value::Field { path, .. }, _) => path
            .last()
            .filter(|field| {
                !write ||
                    matches!(
                        declared_field(checker.facts, field)
                            .map(|field| &field.kind),
                        Some(FieldKind::Value(_))
                    )
            })
            .cloned()
            .map(Reference::Field),
        (Value::Parameter(_), TypeRef::Entity(entity)) if !write => {
            Some(Reference::Symbol(entity.clone()))
        },
        _ => None,
    }
    .ok_or_else(|| {
        checker.scope.error(
            path.span,
            Code::InvalidActionContract,
            "An effect must read a resource or field, or write a value field",
        )
    })?;
    Ok(if write {
        (Effect::Write(target), EffectPath::Write(checked))
    } else {
        (Effect::Read(target), EffectPath::Read(checked))
    })
}

fn store_expression(
    scope: &Scope<'_>,
    owner: &SymbolRef,
    slot: &str,
    span: ast::Span,
    checked: Checked,
    project: &mut Project,
) -> CheckedResult<TypedExpression> {
    let key: QualifiedName =
        format!("{}.{}.{slot}", owner.key().ontology(), owner.key().name())
            .parse()
            .map_err(|_| {
                scope.error(
                    span,
                    Code::InvalidName,
                    "Invalid expression identity",
                )
            })?;
    let mut expression = Expression::new(key.clone());
    expression.dependencies = expression::dependencies(&checked);
    for dependency in &expression.dependencies {
        link(
            project,
            owner,
            DependencyKind::Expression,
            DependencyTarget::Local(dependency.clone()),
        );
    }
    let result = checked.ty.clone();
    project.expressions.insert(key, checked);
    Ok(TypedExpression::new(result, expression))
}

fn validate_derivations(
    facts: &Facts,
    index: &Index<'_>,
    project: &Project,
    errors: &mut Vec<Diagnostic>,
) {
    let mut owners = BTreeMap::new();
    let mut graph = BTreeMap::<&FieldRef, BTreeSet<&FieldRef>>::new();
    for (reference, declaration) in facts {
        let Declaration::Derive(derive) = declaration else {
            continue;
        };
        if let Some(previous) = owners.insert(&derive.target, reference) &&
            let Some(item) = index.items.get(reference) &&
            let Some(other) = index.items.get(previous)
        {
            errors.push(
                Diagnostic::new(
                    item.file,
                    item.node.span,
                    Code::DuplicateSymbol,
                    "Multiple derivations target the same field",
                )
                .with_related(other.file, other.node.span),
            );
        }
        graph.entry(&derive.target).or_default().extend(
            derive.value.expression.dependencies.iter().filter_map(
                |reference| match reference {
                    Reference::Field(field) => Some(field),
                    _ => None,
                },
            ),
        );
    }
    if let Some(field) = types::cycle(&graph) &&
        let Some(item) =
            owners.get(field).and_then(|owner| index.items.get(*owner))
    {
        errors.push(Diagnostic::new(
            item.file,
            item.node.span,
            Code::Cycle,
            "Recursive derived field dependency",
        ));
    }
    for (owner, effects) in &project.effects {
        let writes_derived = effects.iter().any(|effect| matches!(effect,
            EffectPath::Write(Checked { value: Value::Field { path, .. }, .. }) if path.last().is_some_and(|field| owners.contains_key(field))));
        if writes_derived && let Some(item) = index.items.get(owner) {
            errors.push(Diagnostic::new(
                item.file,
                item.node.span,
                Code::InvalidActionContract,
                "An action cannot write a derived field",
            ));
        }
    }
}

fn assemble(
    index: &Index<'_>,
    scopes: BTreeMap<crate::OntologyId, Scope<'_>>,
    facts: Facts,
    project: &mut Project,
) -> CheckedResult<()> {
    for (id, scope) in &scopes {
        project.modules.insert(id.clone(), scope.canonical()?);
        if !scope.external.is_empty() {
            project
                .external_imports
                .insert(id.clone(), scope.external.clone());
        }
    }
    security_dependencies(project);
    for (position, (owner, declaration)) in facts.into_iter().enumerate() {
        let Some(item) = index.items.get(&owner) else {
            continue;
        };
        let Some(module) = project.modules.get_mut(owner.key().ontology())
        else {
            return Err(Diagnostic::new(
                item.file,
                item.node.span,
                Code::UnresolvedSymbol,
                "Missing owning module",
            ));
        };
        let id = u32::try_from(position).map_err(|_| {
            Diagnostic::new(
                item.file,
                item.node.span,
                Code::UnsupportedSemanticConstruct,
                "Project exceeds the compilation ID range",
            )
        })?;
        module
            .declare(owner.key().name().clone(), declaration)
            .map_err(|error| {
                Diagnostic::new(
                    item.file,
                    item.node.span,
                    Code::DuplicateSymbol,
                    error.to_string(),
                )
            })?;
        project.symbols.insert(owner, SymbolId::from(id));
    }
    for module in project.modules.values() {
        for (name, declaration) in module.declarations().iter() {
            let reference = module.module().reference(name.clone());
            declaration_dependencies(
                &mut project.dependencies,
                &reference,
                declaration,
            );
        }
    }
    for (owner, effects) in &project.effects {
        for effect in effects {
            match effect {
                EffectPath::Emit(name) => {
                    project.dependencies.insert(Dependency {
                        owner: owner.clone(),
                        kind: DependencyKind::Expression,
                        target: DependencyTarget::HostEffect(name.clone()),
                    });
                },
                EffectPath::Read(path) | EffectPath::Write(path) => {
                    project.dependencies.extend(
                        expression::dependencies(path).into_iter().map(
                            |reference| Dependency {
                                owner: owner.clone(),
                                kind: DependencyKind::Expression,
                                target: DependencyTarget::Local(reference),
                            },
                        ),
                    );
                },
            }
        }
    }
    Ok(())
}

fn link(
    project: &mut Project,
    owner: &SymbolRef,
    kind: DependencyKind,
    target: DependencyTarget,
) {
    project.dependencies.insert(Dependency {
        owner: owner.clone(),
        kind,
        target,
    });
}

fn security_dependencies(project: &mut Project) {
    for (target, contract) in &project.security {
        let owner = match target {
            Reference::Symbol(owner) => owner,
            Reference::Field(field) => &field.owner,
        };
        if let Some(label) = &contract.label {
            project.dependencies.insert(Dependency {
                owner: owner.clone(),
                kind: DependencyKind::Security,
                target: DependencyTarget::Local(label.clone().into()),
            });
        }
        let permissions = contract
            .permissions
            .keys()
            .map(|name| super::ir::PermissionRef {
                target: owner.clone(),
                name: name.clone(),
            })
            .chain(contract.requires.iter().cloned());
        project.dependencies.extend(permissions.map(|permission| {
            Dependency {
                owner: owner.clone(),
                kind: DependencyKind::Security,
                target: DependencyTarget::Permission(permission),
            }
        }));
        for policy in &contract.policies {
            for condition in policy.when.iter().chain(policy.unless.iter()) {
                project.dependencies.extend(
                    expression::dependencies(condition).into_iter().map(
                        |reference| Dependency {
                            owner: owner.clone(),
                            kind: DependencyKind::Security,
                            target: DependencyTarget::Local(reference),
                        },
                    ),
                );
            }
        }
    }
}

fn declaration_dependencies(
    dependencies: &mut BTreeSet<Dependency>,
    owner: &SymbolRef,
    declaration: &Declaration,
) {
    let mut record = |kind, target| {
        dependencies.insert(Dependency {
            owner: owner.clone(),
            kind,
            target,
        });
    };
    match declaration {
        Declaration::Invariant(value) => record(
            DependencyKind::Expression,
            DependencyTarget::Local(value.subject.clone().into()),
        ),
        Declaration::Derive(value) => record(
            DependencyKind::Expression,
            DependencyTarget::Local(value.target.clone().into()),
        ),
        Declaration::Relation(relation) => {
            for (_, role) in relation.roles.iter() {
                record(
                    DependencyKind::BusinessReference,
                    DependencyTarget::Local(role.entity.clone().into()),
                );
            }
        },
        _ => {},
    }
    if let Some(fields) = types::named_fields(declaration) {
        for (_, field) in fields.iter() {
            match &field.kind {
                FieldKind::Key(ty) | FieldKind::Value(ty) => {
                    type_dependency(ty, &mut record)
                },
                FieldKind::OwnedAttribute { attribute, .. } => record(
                    DependencyKind::Type,
                    DependencyTarget::Local(attribute.clone().into()),
                ),
            }
        }
    }
    match declaration {
        Declaration::TypeAlias(alias) => {
            type_dependency(&alias.target, &mut record)
        },
        Declaration::Attribute(attribute) => {
            type_dependency(&attribute.value_type, &mut record)
        },
        Declaration::Derive(derive) => {
            type_dependency(&derive.value.result, &mut record)
        },
        Declaration::View(view) => {
            for (_, output) in view.outputs.iter() {
                type_dependency(&output.result, &mut record);
            }
        },
        Declaration::Action(action) => {
            for (_, parameter) in action.parameters.iter() {
                type_dependency(parameter, &mut record);
            }
            type_dependency(&action.result, &mut record);
            for effect in &action.effects {
                if let Effect::Read(target) | Effect::Write(target) = effect {
                    record(
                        DependencyKind::Expression,
                        DependencyTarget::Local(target.clone()),
                    );
                }
            }
        },
        _ => {},
    }
}

fn type_dependency(
    mut ty: &TypeRef,
    record: &mut impl FnMut(DependencyKind, DependencyTarget),
) {
    loop {
        match ty {
            TypeRef::Option(inner) | TypeRef::List { element: inner, .. } => {
                ty = inner
            },
            TypeRef::Entity(target) => {
                return record(
                    DependencyKind::BusinessReference,
                    DependencyTarget::Local(target.clone().into()),
                );
            },
            TypeRef::Named(target) => {
                return record(
                    DependencyKind::Type,
                    DependencyTarget::Local(target.clone().into()),
                );
            },
            TypeRef::External(target) => {
                return record(
                    DependencyKind::Type,
                    DependencyTarget::External(target.clone()),
                );
            },
            TypeRef::Primitive(_) => return,
        }
    }
}

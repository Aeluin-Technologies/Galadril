//! Propagate IFC requirements over declarations and known control inputs.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use super::compiler::{Issue, IssueKind};
use super::contract::{Operation, Route, owner};
use super::environment::SecurityEnvironment;
use super::flow::Flow;
use crate::semantic::{
    DependencyKind, DependencyTarget, EffectPath, Project, Value,
    expression_dependencies,
};
use crate::{
    Declaration, Effect, FieldKind, FieldRef, Reference, SymbolRef, TypeRef,
};

pub(super) type Edges = BTreeMap<Route, BTreeSet<Route>>;

pub(super) struct Propagation {
    pub flows: BTreeMap<Reference, Flow>,
    pub prerequisites: Edges,
    pub derived: BTreeSet<FieldRef>,
}

pub(super) fn declaration<'a>(
    project: &'a Project,
    symbol: &SymbolRef,
) -> Option<&'a Declaration> {
    project
        .modules()
        .get(symbol.key().ontology())?
        .resolve_local(symbol)
}

pub(super) fn propagate(
    project: &Project,
    environment: &SecurityEnvironment,
) -> Result<Propagation, Issue> {
    let mut flows = BTreeMap::new();
    let mut derived = BTreeSet::new();
    for reference in project.symbols().keys() {
        let Some(declaration) = declaration(project, reference) else {
            return Err(Issue::new(
                IssueKind::InvalidTarget,
                Some(reference.clone().into()),
                "Missing canonical declaration",
            ));
        };
        if let Declaration::Derive(derive) = declaration {
            derived.insert(derive.target.clone());
        }
        let fields: Vec<_> = match declaration {
            Declaration::Entity(entity) => {
                entity.fields.iter().map(|(name, _)| name).collect()
            },
            Declaration::Relation(relation) => {
                relation.fields.iter().map(|(name, _)| name).collect()
            },
            Declaration::Struct(record) => {
                record.fields.iter().map(|(name, _)| name).collect()
            },
            Declaration::View(view) => {
                view.outputs.iter().map(|(name, _)| name).collect()
            },
            Declaration::Action(_) |
            Declaration::Enum(_) |
            Declaration::TypeAlias(_) |
            Declaration::Attribute(_) => Vec::new(),
            _ => continue,
        };
        let baseline = environment
            .module_floors
            .get(reference.key().ontology())
            .map_or_else(
                || environment.baseline.clone(),
                |floor| environment.baseline.join(floor),
            );
        flows.insert(reference.clone().into(), baseline.clone());
        for name in fields {
            flows.insert(
                FieldRef::new(reference.clone(), name.clone()).into(),
                baseline.clone(),
            );
        }
    }

    for (target, security) in project.security() {
        if let Some(label) = &security.label {
            let Some(Declaration::IfcLabel(definition)) =
                declaration(project, label)
            else {
                return Err(Issue::new(
                    IssueKind::InvalidFlow,
                    Some(target.clone()),
                    "Unknown IFC label",
                ));
            };
            if definition.integrity.is_some() {
                return Err(Issue::new(
                    IssueKind::InvalidFlow,
                    Some(target.clone()),
                    "Integrity labels require a defined integrity policy",
                ));
            }
            let Some(floor) = flows.get_mut(target) else {
                return Err(Issue::new(
                    IssueKind::InvalidTarget,
                    Some(target.clone()),
                    "Missing protected target",
                ));
            };
            let transfer = project.labels().get(label);
            let mut value = Flow::new(definition.classification);
            value.compartments.clone_from(&definition.compartments);
            value.destinations =
                transfer.and_then(|data| data.destinations.clone());
            value.purposes = transfer.and_then(|data| data.purposes.clone());
            *floor = floor.join(&value);
        }
    }

    for (target, floor) in &environment.target_floors {
        let Some(current) = flows.get_mut(target) else {
            return Err(Issue::new(
                IssueKind::InvalidTarget,
                Some(target.clone()),
                "The IFC floor names an unknown target",
            ));
        };
        *current = current.join(floor);
    }

    let mut dependents = BTreeMap::<Reference, BTreeSet<Reference>>::new();
    let mut prerequisites = Edges::new();
    for reference in flows.keys() {
        if let Reference::Field(field) = reference {
            edge(
                &mut dependents,
                field.owner.clone().into(),
                reference.clone(),
            );
            if matches!(
                declaration(project, &field.owner),
                Some(Declaration::View(_))
            ) {
                // A filter can leak through the existence of any projected
                // field, so every field inherits the view-wide inputs.
                requires(
                    &mut prerequisites,
                    reference.clone(),
                    Operation::Read,
                    field.owner.clone().into(),
                    Operation::Read,
                );
            }
        }
    }

    for dependency in project.dependencies() {
        if dependency.kind != DependencyKind::Expression {
            continue;
        }
        let DependencyTarget::Local(input) = &dependency.target else {
            continue;
        };
        if !flows.contains_key(input) {
            continue;
        }
        match declaration(project, &dependency.owner) {
            Some(Declaration::Derive(derive)) => {
                let target = Reference::Field(derive.target.clone());
                if input != &target {
                    edge(&mut dependents, input.clone(), target.clone());
                    requires(
                        &mut prerequisites,
                        target,
                        Operation::Read,
                        input.clone(),
                        Operation::Read,
                    );
                }
            },
            Some(Declaration::View(_)) => {
                let target = dependency.owner.clone().into();
                edge(&mut dependents, input.clone(), target);
                requires(
                    &mut prerequisites,
                    dependency.owner.clone().into(),
                    Operation::Read,
                    input.clone(),
                    Operation::Read,
                );
            },
            Some(Declaration::Action(_)) => {},
            _ => {},
        }
    }

    for reference in project.symbols().keys() {
        if let Some(Declaration::Action(action)) =
            declaration(project, reference)
        {
            // A write address is not an observation of its old value.
            // Conditions and intermediate reference hops remain real inputs.
            let mut observations: BTreeSet<_> = action
                .requires
                .iter()
                .chain(&action.ensures)
                .flat_map(|predicate| {
                    predicate.expression.dependencies.iter().cloned()
                })
                .collect();
            for path in project.effects().get(reference).into_iter().flatten()
            {
                match path {
                    EffectPath::Read(value) => {
                        observations.extend(expression_dependencies(value))
                    },
                    EffectPath::Write(value) => {
                        let mut inputs = expression_dependencies(value);
                        if let Value::Field { path, .. } = &value.value &&
                            let Some(last) = path.last()
                        {
                            inputs.remove(&Reference::Field(last.clone()));
                        }
                        observations.extend(inputs);
                    },
                    EffectPath::Emit(_) => {},
                }
            }
            for effect in &action.effects {
                if let Effect::Read(target) = effect {
                    observations.insert(target.clone());
                    if let Reference::Symbol(owner) = target &&
                        let Some(Declaration::Entity(entity)) =
                            declaration(project, owner)
                    {
                        observations.extend(entity.fields.iter().map(
                            |(name, _)| {
                                Reference::Field(FieldRef::new(
                                    owner.clone(),
                                    name.clone(),
                                ))
                            },
                        ));
                    }
                }
                if let Effect::Write(target) = effect {
                    requires(
                        &mut prerequisites,
                        reference.clone().into(),
                        Operation::Execute,
                        target.clone(),
                        Operation::Write,
                    );
                }
            }
            for input in observations {
                edge(&mut dependents, input.clone(), reference.clone().into());
                requires(
                    &mut prerequisites,
                    reference.clone().into(),
                    Operation::Execute,
                    input,
                    Operation::Read,
                );
            }
        }
    }

    // Each update raises confidentiality or narrows a finite release set.
    // Scheduling only affected nodes handles dependency cycles without
    // repeatedly scanning unrelated ontologies.
    let mut pending: VecDeque<_> = flows.keys().cloned().collect();
    let mut queued: BTreeSet<_> = flows.keys().cloned().collect();
    while let Some(source) = pending.pop_front() {
        queued.remove(&source);
        let Some(value) = flows.get(&source).cloned() else {
            continue;
        };
        for target in dependents.get(&source).into_iter().flatten() {
            if let Some(previous) = flows.get_mut(target) &&
                !previous.dominates(&value)
            {
                *previous = previous.join(&value);
                if queued.insert(target.clone()) {
                    pending.push_back(target.clone());
                }
            }
        }
    }
    Ok(Propagation {
        flows,
        prerequisites,
        derived,
    })
}

/// A whole structured value could contain a member with its own label or
/// guard. The current decision profile handles named fields, so it must
/// reject aggregate release until child-level enforcement is available.
pub(super) fn structured_value(project: &Project, field: &FieldRef) -> bool {
    let ty = match declaration(project, &field.owner) {
        Some(Declaration::View(view)) => view
            .outputs
            .get(field.name.as_str())
            .map(|output| &output.result),
        Some(declaration) => {
            let fields = match declaration {
                Declaration::Entity(entity) => &entity.fields,
                Declaration::Relation(relation) => &relation.fields,
                Declaration::Struct(record) => &record.fields,
                _ => return false,
            };
            fields.get(field.name.as_str()).and_then(|field| {
                match &field.kind {
                    FieldKind::Value(ty) | FieldKind::Key(ty) => Some(ty),
                    FieldKind::OwnedAttribute { attribute, .. } => {
                        match self::declaration(project, attribute) {
                            Some(Declaration::Attribute(attribute)) => {
                                Some(&attribute.value_type)
                            },
                            _ => None,
                        }
                    },
                }
            })
        },
        None => None,
    };
    let Some(mut ty) = ty else { return false };
    loop {
        match ty {
            TypeRef::Option(inner) | TypeRef::List { element: inner, .. } => {
                ty = inner
            },
            TypeRef::Named(symbol) => match declaration(project, symbol) {
                Some(Declaration::TypeAlias(alias)) => ty = &alias.target,
                Some(Declaration::Struct(_)) => return true,
                _ => return false,
            },
            _ => return false,
        }
    }
}

fn edge(
    edges: &mut BTreeMap<Reference, BTreeSet<Reference>>,
    input: Reference,
    output: Reference,
) {
    edges.entry(input).or_default().insert(output);
}

fn requires(
    edges: &mut Edges,
    output: Reference,
    operation: Operation,
    input: Reference,
    check: Operation,
) {
    edges
        .entry(Route::new(output, operation))
        .or_default()
        .insert(Route::new(input, check));
}

/// Converts direct dependency edges to mandatory requests for concrete
/// instances. A transfer carries both read and transfer obligations for
/// dependency values, including values that affect a filter.
pub(super) fn closure(route: &Route, edges: &Edges) -> BTreeSet<Route> {
    let initial = if matches!(
        route.operation,
        Operation::Transfer |
            Operation::Describe |
            Operation::DescribeTransfer
    ) {
        Route::new(route.target.clone(), Operation::Read)
    } else {
        route.clone()
    };
    let mut visited = BTreeSet::from([initial.clone()]);
    let mut required = BTreeSet::new();
    let mut pending = vec![initial];
    while let Some(source) = pending.pop() {
        for target in edges.get(&source).into_iter().flatten() {
            // A repeated symbolic route can still denote another instance.
            // Keep it as a prerequisite while bounding expansion by route.
            required.insert(target.clone());
            if matches!(
                route.operation,
                Operation::Transfer | Operation::DescribeTransfer
            ) && target.operation == Operation::Read
            {
                required.insert(Route::new(
                    target.target.clone(),
                    Operation::Transfer,
                ));
            }
            if visited.insert(target.clone()) {
                pending.push(target.clone());
            }
        }
    }
    required
}

pub(super) fn inherited(route: &Route) -> Option<Route> {
    if matches!(route.target, Reference::Field(_)) {
        Some(Route::new(owner(&route.target).clone(), route.operation))
    } else {
        None
    }
}

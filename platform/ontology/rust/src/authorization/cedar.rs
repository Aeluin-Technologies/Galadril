//! Generate approved projections and validate with the official Cedar SDK.

use std::collections::{BTreeMap, BTreeSet};

use cedar_policy::{
    EntityId, EntityTypeName, EntityUid, LinkError, PolicySet, Schema,
    ValidationMode, ValidationWarning, Validator,
};

use super::compiler::{Issue, IssueKind};
use super::environment::{CedarInput, CedarResource};
use super::propagation::declaration;
use crate::semantic::{Checked, Literal, Policy, Project, Value};
use crate::syntax::{BinaryOp, PolicyKind, UnaryOp};
use crate::{
    Declaration, Field, FieldKind, Name, Primitive, Reference, SymbolRef,
    TypeRef,
};

pub(super) struct CompiledCedar {
    pub schema: Schema,
    pub policies: PolicySet,
    pub validator: Validator,
    #[cfg(feature = "archive")]
    pub fragments: Vec<Box<str>>,
}

pub(super) fn compile(
    project: &Project,
    input: &CedarInput,
) -> Result<CompiledCedar, Issue> {
    let generated = generate(project, input)?;
    let fragments = std::iter::once(generated.as_str())
        .chain(input.schemas.iter().map(Box::as_ref))
        .map(|source| {
            Schema::from_cedarschema_str(source)
                .map(|(schema, _warnings)| schema)
                .map_err(|error| {
                    Issue::new(
                        IssueKind::InvalidCedarSchema,
                        None,
                        error.to_string(),
                    )
                })
        })
        .collect::<Result<Vec<_>, _>>()?;
    let schema = Schema::schema_link_complete(fragments).map_err(|error| {
        let kind = if matches!(error, LinkError::IncompleteSchema(_)) {
            IssueKind::UnlinkedCedarSchema
        } else {
            IssueKind::InvalidCedarSchema
        };
        Issue::new(kind, None, error.to_string())
    })?;
    let validator = Validator::new(schema.clone());
    let policies = policies(&validator, &input.policies, None)?;
    Ok(CompiledCedar {
        schema,
        policies,
        validator,
        #[cfg(feature = "archive")]
        fragments: std::iter::once(generated.into_boxed_str())
            .chain(input.schemas.iter().cloned())
            .collect(),
    })
}

fn generate(project: &Project, input: &CedarInput) -> Result<String, Issue> {
    let mut namespaces = BTreeMap::<String, Vec<String>>::new();
    namespaces
        .entry(input.principal.namespace())
        .or_default()
        .push(format!("external entity {};", input.principal.basename()));
    let context = record(
        input.context.iter().map(|(name, kind)| (name, Some(*kind))),
        None,
    )?;
    let mut types = BTreeSet::new();
    let mut actions = BTreeSet::new();
    for (owner, resource) in &input.resources {
        let target = Reference::Symbol(owner.clone());
        let definition = declaration(project, owner)
            .filter(|declaration| {
                matches!(
                    declaration,
                    Declaration::Entity(_) |
                        Declaration::Struct(_) |
                        Declaration::Relation(_) |
                        Declaration::View(_) |
                        Declaration::Action(_) |
                        Declaration::Enum(_) |
                        Declaration::TypeAlias(_) |
                        Declaration::Attribute(_)
                )
            })
            .ok_or_else(|| {
                Issue::new(
                    IssueKind::InvalidTarget,
                    Some(target.clone()),
                    "Unknown Cedar resource projection",
                )
            })?;
        if resource.entity_type == input.principal ||
            !types.insert(&resource.entity_type)
        {
            return Err(Issue::new(
                IssueKind::InvalidCedarSchema,
                Some(target),
                "Each resource needs its own Cedar type, distinct from the IAM principal",
            ));
        }
        let attrs = record(
            resource
                .fields
                .iter()
                .map(|name| (name, projected_type(project, definition, name))),
            Some(&target),
        )?;
        namespaces
            .entry(resource.entity_type.namespace())
            .or_default()
            .push(format!(
                "entity {} {attrs};",
                resource.entity_type.basename()
            ));
        for action in &resource.actions {
            if action.type_name().basename() != "Action" ||
                !actions.insert(action)
            {
                return Err(Issue::new(
                    IssueKind::InvalidCedarSchema,
                    Some(target),
                    "A Cedar action must belong to exactly one approved resource",
                ));
            }
            namespaces.entry(action.type_name().namespace()).or_default().push(format!(
                "action \"{}\" appliesTo {{ principal: [{}], resource: [{}], context: {context} }};",
                action.id().escaped(), input.principal, resource.entity_type,
            ));
        }
    }
    let mut source = String::new();
    for (namespace, statements) in namespaces {
        if namespace.is_empty() {
            source.push_str(&statements.join("\n"));
            source.push('\n');
        } else {
            source.push_str(&format!(
                "namespace {namespace} {{\n{}\n}}\n",
                statements.join("\n")
            ));
        }
    }
    Ok(source)
}

fn record<'a>(
    entries: impl Iterator<Item = (&'a Name, Option<Primitive>)>,
    target: Option<&Reference>,
) -> Result<String, Issue> {
    let mut attributes = Vec::new();
    for (name, ty) in entries {
        let cedar = match ty {
            Some(Primitive::Bool) => "Bool",
            Some(Primitive::String) => "String",
            Some(Primitive::I64) => "Long",
            _ => {
                return Err(Issue::new(
                    IssueKind::UnsupportedCedarType,
                    target.cloned(),
                    format!(
                        "Projection {name} must be a known bool, String or i64 value"
                    ),
                ));
            },
        };
        attributes.push(format!("\"{name}\": {cedar}"));
    }
    Ok(format!("{{ {} }}", attributes.join(", ")))
}

fn projected_type(
    project: &Project,
    definition: &Declaration,
    name: &Name,
) -> Option<Primitive> {
    let field_type = |field: &Field| match &field.kind {
        FieldKind::Value(ty) | FieldKind::Key(ty) => primitive(project, ty),
        FieldKind::OwnedAttribute { .. } => None,
    };
    match definition {
        Declaration::Entity(entity) => {
            entity.fields.get(name.as_str()).and_then(field_type)
        },
        Declaration::Relation(relation) => {
            relation.fields.get(name.as_str()).and_then(field_type)
        },
        Declaration::Struct(record) => {
            record.fields.get(name.as_str()).and_then(field_type)
        },
        Declaration::View(view) => view
            .outputs
            .get(name.as_str())
            .and_then(|value| primitive(project, &value.result)),
        _ => None,
    }
}

fn primitive<'a>(
    project: &'a Project,
    mut ty: &'a TypeRef,
) -> Option<Primitive> {
    while let TypeRef::Named(reference) = ty {
        let Declaration::TypeAlias(alias) = declaration(project, reference)?
        else {
            return None;
        };
        ty = &alias.target;
    }
    match ty {
        TypeRef::Primitive(ty) => Some(*ty),
        _ => None,
    }
}

fn policies(
    validator: &Validator,
    source: &str,
    target: Option<&Reference>,
) -> Result<PolicySet, Issue> {
    let policy_set: PolicySet =
        source.parse().map_err(|error: cedar_policy::ParseErrors| {
            Issue::new(
                IssueKind::InvalidCedarPolicy,
                target.cloned(),
                error.to_string(),
            )
        })?;
    let validation = validator.validate(&policy_set, ValidationMode::Strict);
    if let Some(error) = validation.validation_errors().next() {
        return Err(Issue::new(
            IssueKind::InvalidCedarPolicy,
            target.cloned(),
            error.to_string(),
        ));
    }
    // Cedar can report an inapplicable action/resource combination as a
    // warning. Publishing such a mandatory policy could drop a restriction.
    if let Some(warning) = validation.validation_warnings().find(|warning| {
        matches!(warning, ValidationWarning::InvalidActionApplication(_))
    }) {
        return Err(Issue::new(
            IssueKind::InvalidCedarPolicy,
            target.cloned(),
            warning.to_string(),
        ));
    }
    Ok(policy_set)
}

pub(super) fn action(
    target: &SymbolRef,
    resource: &CedarResource,
    schema: &Schema,
    principal: &EntityTypeName,
    spelling: &str,
) -> Result<EntityUid, Issue> {
    let issue = || {
        Issue::new(
            IssueKind::InvalidCedarSchema,
            Some(target.clone().into()),
            "The bound Cedar action is incompatible with the approved principal and resource",
        )
    };
    let action: EntityUid = spelling.parse().map_err(|_| issue())?;
    if !resource.actions.contains(&action) ||
        !schema
            .actions_for_principal_and_resource(
                principal,
                &resource.entity_type,
            )
            .any(|known| known == &action)
    {
        return Err(issue());
    }
    Ok(action)
}

pub(super) fn groups(
    project: &Project,
    input: &CedarInput,
    cedar: &CompiledCedar,
    target: &Reference,
    name: &Name,
    action: &EntityUid,
    resource: &CedarResource,
) -> Result<Vec<PolicySet>, Issue> {
    let owner = super::contract::owner(target);
    let root = Reference::Symbol(owner.clone());
    let targets: Vec<_> = if &root == target {
        vec![target]
    } else {
        vec![&root, target]
    };
    let mut groups = Vec::new();
    for target in targets {
        let Some(security) = project.security().get(target) else {
            continue;
        };
        // An allow at one scope cannot override a constraint at another.
        // Keep each target and optional local permission in its own policy
        // set.
        for selector in [None, Some(name)] {
            let rules: Vec<_> = security
                .policies
                .iter()
                .filter(|policy| policy.permission.as_ref() == selector)
                .collect();
            if rules.is_empty() {
                continue;
            }
            let mut rendered = String::new();
            let scope = format!(
                "(principal, action == {action}, resource is {})",
                resource.entity_type
            );
            if rules.iter().all(|policy| policy.kind == PolicyKind::Forbid) {
                // A forbid-only layer is a restriction. This local baseline
                // can never substitute for the independently checked bundle.
                rendered.push_str(&format!("permit{scope};\n"));
            }
            for rule in rules {
                render_policy(
                    rule,
                    owner,
                    input,
                    resource,
                    &scope,
                    &mut rendered,
                )
                .map_err(|message| {
                    Issue::new(
                        IssueKind::UnsupportedCedarType,
                        Some(target.clone()),
                        message,
                    )
                })?;
            }
            groups.push(policies(&cedar.validator, &rendered, Some(target))?);
        }
    }
    Ok(groups)
}

fn render_policy(
    policy: &Policy,
    owner: &SymbolRef,
    input: &CedarInput,
    resource: &CedarResource,
    scope: &str,
    output: &mut String,
) -> Result<(), &'static str> {
    let mut budget = 256usize;
    let effect = match policy.kind {
        PolicyKind::Permit => "permit",
        PolicyKind::Forbid => "forbid",
    };
    output.push_str(effect);
    output.push_str(scope);
    for (keyword, condition) in
        [("when", &policy.when), ("unless", &policy.unless)]
    {
        if let Some(condition) = condition {
            output.push_str(keyword);
            output.push_str(" { ");
            render(condition, owner, input, resource, output, &mut budget)?;
            output.push_str(" }");
        }
    }
    output.push_str(";\n");
    Ok(())
}

fn render(
    node: &Checked,
    owner: &SymbolRef,
    input: &CedarInput,
    resource: &CedarResource,
    output: &mut String,
    budget: &mut usize,
) -> Result<(), &'static str> {
    // Pest and the semantic IR flatten long operator chains. Bound generated
    // Cedar expressions before introducing nested syntax for the SDK parser.
    *budget = budget
        .checked_sub(1)
        .ok_or("GOL policy exceeds the Cedar lowering budget")?;
    match &node.value {
        Value::Literal(literal) => render_literal(literal, output)?,
        Value::Context(name)
            if input
                .context
                .get(name)
                .is_some_and(|kind| node.ty == (*kind).into()) =>
        {
            output.push_str(&format!("context[\"{name}\"]"));
        },
        Value::Field { base: None, path } => {
            let [field] = path.as_slice() else {
                return Err(
                    "Cedar policies require an explicitly projected, direct resource field",
                );
            };
            if &field.owner != owner || !resource.fields.contains(&field.name)
            {
                return Err(
                    "Policy reads a field outside its approved Cedar projection",
                );
            }
            output.push_str(&format!("resource[\"{}\"]", field.name));
        },
        Value::Unary(operators, operand) => {
            *budget = budget
                .checked_sub(operators.len())
                .ok_or("GOL policy exceeds the Cedar lowering budget")?;
            for operator in operators {
                if *operator != UnaryOp::Not {
                    return Err(
                        "Unspecified GOL numeric operations cannot be lowered to Cedar",
                    );
                }
                output.push_str("!(");
            }
            render(operand, owner, input, resource, output, budget)?;
            for _ in operators {
                output.push(')');
            }
        },
        Value::Binary(base, terms) => {
            output.push('(');
            render(base, owner, input, resource, output, budget)?;
            for (operator, operand) in terms {
                output.push_str(match operator {
                    BinaryOp::And => " && ", BinaryOp::Or => " || ",
                    BinaryOp::Equal => " == ", BinaryOp::NotEqual => " != ",
                    BinaryOp::Less => " < ", BinaryOp::LessEqual => " <= ",
                    BinaryOp::Greater => " > ", BinaryOp::GreaterEqual => " >= ",
                    _ => return Err("Unspecified GOL numeric operations cannot be lowered to Cedar"),
                });
                render(operand, owner, input, resource, output, budget)?;
            }
            output.push(')');
        },
        _ => {
            return Err(
                "Policy uses a value outside the supported Cedar projection",
            );
        },
    }
    Ok(())
}

fn render_literal(
    value: &Literal,
    output: &mut String,
) -> Result<(), &'static str> {
    match value {
        Literal::Bool(value) => {
            output.push_str(if *value { "true" } else { "false" })
        },
        Literal::String(value) => {
            output.push('"');
            output.push_str(&EntityId::new(value).escaped());
            output.push('"');
        },
        Literal::Integer(value) => output.push_str(
            &i64::try_from(*value)
                .map_err(|_| "Literal does not fit Cedar Long")?
                .to_string(),
        ),
        Literal::NegativeInteger(value) => {
            let signed = i128::try_from(*value)
                .map_err(|_| "Literal does not fit Cedar Long")?;
            output.push_str(
                &i64::try_from(-signed)
                    .map_err(|_| "Literal does not fit Cedar Long")?
                    .to_string(),
            );
        },
        _ => return Err("Literal has no lossless Cedar representation"),
    }
    Ok(())
}

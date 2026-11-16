//! Explicit metadata permissions and registered temporal encoding.

use std::collections::BTreeSet;
use std::num::{NonZeroU32, NonZeroU64};

use base::TestError;
use galadril_ontology::authorization::{
    CedarResource, Operation, RelationshipResource, Route, SecurityEnvironment,
};
use galadril_ontology::export::{
    ExternalMapping, ExternalMappings, MappingPrecision, Projection, Selection,
};
use galadril_ontology::semantic::{Environment, Project, analyze_project};
use galadril_ontology::syntax::parse_source;
use galadril_ontology::{
    AdapterContract, Declaration, ExternalTypeDescriptor, ExternalTypeId,
    ExternalTypeRegistry, Primitive, Reference, Version, VersionedName,
};
use serde_json::json;
use sha2::{Digest, Sha256};

pub(crate) use crate::base;

pub fn instant() -> Result<ExternalTypeId, TestError> {
    Ok(ExternalTypeId::new(
        "time.api.Instant".parse()?,
        Version::new(1, 0, 0),
    ))
}

pub fn project() -> Result<Project, TestError> {
    project_with_order(false)
}

pub fn project_with_order(reverse: bool) -> Result<Project, TestError> {
    let mut types = ExternalTypeRegistry::default();
    let id = instant()?;
    types.register(
        id.clone(),
        ExternalTypeDescriptor::new(
            AdapterContract::new(
                VersionedName::new(
                    "adapters.time".parse()?,
                    Version::new(1, 0, 0),
                ),
                "^1".parse()?,
            ),
            VersionedName::new(
                "encoding.rfc3339".parse()?,
                Version::new(1, 0, 0),
            ),
            NonZeroU64::new(128).ok_or("invalid bound")?,
            NonZeroU32::MIN,
        ),
    )?;
    let mut env = Environment::new(&types);
    for (name, ty) in [
        ("mfa", Primitive::Bool),
        ("hour", Primitive::I64),
        ("region", Primitive::String),
    ] {
        env.context.insert(name.parse()?, ty)?;
    }
    let mut source = parse_source(
        "contracts.gol",
        include_str!("../fixtures/export_types.gol"),
    )?;
    if reverse {
        for module in &mut source.modules {
            module.value.declarations.reverse();
        }
    }
    Ok(analyze_project(&[source], &env)?)
}

pub fn environment(
    project: &Project,
) -> Result<SecurityEnvironment, TestError> {
    let base = base::project(0, false)?;
    let mut config = base::environment(&base)?;
    config.routes.clear();
    let cedar = config.cedar.as_mut().ok_or("missing Cedar")?;
    cedar.policies = r#"
        permit(principal, action, resource)
            when { context.hour >= 8 && context.hour < 18 }
            unless { context.region == "blocked" };
    "#
    .into();
    cedar.revision = galadril_ontology::authorization::AuthorityRevision::new(
        VersionedName::new(
            "fixture.export_cedar".parse()?,
            Version::new(1, 0, 0),
        ),
        Sha256::digest(cedar.policies.as_bytes()).into(),
    );
    cedar.resources.clear();
    let rebac = config.relationship.as_mut().ok_or("missing ReBAC")?;
    rebac.resources.clear();
    for symbol in project.symbols().keys() {
        let Some(definition) = project
            .modules()
            .get(symbol.key().ontology())
            .and_then(|module| module.resolve_local(symbol))
        else {
            return Err("missing definition".into());
        };
        let (name, action, extra, fields) = match definition {
            Declaration::Entity(entity) => (
                "Profile",
                "profile",
                Some(("read_body", "secret")),
                Some(&entity.fields),
            ),
            Declaration::Struct(record) => (
                "Details",
                "details",
                Some(("read_body", "note")),
                Some(&record.fields),
            ),
            Declaration::Enum(_) => ("Status", "status", None, None),
            Declaration::TypeAlias(_)
                if symbol.key().name().as_str() == "OptionalName" =>
            {
                ("OptionalName", "optionalName", None, None)
            },
            Declaration::TypeAlias(_) => {
                ("Identifier", "identifier", None, None)
            },
            Declaration::Action(_) => ("Validate", "validate", None, None),
            _ => continue,
        };
        let mut resource = CedarResource::new(
            format!("Domain::{}", symbol.key().name()).parse()?,
        );
        resource
            .actions
            .insert(format!("Domain::Action::\"{action}\"").parse()?);
        if let Some((_, action)) = extra {
            resource
                .actions
                .insert(format!("Domain::Action::\"{action}\"").parse()?);
        }
        if name == "Profile" {
            resource
                .actions
                .insert(r#"Domain::Action::"exportProfile""#.parse()?);
            resource
                .actions
                .insert(r#"Domain::Action::"writeProfile""#.parse()?);
        }
        cedar.resources.insert(symbol.clone(), resource);
        rebac.resources.insert(
            symbol.clone(),
            RelationshipResource::new(
                "company_record",
                [
                    "view".parse()?,
                    "view_body".parse()?,
                    "export".parse()?,
                    "edit".parse()?,
                ]
                .into(),
            ),
        );
        config.routes.insert(
            Route::new(symbol.clone(), Operation::Describe),
            ["read_meta".parse()?].into(),
        );
        if name == "Profile" {
            for operation in [Operation::DescribeTransfer, Operation::Write] {
                let permission = if operation == Operation::Write {
                    "write_profile"
                } else {
                    "export_meta"
                };
                config.routes.insert(
                    Route::new(symbol.clone(), operation),
                    [permission.parse()?].into(),
                );
            }
        }
        if let Some(fields) = fields {
            for (name, _) in fields.iter() {
                let target = galadril_ontology::FieldRef::new(
                    symbol.clone(),
                    name.clone(),
                );
                config.routes.insert(
                    Route::new(target.clone(), Operation::Describe),
                    BTreeSet::new(),
                );
                if symbol.key().name().as_str() == "Profile" {
                    for operation in
                        [Operation::DescribeTransfer, Operation::Write]
                    {
                        config.routes.insert(
                            Route::new(target.clone(), operation),
                            BTreeSet::new(),
                        );
                    }
                }
            }
        }
    }
    config.approved_types.insert(instant()?);
    let checked = galadril_ontology::authorization::compile(project, &config);
    if checked.publishable().is_none() {
        return Err(format!("{:?}", checked.issues()).into());
    }
    Ok(config)
}

pub fn projection(
    project: &Project,
    fields: &[&str],
) -> Result<Projection, TestError> {
    let selection = |name, fields: &[&str]| {
        let access =
            base::access(project, name, None, "schema", Operation::Describe)?;
        Ok::<_, TestError>(Selection::new(
            access,
            fields
                .iter()
                .map(|name| name.parse())
                .collect::<Result<Vec<_>, _>>()?,
        )?)
    };
    let mut projection = Projection::new(selection("Profile", fields)?);
    for (name, fields) in [
        ("Details", &["city", "private_note"][..]),
        ("Status", &[]),
        ("Profile", &[]),
        ("Identifier", &[]),
        ("OptionalName", &[]),
    ] {
        let selection = selection(name, fields)?;
        let Reference::Symbol(symbol) = &selection.access.route.target else {
            return Err("expected symbol".into());
        };
        projection.definitions.insert(symbol.clone(), selection);
    }
    Ok(projection)
}

pub fn mappings() -> Result<ExternalMappings, TestError> {
    let mut mappings = ExternalMappings::default();
    mappings.register(
        instant()?,
        ExternalMapping::new(
            json!({"type": "string", "format": "date-time"}),
            VersionedName::new(
                "mapping.instant".parse()?,
                Version::new(1, 0, 0),
            ),
            MappingPrecision::Exact,
        )?,
    )?;
    Ok(mappings)
}

pub fn describe_routes(config: &mut SecurityEnvironment) {
    let routes: Vec<_> = config
        .routes
        .iter()
        .filter_map(|(route, permissions)| {
            let operation = match route.operation {
                Operation::Read => Operation::Describe,
                Operation::Transfer => Operation::DescribeTransfer,
                _ => return None,
            };
            Some((
                Route::new(route.target.clone(), operation),
                permissions.clone(),
            ))
        })
        .collect();
    config.routes.extend(routes);
}

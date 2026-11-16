//! Deterministic business data and trusted context shared by archive tests.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::sync::{Arc, Mutex};

use cedar_policy::{
    Context, Entity, EntityId, EntityUid, RestrictedExpression,
};
use galadril_ontology::authorization::{
    Access, AuthorityRevision, CedarInput, CedarResource, Consistency,
    Contract, Evidence, Flow, Instance, InstanceEvidence, Operation,
    Principal, RelationshipInput, RelationshipResource, Route,
    SecurityEnvironment, Sink, SinkKind, TrustedRuntime, Versions,
};
use galadril_ontology::semantic::{Environment, Project, analyze_project};
use galadril_ontology::syntax::parse_source;
use galadril_ontology::{
    Classification, ExternalTypeRegistry, FieldRef, Name, Primitive,
    Reference, SymbolRef, Version, VersionedName,
};
use sha2::{Digest, Sha256};

pub type TestError = Box<dyn Error>;
pub type TestResult = Result<(), TestError>;

pub const SCHEMA: &str = include_str!("../fixtures/archive_spicedb.zed");
pub const POLICIES: &str = r#"
    permit(principal, action, resource)
        when { context.hour >= 8 && context.hour < 18 }
        unless { context.region == "blocked" };
    forbid(principal, action, resource is Domain::Document)
        when { resource.visibility == "sealed" };
    forbid(principal, action, resource)
        when { context.region == "error" && principal.badge == "blocked" };
"#;

pub fn project(size: usize, reverse: bool) -> Result<Project, TestError> {
    project_with_document_title(size, reverse, "String")
}

pub fn project_with_document_title(
    size: usize,
    reverse: bool,
    title: &str,
) -> Result<Project, TestError> {
    use std::fmt::Write;

    let documents = include_str!("../fixtures/archive_documents.gol")
        .replace("field title: String", &format!("field title: {title}"));
    let mut input = vec![
        (
            "organizations.gol",
            include_str!("../fixtures/archive_organization.gol"),
        ),
        ("documents.gol", documents.as_str()),
        ("mail.gol", include_str!("../fixtures/archive_mail.gol")),
        (
            "observations.gol",
            include_str!("../fixtures/archive_observations.gol"),
        ),
    ];
    let fillers = ["north", "south", "west"].into_iter().enumerate().map(
        |(offset, region)| -> Result<_, TestError> {
            let mut text = format!("ontology generated.{region} version \"1.0.0\" {{");
            let mut order: Vec<_> = (offset..size).step_by(3).collect();
            if reverse {
                order.reverse();
            }
            for index in order {
                writeln!(text, "entity Entry{index:04} {{ field name: String; field value: i64; }}")?;
            }
            text.push('}');
            Ok((region, text))
        },
    ).collect::<Result<Vec<_>, _>>()?;
    input.extend(
        fillers
            .iter()
            .map(|(region, text)| (*region, text.as_str())),
    );
    if reverse {
        input.reverse();
    }
    let ast = input
        .into_iter()
        .map(|(name, text)| parse_source(name, text))
        .collect::<Result<Vec<_>, _>>()?;
    let registry = ExternalTypeRegistry::default();
    let mut environment = Environment::new(&registry);
    for (name, ty) in context_types()? {
        environment.context.insert(name, ty)?;
    }
    Ok(analyze_project(&ast, &environment)?)
}

fn context_types() -> Result<BTreeMap<Name, Primitive>, TestError> {
    [
        ("mfa", Primitive::Bool),
        ("hour", Primitive::I64),
        ("region", Primitive::String),
    ]
    .into_iter()
    .map(|(name, ty)| Ok((name.parse()?, ty)))
    .collect()
}

pub fn symbol(project: &Project, name: &str) -> Result<SymbolRef, TestError> {
    project
        .symbols()
        .keys()
        .find(|reference| reference.key().name().as_str() == name)
        .cloned()
        .ok_or_else(|| format!("unknown fixture symbol {name}").into())
}

fn revision(name: &str, bytes: &[u8]) -> Result<AuthorityRevision, TestError> {
    Ok(AuthorityRevision::new(
        VersionedName::new(name.parse()?, Version::new(1, 0, 0)),
        Sha256::digest(bytes).into(),
    ))
}

fn route_names(names: &[&str]) -> Result<BTreeSet<Name>, TestError> {
    names.iter().map(|name| Ok(name.parse()?)).collect()
}

pub fn target(
    owner: SymbolRef,
    field: Option<&str>,
) -> Result<Reference, TestError> {
    match field {
        Some(name) => Ok(FieldRef::new(owner, name.parse()?).into()),
        None => Ok(owner.into()),
    }
}

pub fn environment(
    project: &Project,
) -> Result<SecurityEnvironment, TestError> {
    let mut deployment = SecurityEnvironment::new(
        revision("fixture.deployment", b"fixture-deployment-v1")?,
        Flow::new(Classification::Public),
    );
    let mut spice = RelationshipInput::new(
        revision("fixture.rebac", SCHEMA.as_bytes())?,
        revision("fixture.bridge", b"hex-v1")?,
    );
    let mut cedar = CedarInput::new(
        revision("fixture.cedar", POLICIES.as_bytes())?,
        "IAM::Actor".parse()?,
        POLICIES,
    );
    cedar.context = context_types()?;
    cedar
        .schemas
        .push("namespace IAM { entity Actor { badge: String }; }".into());
    for (name, action, permission, fields) in [
        (
            "Organization",
            "organization",
            "view_organization",
            &["name"][..],
        ),
        (
            "Message",
            "message",
            "read_message",
            &["title", "attachment"],
        ),
        ("Attached", "attached", "read_link", &[]),
        ("RestrictedIndex", "index", "read_index", &["title"]),
        (
            "Observation",
            "observation",
            "read_observation",
            &["summary", "message"],
        ),
        (
            "Document",
            "document",
            "read_doc",
            &[
                "title",
                "visibility",
                "body",
                "sealed",
                "excerpt",
                "organization",
            ],
        ),
    ] {
        let symbol = symbol(project, name)?;
        let mut resource =
            CedarResource::new(format!("Domain::{name}").parse()?);
        resource
            .actions
            .insert(format!("Domain::Action::\"{action}\"").parse()?);
        if name == "Message" {
            resource
                .actions
                .insert(r#"Domain::Action::"attachment""#.parse()?);
        }
        if name == "Document" {
            resource.fields.insert("visibility".parse()?);
            resource
                .actions
                .insert(r#"Domain::Action::"body""#.parse()?);
            resource
                .actions
                .insert(r#"Domain::Action::"export""#.parse()?);
            deployment.routes.insert(
                Route::new(symbol.clone(), Operation::Transfer),
                route_names(&["send_doc"])?,
            );
            for field in ["title", "body"] {
                deployment.routes.insert(
                    Route::new(
                        target(symbol.clone(), Some(field))?,
                        Operation::Transfer,
                    ),
                    BTreeSet::new(),
                );
            }
        }
        cedar.resources.insert(symbol.clone(), resource);
        spice.resources.insert(
            symbol.clone(),
            RelationshipResource::new(
                "company_record",
                route_names(&["view", "view_body", "export"])?,
            ),
        );
        deployment.routes.insert(
            Route::new(symbol.clone(), Operation::Read),
            route_names(&[permission])?,
        );
        for field in fields {
            deployment.routes.insert(
                Route::new(
                    target(symbol.clone(), Some(field))?,
                    Operation::Read,
                ),
                BTreeSet::new(),
            );
        }
    }
    deployment.cedar = Some(cedar);
    deployment.relationship = Some(spice);
    for (name, kind, clearance, compartments) in [
        (
            "Internal",
            SinkKind::Internal,
            Classification::Restricted,
            &["Legal", "Audit"][..],
        ),
        (
            "LowClearance",
            SinkKind::Internal,
            Classification::Internal,
            &["Legal"],
        ),
        (
            "NoCompartment",
            SinkKind::Internal,
            Classification::Restricted,
            &[],
        ),
        (
            "LLM",
            SinkKind::External,
            Classification::Restricted,
            &["Legal", "Audit"],
        ),
    ] {
        let mut sink = Sink::new(kind, clearance);
        sink.compartments = compartments
            .iter()
            .map(|name| name.parse())
            .collect::<Result<_, _>>()?;
        sink.purposes = ["Review".parse()?, "Analytics".parse()?].into();
        deployment.sinks.insert(name.parse()?, sink);
    }
    Ok(deployment)
}

pub fn access(
    project: &Project,
    name: &str,
    field: Option<&str>,
    id: &str,
    operation: Operation,
) -> Result<Access, TestError> {
    let owner = symbol(project, name)?;
    Ok(Access {
        route: Route::new(target(owner.clone(), field)?, operation),
        instance: Instance {
            tenant: "tenant_a".into(),
            owner,
            id: id.into(),
        },
        destination: "Internal".parse()?,
        purpose: "Review".parse()?,
    })
}

#[derive(Clone)]
pub struct Runtime<'a> {
    pub contract: &'a Contract,
    pub versions: Versions,
    pub tenant: &'a str,
    pub subject: &'a str,
    pub subject_type: &'a str,
    pub hour: Option<i64>,
    pub mfa: bool,
    pub region: &'a str,
    pub visibility: &'a str,
    pub consistency: Consistency,
    pub dependencies: Arc<BTreeMap<Route, Vec<InstanceEvidence>>>,
    pub calculations: Arc<
        Mutex<
            BTreeMap<
                String,
                Arc<galadril_ontology::authorization::ExecutionContext>,
            >,
        >,
    >,
}

pub fn runtime(contract: &Contract) -> Runtime<'_> {
    Runtime {
        contract,
        versions: contract.versions().clone(),
        tenant: "tenant_a",
        subject: "owner",
        subject_type: "user",
        hour: Some(10),
        mfa: true,
        region: "local",
        visibility: "open",
        consistency: Consistency::FullyConsistent,
        dependencies: Arc::default(),
        calculations: Arc::default(),
    }
}

impl TrustedRuntime for Runtime<'_> {
    fn computation(
        &self,
        principal: &Principal,
    ) -> Option<Arc<galadril_ontology::authorization::ExecutionContext>> {
        use galadril_ontology::authorization::{Clearance, ExecutionContext};
        let mut contexts = self.calculations.lock().ok()?;
        let key = principal.cedar.to_string();
        if let Some(context) = contexts.get(&key) {
            return Some(Arc::clone(context));
        }
        let mut clearance = Clearance::new(Classification::Restricted);
        clearance
            .compartments
            .extend(self.contract.routes().flat_map(|(_, descriptor)| {
                descriptor.flow().compartments.iter().cloned()
            }));
        let context = match ExecutionContext::from_inputs(
            principal,
            clearance,
            Flow::new(Classification::Public),
        ) {
            Ok(context) => Arc::new(context),
            Err(error) => {
                tracing::error!(reason = ?error, "Invalid fixture computation");
                return None;
            },
        };
        contexts.insert(key, Arc::clone(&context));
        Some(context)
    }

    fn verify(&self, access: &Access) -> Option<Evidence> {
        let subject = format!("{}/{}", self.tenant, self.subject);
        let cedar = EntityUid::from_type_name_and_id(
            "IAM::Actor".parse().ok()?,
            EntityId::new(&subject),
        );
        let context = Context::from_pairs(
            [
                ("mfa".into(), RestrictedExpression::new_bool(self.mfa)),
                (
                    "region".into(),
                    RestrictedExpression::new_string(self.region.into()),
                ),
            ]
            .into_iter()
            .chain(self.hour.map(|hour| {
                ("hour".into(), RestrictedExpression::new_long(hour))
            })),
        )
        .ok()?;
        let primary = InstanceEvidence {
            instance: access.instance.clone(),
            revision: "fixture_data".into(),
            flow: None,
        };
        let mut entities = BTreeMap::new();
        for (route, instance) in std::iter::once((&access.route, &primary))
            .chain(self.dependencies.iter().flat_map(|(route, instances)| {
                instances.iter().map(move |instance| (route, instance))
            }))
        {
            let descriptor = self.contract.descriptor(route)?;
            let uid = EntityUid::from_type_name_and_id(
                descriptor.cedar_resource_type().clone(),
                EntityId::new(instance.instance.object_id()?),
            );
            let attrs = if instance.instance.owner.key().name().as_str() ==
                "Document"
            {
                [(
                    "visibility".into(),
                    RestrictedExpression::new_string(self.visibility.into()),
                )]
                .into()
            } else {
                Default::default()
            };
            entities.insert(
                uid.clone(),
                Entity::new(uid, attrs, Default::default()).ok()?,
            );
        }
        Some(Evidence {
            max_clearance: galadril_ontology::authorization::Clearance {
                classification: Classification::Restricted,
                compartments: self
                    .contract
                    .routes()
                    .flat_map(|(_, descriptor)| {
                        descriptor.flow().compartments.iter().cloned()
                    })
                    .collect(),
            },
            principal: Principal {
                issuer: "fixture-iam".into(),
                tenant: self.tenant.into(),
                subject_type: self.subject_type.into(),
                subject_id: subject.into(),
                cedar,
            },
            primary,
            dependencies: self
                .dependencies
                .iter()
                .map(|(route, instances)| {
                    (
                        route.clone(),
                        instances
                            .iter()
                            .map(|instance| InstanceEvidence {
                                instance: instance.instance.clone(),
                                revision: instance.revision.clone(),
                                flow: instance.flow.clone(),
                            })
                            .collect(),
                    )
                })
                .collect(),
            versions: self.versions.clone(),
            consistency: self.consistency.clone(),
            context,
            context_flow: Flow::new(Classification::Public),
            entities: entities.into_values().collect(),
            relationship_context: BTreeMap::new(),
            destination: access.destination.clone(),
            purpose: access.purpose.clone(),
        })
    }
}

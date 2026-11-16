//! Public IFC and publication contracts.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::future::{Future, ready};
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, OnceLock};
use std::task::{Context as TaskContext, Poll, Waker};

use cedar_policy::{
    Context, Entity, EntityId, EntityUid, RestrictedExpression,
};
use galadril_ontology::authorization::{
    Access, AuthorityRevision, CedarInput, CedarResource, Compilation,
    Consistency, Contract, Decision, Denial, Evidence, Flow, Instance,
    InstanceEvidence, IssueKind, Operation, PermissionCheck,
    PermissionOutcome, PermissionProvider, Principal, RelationshipInput,
    RelationshipResource, Route, SecurityEnvironment, Sink, SinkKind,
    TrustedRuntime, authorize, compile,
};
use galadril_ontology::semantic::{Environment, Project, analyze_project};
use galadril_ontology::syntax::parse_source;
use galadril_ontology::{
    Classification, ExternalTypeRegistry, FieldRef, Name, Primitive,
    Reference, SymbolRef, Version, VersionedName,
};

type TestResult = Result<(), Box<dyn Error>>;

#[test]
fn joins_labels_without_widening_authorized_destinations() -> TestResult {
    let mut a = Flow::new(Classification::Internal);
    a.compartments.insert("Team".parse()?);
    a.destinations =
        Some(BTreeSet::from(["Internal".parse()?, "Audit".parse()?]));
    let mut b = Flow::new(Classification::Confidential);
    b.compartments.insert("Mail".parse()?);
    b.destinations = Some(BTreeSet::from(["Internal".parse()?]));
    let joined = a.join(&b);
    assert_eq!(joined, b.join(&a));
    assert_eq!(joined.classification, Classification::Confidential);
    assert_eq!(joined.compartments.len(), 2);
    assert_eq!(
        joined.destinations,
        Some(BTreeSet::from(["Internal".parse()?]))
    );
    assert_eq!(joined.join(&joined), joined);
    Ok(())
}

#[test]
fn keeps_unlinked_security_out_of_publication() -> TestResult {
    let registry = ExternalTypeRegistry::default();
    let mut env = Environment::new(&registry);
    env.context.insert("mfa".parse()?, Primitive::Bool)?;
    let source = parse_source(
        "security_mail.gol",
        include_str!("fixtures/security_mail.gol"),
    )?;
    let project = analyze_project(&[source], &env)?;
    let environment = SecurityEnvironment::new(
        revision("mail.security")?,
        Flow::new(Classification::Public),
    );
    let result = compile(&project, &environment);
    assert!(matches!(result, Compilation::PendingExternalValidation(_)));
    assert!(result.publishable().is_none());
    Ok(())
}

type TestError = Box<dyn Error>;

const PERMIT: &str = "permit(principal is IAM::User, action, resource);";

fn revision(name: &str) -> Result<AuthorityRevision, TestError> {
    Ok(AuthorityRevision::new(
        VersionedName::new(name.parse()?, Version::new(1, 0, 0)),
        [1; 32],
    ))
}

fn project() -> Result<Project, TestError> {
    let types = ExternalTypeRegistry::default();
    let mut environment = Environment::new(&types);
    environment
        .context
        .insert("mfa".parse()?, Primitive::Bool)?;
    let source = parse_source(
        "security_mail.gol",
        include_str!("fixtures/security_mail.gol"),
    )?;
    Ok(analyze_project(&[source], &environment)?)
}

fn symbol(project: &Project, name: &str) -> Result<SymbolRef, TestError> {
    project
        .symbols()
        .keys()
        .find(|reference| reference.key().name().as_str() == name)
        .cloned()
        .ok_or_else(|| format!("missing symbol {name}").into())
}

fn permission_names(names: &[&str]) -> Result<BTreeSet<Name>, TestError> {
    names.iter().map(|name| Ok(name.parse()?)).collect()
}

fn setup(
    project: &Project,
    policies: &str,
) -> Result<SecurityEnvironment, TestError> {
    let message = symbol(project, "Message")?;
    let summary = symbol(project, "MessageSummary")?;
    let attached = symbol(project, "Attached")?;
    let mut relationship = RelationshipInput::new(
        revision("mail.rebac")?,
        revision("mail.bridge")?,
    );
    relationship.resources.insert(
        message.clone(),
        RelationshipResource::new(
            "mail_message",
            permission_names(&["view", "view_body", "export"])?,
        ),
    );
    relationship.resources.insert(
        summary.clone(),
        RelationshipResource::new(
            "mail_summary",
            permission_names(&["view"])?,
        ),
    );
    relationship.resources.insert(
        attached.clone(),
        RelationshipResource::new(
            "mail_attachment_link",
            permission_names(&["view"])?,
        ),
    );
    let mut cedar = CedarInput::new(
        revision("mail.cedar")?,
        "IAM::User".parse()?,
        policies,
    );
    cedar
        .schemas
        .push("namespace IAM { entity User { badge: Bool }; }".into());
    cedar.context.insert("mfa".parse()?, Primitive::Bool);
    let mut resource = CedarResource::new("Mail::Message".parse()?);
    resource.fields.insert("subject".parse()?);
    resource.actions = [
        r#"Mail::Action::"metadata""#,
        r#"Mail::Action::"body""#,
        r#"Mail::Action::"export""#,
    ]
    .into_iter()
    .map(str::parse)
    .collect::<Result<_, _>>()?;
    cedar.resources.insert(message.clone(), resource);
    let mut view = CedarResource::new("Mail::Summary".parse()?);
    view.actions.insert(r#"Mail::Action::"summary""#.parse()?);
    cedar.resources.insert(summary.clone(), view);
    let mut link = CedarResource::new("Mail::Attached".parse()?);
    link.actions.insert(r#"Mail::Action::"link""#.parse()?);
    cedar.resources.insert(attached.clone(), link);
    let mut env = SecurityEnvironment::new(
        revision("mail.contract")?,
        Flow::new(Classification::Public),
    );
    env.relationship = Some(relationship);
    env.cedar = Some(cedar);
    for (operation, permission) in [
        (Operation::Read, "read_meta"),
        (Operation::Transfer, "export"),
    ] {
        env.routes.insert(
            Route::new(message.clone(), operation),
            permission_names(&[permission])?,
        );
        for field in ["subject", "body"] {
            env.routes.insert(
                Route::new(
                    FieldRef::new(message.clone(), field.parse()?),
                    operation,
                ),
                BTreeSet::new(),
            );
        }
    }
    env.routes.insert(
        Route::new(summary, Operation::Read),
        permission_names(&["read_summary"])?,
    );
    env.routes.insert(
        Route::new(attached, Operation::Read),
        permission_names(&["read_link"])?,
    );
    for (name, kind) in [
        ("Internal", SinkKind::Internal),
        ("LLM", SinkKind::External),
    ] {
        let mut sink = Sink::new(kind, Classification::Restricted);
        sink.compartments.insert("Mail".parse()?);
        sink.purposes.insert("Review".parse()?);
        env.sinks.insert(name.parse()?, sink);
    }
    Ok(env)
}

fn require_contract(
    compilation: &Compilation,
) -> Result<&Contract, TestError> {
    compilation.publishable().ok_or_else(|| {
        format!("expected validated contract: {:?}", compilation.issues())
            .into()
    })
}

#[test]
fn approved_target_floors_preserve_property_granularity_and_reject_unknown_targets()
-> TestResult {
    let project = project()?;
    let message = symbol(&project, "Message")?;
    let subject =
        Reference::Field(FieldRef::new(message.clone(), "subject".parse()?));
    let mut environment = setup(&project, PERMIT)?;
    let mut floor = Flow::new(Classification::Restricted);
    let compartment = "Mailbox".parse::<galadril_ontology::QualifiedName>()?;
    floor.compartments.insert(compartment.clone());
    environment
        .target_floors
        .insert(subject.clone(), floor.clone());
    let compiled = compile(&project, &environment);
    let contract = require_contract(&compiled)?;
    assert!(
        contract
            .flow(&subject)
            .is_some_and(|flow| flow.dominates(&floor))
    );
    assert!(
        contract
            .flow(&Reference::Symbol(message.clone()))
            .is_some_and(|flow| !flow.compartments.contains(&compartment))
    );
    let unknown = Reference::Field(FieldRef::new(message, "missing".parse()?));
    environment.target_floors.insert(unknown, floor);
    assert!(
        matches!(compile(&project,&environment),Compilation::Invalid(issues) if issues.iter().any(|issue|issue.kind()==IssueKind::InvalidTarget))
    );
    Ok(())
}

#[test]
fn validates_binding_targets_and_linked_native_schemas() -> TestResult {
    let project = project()?;
    let environment = setup(&project, PERMIT)?;
    require_contract(&compile(&project, &environment))?;
    let mut unknown_module = setup(&project, PERMIT)?;
    unknown_module.module_floors.insert(
        "communication.unknown".parse()?,
        Flow::new(Classification::Restricted),
    );
    assert!(matches!(
        compile(&project, &unknown_module),
        Compilation::Invalid(issues)
            if issues.iter().any(|issue| issue.kind() == IssueKind::InvalidTarget)
    ));
    let mut missing_iam = setup(&project, PERMIT)?;
    missing_iam
        .cedar
        .as_mut()
        .ok_or("missing Cedar bundle")?
        .schemas
        .clear();
    let pending = compile(&project, &missing_iam);
    assert!(matches!(pending, Compilation::PendingExternalValidation(_)));
    assert!(
        pending
            .issues()
            .iter()
            .any(|issue| issue.kind() == IssueKind::UnlinkedCedarSchema)
    );
    let mut unknown_permission = setup(&project, PERMIT)?;
    let resource = unknown_permission
        .relationship
        .as_mut()
        .ok_or("missing relationship schema")?
        .resources
        .get_mut(&symbol(&project, "Message")?)
        .ok_or("missing Message profile")?;
    resource.permissions.remove("view_body");
    assert!(compile(&project, &unknown_permission).issues().iter().any(|issue| issue.kind() == IssueKind::InvalidRelationshipBinding));
    let mut wrong_resource = setup(&project, PERMIT)?;
    wrong_resource
        .relationship
        .as_mut()
        .ok_or("missing relationship schema")?
        .resources
        .get_mut(&symbol(&project, "Message")?)
        .ok_or("missing Message profile")?
        .resource_type = "wrong_resource".into();
    assert!(matches!(
        compile(&project, &wrong_resource),
        Compilation::Invalid(_)
    ));
    let mut wrong_action = setup(&project, PERMIT)?;
    let message = symbol(&project, "Message")?;
    let body_action: EntityUid = r#"Mail::Action::"body""#.parse()?;
    let input = wrong_action.cedar.as_mut().ok_or("missing Cedar input")?;
    input
        .resources
        .get_mut(&message)
        .ok_or("missing Message")?
        .actions
        .remove(&body_action);
    input
        .resources
        .get_mut(&symbol(&project, "MessageSummary")?)
        .ok_or("missing Summary")?
        .actions
        .insert(body_action);
    assert!(
        compile(&project, &wrong_action)
            .issues()
            .iter()
            .any(|issue| issue.kind() == IssueKind::InvalidCedarSchema)
    );
    for policy in [
        r#"permit(principal, action == Mail::Action::"body", resource) when { resource.unknown };"#,
        "permit(principal, action, resource) when { context.mfa < 3 };",
        r#"permit(principal, action == Mail::Action::"summary", resource is Mail::Message);"#,
    ] {
        let result = compile(&project, &setup(&project, policy)?);
        assert!(
            result
                .issues()
                .iter()
                .any(|issue| issue.kind() == IssueKind::InvalidCedarPolicy),
            "{policy}: {:?}",
            result.issues()
        );
    }
    Ok(())
}

#[test]
fn propagates_filter_labels_and_requires_their_read_routes() -> TestResult {
    let project = project()?;
    let env = setup(&project, PERMIT)?;
    let compilation = compile(&project, &env);
    let contract = require_contract(&compilation)?;
    let summary = symbol(&project, "MessageSummary")?;
    let subject = FieldRef::new(summary.clone(), "subject".parse()?);
    let flow = contract
        .flow(&subject.into())
        .ok_or("missing output flow")?;
    assert_eq!(flow.classification, Classification::Confidential);
    assert!(flow.compartments.contains(&"Mail".parse()?));
    let descriptor = contract
        .descriptor(&Route::new(summary, Operation::Read))
        .ok_or("missing view route")?;
    let body = FieldRef::new(symbol(&project, "Message")?, "body".parse()?);
    assert!(
        descriptor
            .dependencies()
            .contains(&Route::new(body, Operation::Read))
    );
    Ok(())
}

#[derive(Clone)]
struct Runtime<'a> {
    contract: &'a Contract,
    authorized: bool,
    mfa: Option<bool>,
    obsolete_bundle: bool,
    foreign_tenant: bool,
    computation: Arc<
        OnceLock<
            Option<Arc<galadril_ontology::authorization::ExecutionContext>>,
        >,
    >,
}

impl TrustedRuntime for Runtime<'_> {
    fn computation(
        &self,
        principal: &Principal,
    ) -> Option<Arc<galadril_ontology::authorization::ExecutionContext>> {
        use galadril_ontology::authorization::{Clearance, ExecutionContext};
        self.computation.get_or_init(|| {
            let mut clearance = Clearance::new(Classification::Restricted);
            clearance.compartments.extend(self.contract.routes().flat_map(|(_, descriptor)| descriptor.flow().compartments.iter().cloned()));
            match ExecutionContext::from_inputs(principal, clearance, Flow::new(Classification::Public)) {
                Ok(context) => Some(Arc::new(context)),
                Err(error) => { tracing::error!(reason = ?error, "Invalid fixture computation"); None },
            }
        }).as_ref().map(Arc::clone)
    }

    fn verify(&self, access: &Access) -> Option<Evidence> {
        if !self.authorized {
            return None;
        }
        let mut versions = self.contract.versions().clone();
        if self.obsolete_bundle {
            versions.cedar.sha256 = [2; 32];
        }
        let subject_type = "IAM::User".parse().ok()?;
        let cedar = EntityUid::from_type_name_and_id(
            subject_type,
            EntityId::new("tenant:alice"),
        );
        let context =
            Context::from_pairs(self.mfa.into_iter().map(|mfa| {
                ("mfa".into(), RestrictedExpression::new_bool(mfa))
            }))
            .ok()?;
        let resource_uid = EntityUid::from_type_name_and_id(
            self.contract
                .descriptor(&access.route)?
                .cedar_resource_type()
                .clone(),
            EntityId::new(access.instance.object_id()?),
        );
        let attrs = if access.instance.owner.key().name().as_str() == "Message"
        {
            [(
                "subject".into(),
                RestrictedExpression::new_string("Delivery".into()),
            )]
            .into()
        } else {
            Default::default()
        };
        let resource =
            Entity::new(resource_uid, attrs, Default::default()).ok()?;
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
                issuer: "trusted-iam".into(),
                tenant: if self.foreign_tenant {
                    "other".into()
                } else {
                    access.instance.tenant.clone()
                },
                subject_type: "iam/user".into(),
                subject_id: "alice".into(),
                cedar,
            },
            primary: InstanceEvidence {
                instance: access.instance.clone(),
                revision: "data-v1".into(),
                flow: None,
            },
            dependencies: BTreeMap::new(),
            versions,
            consistency: Consistency::AtLeastAsFresh("minimum".into()),
            context,
            context_flow: Flow::new(Classification::Public),
            entities: vec![resource],
            relationship_context: BTreeMap::new(),
            destination: access.destination.clone(),
            purpose: access.purpose.clone(),
        })
    }
}

struct MockProvider(AtomicU8);

impl PermissionProvider for MockProvider {
    fn check(
        &self,
        request: PermissionCheck<'_>,
    ) -> impl Future<Output = PermissionOutcome> + Send {
        ready(match self.0.load(Ordering::Relaxed) {
            1 => PermissionOutcome::Deny,
            2 => PermissionOutcome::Conditional,
            3 => PermissionOutcome::Unavailable,
            4 => PermissionOutcome::Allow(Consistency::AtLeastAsFresh(
                "older".into(),
            )),
            5 if request.permission.as_str() == "view_body" => {
                PermissionOutcome::Deny
            },
            _ => PermissionOutcome::Allow(request.consistency.clone()),
        })
    }
}

fn run(future: impl Future<Output = Decision>) -> Result<Decision, TestError> {
    let mut future = std::pin::pin!(future);
    match future
        .as_mut()
        .poll(&mut TaskContext::from_waker(Waker::noop()))
    {
        Poll::Ready(decision) => Ok(decision),
        Poll::Pending => {
            Err("the deterministic provider unexpectedly suspended".into())
        },
    }
}

fn access(
    project: &Project,
    owner: &str,
    field: Option<&str>,
    operation: Operation,
    destination: &str,
) -> Result<Access, TestError> {
    let owner = symbol(project, owner)?;
    let target: Reference = if let Some(field) = field {
        FieldRef::new(owner.clone(), field.parse()?).into()
    } else {
        owner.clone().into()
    };
    Ok(Access {
        route: Route::new(target, operation),
        instance: Instance {
            tenant: "tenant".into(),
            owner,
            id: "mail:42".into(),
        },
        destination: destination.parse()?,
        purpose: "Review".parse()?,
    })
}

#[test]
fn composes_current_relationship_cedar_and_flow_obligations() -> TestResult {
    let project = project()?;
    let compilation = compile(&project, &setup(&project, PERMIT)?);
    let contract = require_contract(&compilation)?;
    let runtime = Runtime {
        contract,
        authorized: true,
        mfa: Some(true),
        obsolete_bundle: false,
        foreign_tenant: false,
        computation: Arc::default(),
    };
    let provider = MockProvider(AtomicU8::new(0));
    let subject = access(
        &project,
        "Message",
        Some("subject"),
        Operation::Read,
        "Internal",
    )?;
    let body = access(
        &project,
        "Message",
        Some("body"),
        Operation::Read,
        "Internal",
    )?;
    assert_eq!(
        run(authorize(contract, &body, &runtime, &provider))?,
        Decision::Allow
    );
    for (mode, denial) in [
        (1, Denial::Relationship),
        (2, Denial::Conditional),
        (3, Denial::Unavailable),
        (4, Denial::Freshness),
    ] {
        provider.0.store(mode, Ordering::Relaxed);
        assert_eq!(
            run(authorize(contract, &body, &runtime, &provider))?,
            Decision::Deny(denial)
        );
    }
    provider.0.store(5, Ordering::Relaxed);
    assert_eq!(
        run(authorize(contract, &subject, &runtime, &provider))?,
        Decision::Allow
    );
    assert_eq!(
        run(authorize(contract, &body, &runtime, &provider))?,
        Decision::Deny(Denial::Relationship)
    );
    provider.0.store(0, Ordering::Relaxed);
    let export = access(
        &project,
        "Message",
        Some("body"),
        Operation::Transfer,
        "LLM",
    )?;
    assert_eq!(
        run(authorize(contract, &export, &runtime, &provider))?,
        Decision::Deny(Denial::Flow)
    );
    let external_read =
        access(&project, "Message", Some("subject"), Operation::Read, "LLM")?;
    assert_eq!(
        run(authorize(contract, &external_read, &runtime, &provider))?,
        Decision::Deny(Denial::Flow)
    );
    let unknown = access(
        &project,
        "Message",
        Some("body"),
        Operation::Read,
        "Unknown",
    )?;
    assert_eq!(
        run(authorize(contract, &unknown, &runtime, &provider))?,
        Decision::Deny(Denial::Flow)
    );
    let summary = access(
        &project,
        "MessageSummary",
        None,
        Operation::Read,
        "Internal",
    )?;
    assert_eq!(
        run(authorize(contract, &summary, &runtime, &provider))?,
        Decision::Deny(Denial::Dependency)
    );
    let attachment =
        access(&project, "Attachment", None, Operation::Read, "Internal")?;
    assert_eq!(
        run(authorize(contract, &attachment, &runtime, &provider))?,
        Decision::Deny(Denial::Route)
    );
    for (runtime, denial) in [
        (
            Runtime {
                authorized: false,
                ..runtime.clone()
            },
            Denial::Runtime,
        ),
        (
            Runtime {
                obsolete_bundle: true,
                ..runtime.clone()
            },
            Denial::Version,
        ),
        (
            Runtime {
                foreign_tenant: true,
                ..runtime.clone()
            },
            Denial::Resource,
        ),
        (
            Runtime {
                mfa: None,
                ..runtime.clone()
            },
            Denial::CedarError,
        ),
        (
            Runtime {
                mfa: Some(false),
                ..runtime.clone()
            },
            Denial::Cedar,
        ),
    ] {
        assert_eq!(
            run(authorize(contract, &body, &runtime, &provider))?,
            Decision::Deny(denial)
        );
    }
    Ok(())
}

#[test]
fn rejects_cedar_skip_on_error_even_with_a_permit() -> TestResult {
    let project = project()?;
    for (policies, expected) in [
        ("", Denial::Cedar),
        (
            "permit(principal, action, resource); forbid(principal, action, resource);",
            Denial::Cedar,
        ),
        (
            "permit(principal, action, resource); forbid(principal, action, resource) when { principal.badge };",
            Denial::CedarError,
        ),
    ] {
        let compilation = compile(&project, &setup(&project, policies)?);
        let contract = require_contract(&compilation)?;
        let runtime = Runtime {
            contract,
            authorized: true,
            mfa: Some(true),
            obsolete_bundle: false,
            foreign_tenant: false,
            computation: Arc::default(),
        };
        let requested = access(
            &project,
            "Message",
            Some("body"),
            Operation::Read,
            "Internal",
        )?;
        let provider = MockProvider(AtomicU8::new(0));
        assert_eq!(
            run(authorize(contract, &requested, &runtime, &provider))?,
            Decision::Deny(expected)
        );
    }
    Ok(())
}

#[test]
fn resource_identifiers_separate_tenants_and_preserve_logical_identity()
-> TestResult {
    let project = project()?;
    let original =
        access(&project, "Message", None, Operation::Read, "Internal")?
            .instance;
    let moved = Instance {
        tenant: "another".into(),
        ..original.clone()
    };
    assert_ne!(original.object_id(), moved.object_id());
    let new_version =
        SymbolRef::new(original.owner.key().clone(), Version::new(2, 0, 0));
    let updated = Instance {
        owner: new_version,
        ..original.clone()
    };
    assert_eq!(original.object_id(), updated.object_id());
    let too_large = Instance {
        id: "a".repeat(600).into(),
        ..original
    };
    assert!(too_large.object_id().is_none());
    Ok(())
}

#[test]
fn transfer_routes_require_distinct_permission_and_existing_read_route()
-> TestResult {
    let project = project()?;
    let mut environment = setup(&project, PERMIT)?;
    let message = symbol(&project, "Message")?;
    environment.routes.insert(
        Route::new(message.clone(), Operation::Transfer),
        permission_names(&["read_meta"])?,
    );
    let result = compile(&project, &environment);
    assert!(matches!(result, Compilation::Invalid(_)));

    let mut environment = setup(&project, PERMIT)?;
    let subject = FieldRef::new(message, "subject".parse()?);
    environment
        .routes
        .remove(&Route::new(subject, Operation::Read));
    let result = compile(&project, &environment);
    assert!(matches!(result, Compilation::PendingExternalValidation(_)));
    Ok(())
}

#[test]
fn raises_a_derived_field_above_a_less_restrictive_annotation() -> TestResult {
    let project = project()?;
    let mut env = setup(&project, PERMIT)?;
    let preview =
        FieldRef::new(symbol(&project, "Message")?, "preview".parse()?);
    let reference = preview.into();
    let compilation = compile(&project, &env);
    let label = require_contract(&compilation)?
        .flow(&reference)
        .ok_or("missing derived flow")?;
    assert_eq!(label.classification, Classification::Confidential);
    assert!(label.compartments.contains(&"Mail".parse()?));
    env.baseline = Flow::new(Classification::Restricted);
    let compilation = compile(&project, &env);
    let contract = require_contract(&compilation)?;
    let label = contract.flow(&reference).ok_or("missing flow")?;
    assert_eq!(label.classification, Classification::Restricted);
    assert!(label.compartments.contains(&"Mail".parse()?));
    Ok(())
}

#[test]
fn rejects_whole_struct_reads_without_child_enforcement() -> TestResult {
    let project = project()?;
    let mut env = setup(&project, PERMIT)?;
    let field =
        FieldRef::new(symbol(&project, "Message")?, "envelope".parse()?);
    env.routes
        .insert(Route::new(field, Operation::Read), BTreeSet::new());
    let compilation = compile(&project, &env);
    assert!(
        compilation
            .issues()
            .iter()
            .any(|issue| issue.kind() == IssueKind::InvalidFlow)
    );
    assert!(compilation.publishable().is_none());
    Ok(())
}

#[derive(Clone)]
struct Traversed<'a> {
    local: Runtime<'a>,
    foreign: bool,
}

impl TrustedRuntime for Traversed<'_> {
    fn computation(
        &self,
        principal: &Principal,
    ) -> Option<Arc<galadril_ontology::authorization::ExecutionContext>> {
        self.local.computation(principal)
    }

    fn verify(&self, access: &Access) -> Option<Evidence> {
        let mut evidence = self.local.verify(access)?;
        let descriptor = self.local.contract.descriptor(&access.route)?;
        let mut entities = BTreeSet::new();
        for route in descriptor.dependencies() {
            let owner = match &route.target {
                Reference::Symbol(owner) => owner,
                Reference::Field(field) => &field.owner,
                _ => return None,
            };
            let instance = Instance {
                tenant: if self.foreign {
                    "other".into()
                } else {
                    evidence.principal.tenant.clone()
                },
                owner: owner.clone(),
                id: "referenced-instance".into(),
            };
            let descriptor = self.local.contract.descriptor(route)?;
            let object_id = instance.object_id()?;
            if entities.insert(object_id.clone()) {
                let uid = EntityUid::from_type_name_and_id(
                    descriptor.cedar_resource_type().clone(),
                    EntityId::new(object_id),
                );
                let attrs = if owner.key().name().as_str() == "Message" {
                    [(
                        "subject".into(),
                        RestrictedExpression::new_string("Delivery".into()),
                    )]
                    .into()
                } else {
                    Default::default()
                };
                evidence
                    .entities
                    .push(Entity::new(uid, attrs, Default::default()).ok()?);
            }
            evidence.dependencies.insert(
                route.clone(),
                vec![InstanceEvidence {
                    instance,
                    revision: "nested-v1".into(),
                    flow: None,
                }],
            );
        }
        Some(evidence)
    }
}

#[test]
fn checks_each_resource_on_a_relation_projection() -> TestResult {
    let project = project()?;
    let compilation = compile(&project, &setup(&project, PERMIT)?);
    let contract = require_contract(&compilation)?;
    let runtime = Traversed {
        local: Runtime {
            contract,
            authorized: true,
            mfa: Some(true),
            obsolete_bundle: false,
            foreign_tenant: false,
            computation: Arc::default(),
        },
        foreign: false,
    };
    let view = access(
        &project,
        "MessageSummary",
        None,
        Operation::Read,
        "Internal",
    )?;
    let provider = MockProvider(AtomicU8::new(0));
    assert_eq!(
        run(authorize(contract, &view, &runtime, &provider))?,
        Decision::Allow
    );
    provider.0.store(5, Ordering::Relaxed);
    assert_eq!(
        run(authorize(contract, &view, &runtime, &provider))?,
        Decision::Deny(Denial::Relationship)
    );
    provider.0.store(0, Ordering::Relaxed);
    let foreign = Traversed {
        foreign: true,
        ..runtime.clone()
    };
    assert_eq!(
        run(authorize(contract, &view, &foreign, &provider))?,
        Decision::Deny(Denial::Resource)
    );
    Ok(())
}

#[test]
fn release_join_preserves_all_ceilings_across_input_order() -> TestResult {
    let classes = [
        Classification::Public,
        Classification::Internal,
        Classification::Confidential,
        Classification::Restricted,
    ];
    let sinks = BTreeSet::from(["Internal".parse()?, "Audit".parse()?]);
    let mut values = Vec::new();
    for class in classes {
        for destinations in [None, Some(BTreeSet::new()), Some(sinks.clone())]
        {
            let mut flow = Flow::new(class);
            flow.destinations = destinations;
            values.push(flow);
        }
    }
    for a in &values {
        for b in &values {
            let ab = a.join(b);
            assert!(ab.dominates(a) && ab.dominates(b));
            for c in &values {
                assert_eq!(ab.join(c), a.join(&c.join(b)));
            }
        }
    }
    Ok(())
}

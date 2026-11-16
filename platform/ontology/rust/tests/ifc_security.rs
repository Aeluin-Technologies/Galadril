//! IFC admission is mandatory even when both policy engines grant a write.

#[path = "support/archive_fixture.rs"]
mod base;
#[path = "support/export_fixture.rs"]
#[expect(
    dead_code,
    reason = "Only the security fixture builders are shared here"
)]
mod fixture;

use galadril_ontology::authorization::{
    Access, Decision, Denial, Evidence, Operation, PermissionCheck,
    PermissionOutcome, PermissionProvider, TrustedRuntime, authorize, compile,
};

struct Unscoped<'a>(&'a base::Runtime<'a>);
impl TrustedRuntime for Unscoped<'_> {
    fn verify(&self, access: &Access) -> Option<Evidence> {
        self.0.verify(access)
    }
}
struct Grant;
impl PermissionProvider for Grant {
    async fn check(&self, request: PermissionCheck<'_>) -> PermissionOutcome {
        PermissionOutcome::Allow(request.consistency.clone())
    }
}

#[tokio::test]
async fn an_untracked_write_cannot_be_authorized() -> base::TestResult {
    let project = fixture::project()?;
    let environment = fixture::environment(&project)?;
    let compiled = compile(&project, &environment);
    let contract = compiled.publishable().ok_or("contract")?;
    let runtime = base::runtime(contract);
    let write = base::access(
        &project,
        "Profile",
        Some("title"),
        "schema",
        Operation::Write,
    )?;
    assert_eq!(
        authorize(contract, &write, &Unscoped(&runtime), &Grant).await,
        Decision::Deny(Denial::Flow)
    );
    Ok(())
}

struct Bound<'a> {
    runtime: &'a base::Runtime<'a>,
    context:
        std::sync::Arc<galadril_ontology::authorization::ExecutionContext>,
    body: bool,
}
impl TrustedRuntime for Bound<'_> {
    fn computation(
        &self,
        _principal: &galadril_ontology::authorization::Principal,
    ) -> Option<
        std::sync::Arc<galadril_ontology::authorization::ExecutionContext>,
    > {
        Some(std::sync::Arc::clone(&self.context))
    }

    fn verify(&self, access: &Access) -> Option<Evidence> {
        let mut evidence = self.runtime.verify(access)?;
        if self.body {
            let uid = cedar_policy::EntityUid::from_type_name_and_id(
                self.runtime
                    .contract
                    .descriptor(&access.route)?
                    .cedar_resource_type()
                    .clone(),
                cedar_policy::EntityId::new(access.instance.object_id()?),
            );
            evidence.entities.retain(|entity| entity.uid() != uid);
            evidence.entities.push(
                cedar_policy::Entity::new(
                    uid,
                    [
                        (
                            "visibility".into(),
                            cedar_policy::RestrictedExpression::new_string(
                                "open".into(),
                            ),
                        ),
                        (
                            "body".into(),
                            cedar_policy::RestrictedExpression::new_string(
                                "sensitive".into(),
                            ),
                        ),
                    ]
                    .into(),
                    Default::default(),
                )
                .ok()?,
            );
        }
        Some(evidence)
    }
}

fn context(
    runtime: &base::Runtime<'_>,
    access: &Access,
    maximum: galadril_ontology::Classification,
) -> Result<
    std::sync::Arc<galadril_ontology::authorization::ExecutionContext>,
    base::TestError,
> {
    use galadril_ontology::authorization::{
        Clearance, ExecutionContext, Flow,
    };
    let principal = runtime.verify(access).ok_or("evidence")?.principal;
    let mut clearance = Clearance::new(maximum);
    clearance
        .compartments
        .extend(runtime.contract.routes().flat_map(|(_, descriptor)| {
            descriptor.flow().compartments.iter().cloned()
        }));
    Ok(std::sync::Arc::new(ExecutionContext::from_inputs(
        &principal,
        clearance,
        Flow::new(galadril_ontology::Classification::Public),
    )?))
}

#[tokio::test]
async fn a_denied_policy_observation_still_taints_its_control_result()
-> base::TestResult {
    use galadril_ontology::Classification;
    let project = base::project(0, false)?;
    let mut environment = base::environment(&project)?;
    let symbol = base::symbol(&project, "Document")?;
    let cedar = environment.cedar.as_mut().ok_or("cedar")?;
    cedar
        .resources
        .get_mut(&symbol)
        .ok_or("resource")?
        .fields
        .insert("body".parse()?);
    cedar.policies = format!("{} forbid(principal, action, resource is Domain::Document) when {{ resource.body == \"sensitive\" }};", base::POLICIES).into();
    let compiled = compile(&project, &environment);
    let contract = compiled
        .publishable()
        .ok_or_else(|| format!("{:?}", compiled.issues()))?;
    let runtime = base::runtime(contract);
    let read = base::access(
        &project,
        "Document",
        Some("title"),
        "d1",
        Operation::Read,
    )?;
    let scope = Bound {
        runtime: &runtime,
        context: context(&runtime, &read, Classification::Restricted)?,
        body: true,
    };
    assert_eq!(
        authorize(contract, &read, &scope, &Grant).await,
        Decision::Deny(Denial::Cedar)
    );
    assert_eq!(
        scope.context.current_label()?.classification,
        Classification::Confidential
    );
    Ok(())
}

#[tokio::test]
async fn writes_are_committed_only_while_the_full_gate_is_held()
-> base::TestResult {
    use galadril_ontology::Classification;
    use galadril_ontology::authorization::{Flow, perform};
    let project = fixture::project()?;
    let environment = fixture::environment(&project)?;
    let compiled = compile(&project, &environment);
    let contract = compiled.publishable().ok_or("contract")?;
    let runtime = base::runtime(contract);
    let write = base::access(
        &project,
        "Profile",
        Some("title"),
        "schema",
        Operation::Write,
    )?;
    let scope = Bound {
        runtime: &runtime,
        context: context(&runtime, &write, Classification::Restricted)?,
        body: false,
    };
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("public.json");
    std::fs::write(&path, "before")?;
    perform(contract, &write, &scope, &Grant, || async {
        std::fs::write(&path, "public update")
    })
    .await?;
    assert_eq!(std::fs::read_to_string(&path)?, "public update");
    scope
        .context
        .include_input(&Flow::new(Classification::Restricted))?;
    assert!(
        perform(contract, &write, &scope, &Grant, || async {
            std::fs::write(&path, "forbidden")
        })
        .await
        .is_err()
    );
    assert_eq!(std::fs::read_to_string(&path)?, "public update");
    Ok(())
}

#[path = "support/ifc_fixture.rs"]
mod ifc_fixture;

#[tokio::test]
async fn action_inputs_must_not_reclassify_their_write_destination()
-> base::TestResult {
    use galadril_ontology::Classification;
    let project = ifc_fixture::project()?;
    let environment = ifc_fixture::environment(&project)?;
    let compiled = compile(&project, &environment);
    let contract = compiled
        .publishable()
        .ok_or_else(|| format!("{:?}", compiled.issues()))?;
    let target = base::access(
        &project,
        "Profile",
        Some("a_public"),
        "record",
        Operation::Write,
    )?;
    assert_eq!(
        contract
            .flow(&target.route.target)
            .ok_or("destination label")?
            .classification,
        Classification::Public
    );
    Ok(())
}

#[tokio::test]
async fn a_blind_write_is_not_an_observation_of_its_destination()
-> base::TestResult {
    let project =
        ifc_fixture::project_with_effects("write(record.restricted)")?;
    let environment = ifc_fixture::environment(&project)?;
    let compiled = compile(&project, &environment);
    let contract = compiled
        .publishable()
        .ok_or_else(|| format!("{:?}", compiled.issues()))?;
    let action =
        base::access(&project, "Validate", None, "job", Operation::Execute)?;
    let target = base::access(
        &project,
        "Profile",
        Some("restricted"),
        "record",
        Operation::Write,
    )?;
    assert!(
        !contract
            .descriptor(&action.route)
            .ok_or("action")?
            .dependencies()
            .iter()
            .any(|route| route.target == target.route.target &&
                route.operation == Operation::Read)
    );
    Ok(())
}

#[tokio::test]
async fn a_new_task_cannot_reset_inherited_information_or_failure()
-> base::TestResult {
    use galadril_ontology::Classification;
    use galadril_ontology::authorization::{Flow, FlowError};
    let project = fixture::project()?;
    let environment = fixture::environment(&project)?;
    let compiled = compile(&project, &environment);
    let contract = compiled.publishable().ok_or("contract")?;
    let runtime = base::runtime(contract);
    let write = base::access(
        &project,
        "Profile",
        Some("title"),
        "schema",
        Operation::Write,
    )?;
    let parent = context(&runtime, &write, Classification::Restricted)?;
    parent.include_input(&Flow::new(Classification::Restricted))?;
    let child = parent.fork(&Flow::new(Classification::Public))?;
    let child = Bound {
        runtime: &runtime,
        context: child,
        body: false,
    };
    assert_eq!(
        authorize(contract, &write, &child, &Grant).await,
        Decision::Deny(Denial::Flow)
    );
    let low = context(&runtime, &write, Classification::Public)?;
    assert_eq!(
        low.include_input(&Flow::new(Classification::Restricted)),
        Err(FlowError::Clearance)
    );
    assert!(low.current_label().is_err());
    assert!(low.fork(&Flow::new(Classification::Public)).is_err());
    Ok(())
}

struct SensitiveContext<'a>(&'a Bound<'a>);
impl TrustedRuntime for SensitiveContext<'_> {
    fn computation(
        &self,
        principal: &galadril_ontology::authorization::Principal,
    ) -> Option<
        std::sync::Arc<galadril_ontology::authorization::ExecutionContext>,
    > {
        self.0.computation(principal)
    }

    fn verify(&self, access: &Access) -> Option<Evidence> {
        let mut evidence = self.0.verify(access)?;
        evidence.context_flow = galadril_ontology::authorization::Flow::new(
            galadril_ontology::Classification::Confidential,
        );
        Some(evidence)
    }
}

#[tokio::test]
async fn contextual_inputs_are_part_of_the_information_flow()
-> base::TestResult {
    use galadril_ontology::Classification;
    let project = fixture::project()?;
    let environment = fixture::environment(&project)?;
    let compiled = compile(&project, &environment);
    let contract = compiled.publishable().ok_or("contract")?;
    let runtime = base::Runtime {
        region: "blocked",
        ..base::runtime(contract)
    };
    let read = base::access(
        &project,
        "Profile",
        Some("title"),
        "schema",
        Operation::Read,
    )?;
    let mut environment = environment;
    environment.routes.insert(
        galadril_ontology::authorization::Route::new(
            base::symbol(&project, "Profile")?,
            Operation::Read,
        ),
        ["read_meta".parse()?].into(),
    );
    environment
        .routes
        .insert(read.route.clone(), ["read_meta".parse()?].into());
    let compiled = compile(&project, &environment);
    let contract = compiled.publishable().ok_or("contract")?;
    let runtime = base::Runtime {
        contract,
        versions: contract.versions().clone(),
        ..runtime
    };
    let bound = Bound {
        runtime: &runtime,
        context: context(&runtime, &read, Classification::Restricted)?,
        body: false,
    };
    assert_eq!(
        authorize(contract, &read, &SensitiveContext(&bound), &Grant).await,
        Decision::Deny(Denial::Cedar)
    );
    assert_eq!(
        bound.context.current_label()?.classification,
        Classification::Confidential
    );
    Ok(())
}

#[tokio::test]
async fn a_parent_write_grant_cannot_replace_protected_children()
-> base::TestResult {
    use galadril_ontology::authorization::perform;
    let project = fixture::project()?;
    let compiled = compile(&project, &fixture::environment(&project)?);
    let contract = compiled.publishable().ok_or("contract")?;
    let runtime = base::runtime(contract);
    let write = base::access(
        &project,
        "Profile",
        Some("details"),
        "schema",
        Operation::Write,
    )?;
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("private_note");
    std::fs::write(&path, "protected")?;
    assert!(
        perform(contract, &write, &runtime, &Grant, || async {
            std::fs::write(&path, "replaced")
        })
        .await
        .is_err()
    );
    assert_eq!(std::fs::read_to_string(&path)?, "protected");
    Ok(())
}

#[tokio::test]
async fn a_private_storage_error_is_not_an_unlabelled_result()
-> base::TestResult {
    use galadril_ontology::authorization::perform;
    let project =
        ifc_fixture::project_with_effects("write(record.restricted)")?;
    let compiled = compile(&project, &ifc_fixture::environment(&project)?);
    let contract = compiled.publishable().ok_or("contract")?;
    let runtime = base::runtime(contract);
    let write = base::access(
        &project,
        "Profile",
        Some("restricted"),
        "record",
        Operation::Write,
    )?;
    let received = perform(contract, &write, &runtime, &Grant, || async {
        Err::<(), _>(std::io::Error::other("sensitive constraint"))
    })
    .await;
    assert!(
        received.is_ok(),
        "the status must remain inside an opaque, labeled receipt"
    );
    Ok(())
}

struct LiveClearance<'a>(&'a Bound<'a>);
impl TrustedRuntime for LiveClearance<'_> {
    fn computation(
        &self,
        principal: &galadril_ontology::authorization::Principal,
    ) -> Option<
        std::sync::Arc<galadril_ontology::authorization::ExecutionContext>,
    > {
        self.0.computation(principal)
    }

    fn verify(&self, access: &Access) -> Option<Evidence> {
        let mut evidence = self.0.verify(access)?;
        evidence.max_clearance =
            galadril_ontology::authorization::Clearance::new(
                galadril_ontology::Classification::Public,
            );
        Some(evidence)
    }
}

#[tokio::test]
async fn a_clearance_revocation_blocks_effects_in_a_warm_calculation()
-> base::TestResult {
    use galadril_ontology::Classification;
    use galadril_ontology::authorization::{Flow, perform};
    let project = fixture::project()?;
    let compiled = compile(&project, &fixture::environment(&project)?);
    let contract = compiled.publishable().ok_or("contract")?;
    let runtime = base::runtime(contract);
    let write = base::access(
        &project,
        "Profile",
        Some("title"),
        "schema",
        Operation::Write,
    )?;
    let bound = Bound {
        runtime: &runtime,
        context: context(&runtime, &write, Classification::Restricted)?,
        body: false,
    };
    bound
        .context
        .include_input(&Flow::new(Classification::Confidential))?;
    let effect = std::sync::atomic::AtomicBool::new(false);
    assert!(
        perform(contract, &write, &LiveClearance(&bound), &Grant, || async {
            effect.store(true, std::sync::atomic::Ordering::Relaxed);
            Ok::<_, std::io::Error>(())
        })
        .await
        .is_err()
    );
    assert!(!effect.load(std::sync::atomic::Ordering::Relaxed));
    assert!(bound.context.current_label().is_err());
    Ok(())
}

#[tokio::test]
async fn private_metadata_candidates_do_not_poison_public_schema_filtering()
-> base::TestResult {
    use galadril_ontology::Classification;
    use galadril_ontology::authorization::{Flow, FlowError};
    use galadril_ontology::export::{
        ExportOptions, ExportRequest, ExternalMappings, ProjectExporter,
        SchemaPart,
    };
    let project = ifc_fixture::project()?;
    let exporter =
        ProjectExporter::new(&project, &ifc_fixture::environment(&project)?)?;
    let runtime = base::runtime(exporter.contract());
    let access = base::access(
        &project,
        "Profile",
        None,
        "schema",
        Operation::Describe,
    )?;
    let bound = Bound {
        runtime: &runtime,
        context: context(&runtime, &access, Classification::Public)?,
        body: false,
    };
    let options = ExportOptions::default();
    let mappings = ExternalMappings::default();
    let selection = |names: &[&str]| -> Result<_, base::TestError> {
        Ok(galadril_ontology::export::Projection::new(
            galadril_ontology::export::Selection::new(
                base::access(
                    &project,
                    "Profile",
                    None,
                    "schema",
                    Operation::Describe,
                )?,
                names
                    .iter()
                    .map(|name| name.parse())
                    .collect::<Result<Vec<_>, _>>()?,
            )?,
        ))
    };
    let private = selection(&["a_public", "z_secret"])?;
    let absent = selection(&["unknown", "a_public"])?;
    let request = ExportRequest {
        projection: &private,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    let filtered = exporter.export_schema(&request, &bound, &Grant).await?;
    let reference = exporter
        .export_schema(
            &ExportRequest {
                projection: &absent,
                ..request
            },
            &bound,
            &Grant,
        )
        .await?;
    assert_eq!(filtered.schema(), reference.schema());
    assert_eq!(
        bound.context.current_label()?,
        Flow::new(Classification::Public)
    );
    assert_ne!(
        bound
            .context
            .include_input(&Flow::new(Classification::Public)),
        Err(FlowError::Poisoned)
    );
    Ok(())
}

#[tokio::test]
async fn receipts_need_fresh_read_permission_and_their_original_calculation()
-> base::TestResult {
    use galadril_ontology::Classification;
    use galadril_ontology::authorization::perform;
    let project =
        ifc_fixture::project_with_effects("write(record.restricted)")?;
    let compiled = compile(&project, &ifc_fixture::environment(&project)?);
    let contract = compiled.publishable().ok_or("contract")?;
    let runtime = base::runtime(contract);
    let write = base::access(
        &project,
        "Profile",
        Some("restricted"),
        "record",
        Operation::Write,
    )?;
    let bound = Bound {
        runtime: &runtime,
        context: context(&runtime, &write, Classification::Restricted)?,
        body: false,
    };
    let receipt = perform(contract, &write, &bound, &Grant, || async {
        Ok::<_, std::io::Error>(17)
    })
    .await?;
    let independent = Bound {
        runtime: &runtime,
        context: context(&runtime, &write, Classification::Restricted)?,
        body: false,
    };
    assert!(
        receipt
            .inspect(contract, &independent, &Grant)
            .await
            .is_err()
    );
    assert!(
        receipt
            .inspect(contract, &LiveClearance(&bound), &Grant)
            .await
            .is_err()
    );
    Ok(())
}

#[tokio::test]
async fn compiled_archives_guard_actual_effects_without_source_loading()
-> base::TestResult {
    use galadril_ontology::archive::{ArchiveReader, compile_archive};
    let project =
        ifc_fixture::project_with_effects("write(record.restricted)")?;
    let environment = ifc_fixture::environment(&project)?;
    let compiled = compile(&project, &environment);
    let contract = compiled.publishable().ok_or("contract")?;
    let archive = compile_archive(&project, &environment)?;
    let reader = ArchiveReader::open(archive.bytes(), archive.pin())?;
    let runtime = base::runtime(contract);
    let write = base::access(
        &project,
        "Profile",
        Some("a_public"),
        "record",
        Operation::Write,
    )?;
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("value");
    let receipt = reader
        .perform(&write, &runtime, &Grant, || async {
            std::fs::write(&path, "public")
        })
        .await?;
    assert!(reader.inspect(&receipt, &runtime, &Grant).await?.is_ok());
    assert_eq!(std::fs::read_to_string(&path)?, "public");
    Ok(())
}

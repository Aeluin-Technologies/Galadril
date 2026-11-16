//! End-to-end disclosure through the real SpiceDB and Cedar engines.

#[path = "support/archive_fixture.rs"]
mod fixture;

use std::sync::Arc;
use std::time::Duration;

pub(crate) use fixture as base;
use fixture::{
    Runtime, TestError, TestResult, access, environment, project, runtime,
};
use galadril_ontology::Reference;
use galadril_ontology::archive::{
    ArchiveReader, Direction, LocalFile, RangeReader, compile_archive,
};
use galadril_ontology::authorization::{
    Access, Consistency, Decision, InstanceEvidence, Operation,
    PermissionCheck, PermissionOutcome, PermissionProvider, Route, authorize,
    compile,
};
use galadril_ontology::semantic::Project;
use serde_json::{Value, json};
use testcontainers::core::{IntoContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{GenericImage, ImageExt};

const HTTP_PORT: u16 = 8443;
const TOKEN: &str = "isolated-gol-fixture-v1";

struct Spice {
    client: reqwest::Client,
    base: String,
}

impl Spice {
    async fn post(
        &self,
        path: &str,
        value: &Value,
    ) -> Result<Value, reqwest::Error> {
        self.client
            .post(format!("{}{path}", self.base))
            .bearer_auth(TOKEN)
            .json(value)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await
    }

    async fn initialize(&self) -> TestResult {
        self.post("/v1/schema/write", &json!({"schema": fixture::SCHEMA}))
            .await?;
        Ok(())
    }

    async fn write(&self, changes: Vec<Value>) -> Result<Box<str>, TestError> {
        let response = self
            .post("/v1/relationships/write", &json!({"updates": changes}))
            .await?;
        Ok(response
            .pointer("/writtenAt/token")
            .and_then(Value::as_str)
            .filter(|token| !token.is_empty())
            .ok_or("missing write revision")?
            .into())
    }
}

impl PermissionProvider for Spice {
    async fn check(&self, request: PermissionCheck<'_>) -> PermissionOutcome {
        let consistency = match request.consistency {
            Consistency::FullyConsistent => json!({"fullyConsistent": true}),
            Consistency::AtLeastAsFresh(token) => {
                json!({"atLeastAsFresh": {"token": token}})
            },
            _ => return PermissionOutcome::Unavailable,
        };
        let values: serde_json::Map<_, _> = request
            .context
            .iter()
            .map(|(key, value)| (key.to_string(), Value::Bool(*value)))
            .collect();
        let payload = json!({
            "resource": {"objectType": request.resource_type, "objectId": request.resource_id},
            "permission": request.permission.as_str(),
            "subject": {"object": {"objectType": request.principal.subject_type, "objectId": request.principal.subject_id}},
            "consistency": consistency,
            "context": values,
        });
        let Ok(result) = self.post("/v1/permissions/check", &payload).await
        else {
            return PermissionOutcome::Unavailable;
        };
        if result
            .pointer("/checkedAt/token")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
        {
            return PermissionOutcome::Unavailable;
        }
        match result.get("permissionship").and_then(Value::as_str) {
            Some("PERMISSIONSHIP_HAS_PERMISSION") => {
                PermissionOutcome::Allow(request.consistency.clone())
            },
            Some("PERMISSIONSHIP_NO_PERMISSION") => PermissionOutcome::Deny,
            Some("PERMISSIONSHIP_CONDITIONAL_PERMISSION") => {
                PermissionOutcome::Conditional
            },
            _ => PermissionOutcome::Unavailable,
        }
    }
}

fn tuple(
    resource: (&str, &str),
    relation: &str,
    subject: (&str, &str),
    member: Option<&str>,
) -> Value {
    json!({
        "resource": {"objectType": resource.0, "objectId": resource.1},
        "relation": relation,
        "subject": {"object": {"objectType": subject.0, "objectId": subject.1}, "optionalRelation": member.unwrap_or("")},
    })
}

fn update(operation: &str, relationship: Value) -> Value {
    json!({"operation": operation, "relationship": relationship})
}

fn object(access: &Access) -> Result<String, TestError> {
    access
        .instance
        .object_id()
        .ok_or_else(|| "invalid fixture instance".into())
}

fn seed(project: &Project) -> Result<Vec<Value>, TestError> {
    let document =
        object(&access(project, "Document", None, "d1", Operation::Read)?)?;
    let anchor = ("company_record", document.as_str());
    let mut relationships = vec![
        tuple(
            ("department", "tenant_a_legal"),
            "parent",
            ("organization", "tenant_a_org"),
            None,
        ),
        tuple(
            ("team", "tenant_a_docs"),
            "parent",
            ("department", "tenant_a_legal"),
            None,
        ),
        tuple(
            ("group", "tenant_a_readers"),
            "team",
            ("team", "tenant_a_docs"),
            None,
        ),
        tuple(
            ("group", "tenant_a_readers"),
            "member",
            ("group", "tenant_a_subgroup"),
            Some("member"),
        ),
        tuple(
            ("group", "tenant_a_subgroup"),
            "member",
            ("group", "tenant_a_nested"),
            Some("member"),
        ),
        tuple(
            ("group", "tenant_a_nested"),
            "member",
            ("user", "tenant_a/nested"),
            None,
        ),
        tuple(anchor, "viewer_group", ("group", "tenant_a_readers"), None),
        tuple(
            anchor,
            "delegate",
            ("service_account", "tenant_a/service"),
            None,
        ),
        tuple(anchor, "delegate", ("user", "tenant_a/delegated"), None),
        tuple(
            anchor,
            "cleared",
            ("service_account", "tenant_a/service"),
            None,
        ),
        tuple(anchor, "reader", ("user", "tenant_a/reader"), None),
        tuple(anchor, "reader", ("user", "tenant_a/uncleared"), None),
        tuple(anchor, "excluded", ("user", "tenant_a/excluded"), None),
    ];
    for name in ["inherited", "blocked", "excluded"] {
        relationships.push(tuple(
            ("organization", "tenant_a_org"),
            "subscriber",
            ("user", &format!("tenant_a/{name}")),
            None,
        ));
    }
    relationships.push(tuple(
        ("organization", "tenant_a_org"),
        "blocked",
        ("user", "tenant_a/blocked"),
        None,
    ));
    for name in [
        "reader",
        "delegated",
        "inherited",
        "nested",
        "blocked",
        "excluded",
        "caveated",
    ] {
        relationships.push(tuple(
            anchor,
            "cleared",
            ("user", &format!("tenant_a/{name}")),
            None,
        ));
    }
    let mut caveat =
        tuple(anchor, "contingent", ("user", "tenant_a/caveated"), None);
    caveat
        .as_object_mut()
        .ok_or("invalid caveat fixture")?
        .insert(
            "optionalCaveat".into(),
            json!({"caveatName": "attestation"}),
        );
    relationships.push(caveat);

    for (name, id) in [
        ("Document", "d1"),
        ("Message", "m1"),
        ("Attached", "a1"),
        ("Organization", "org1"),
        ("RestrictedIndex", "i1"),
        ("Observation", "o1"),
    ] {
        let global =
            object(&access(project, name, None, id, Operation::Read)?)?;
        for relation in ["owner", "cleared"] {
            relationships.push(tuple(
                ("company_record", &global),
                relation,
                ("user", "tenant_a/owner"),
                None,
            ));
        }
        if name == "Attached" || name == "Message" {
            for relation in ["reader", "cleared"] {
                relationships.push(tuple(
                    ("company_record", &global),
                    relation,
                    ("user", "tenant_a/reader"),
                    None,
                ));
            }
        }
    }

    let mut other = access(project, "Document", None, "d1", Operation::Read)?;
    other.instance.tenant = "tenant_b".into();
    let other = object(&other)?;
    for relation in ["owner", "cleared"] {
        relationships.push(tuple(
            ("company_record", &other),
            relation,
            ("user", "tenant_b/owner"),
            None,
        ));
    }

    Ok(relationships
        .into_iter()
        .map(|relationship| update("OPERATION_TOUCH", relationship))
        .collect())
}

async fn parity(
    scenario: &str,
    reader: &ArchiveReader<impl RangeReader>,
    access: &Access,
    runtime: &Runtime<'_>,
    provider: &Spice,
    expected: Decision,
) -> TestResult {
    let baseline =
        authorize(runtime.contract, access, runtime, provider).await;
    let sparse = reader.read(access, runtime, provider).await;
    assert_eq!(baseline, expected, "{scenario}");
    assert_eq!(sparse.is_some(), expected == Decision::Allow, "{scenario}");
    if let Some(record) = sparse {
        assert_eq!(record.target, access.route.target, "{scenario}");
    }
    Ok(())
}

fn dependencies(
    project: &Project,
    item: &Access,
    runtime: &mut Runtime<'_>,
) -> TestResult {
    let descriptor = runtime
        .contract
        .descriptor(&item.route)
        .ok_or("fixture route")?;
    let mut instances = std::collections::BTreeMap::new();
    for route in descriptor.dependencies() {
        let owner = match &route.target {
            Reference::Symbol(symbol) => symbol,
            Reference::Field(field) => &field.owner,
            _ => return Err("unsupported fixture dependency".into()),
        };
        let id = match owner.key().name().as_str() {
            "Document" => "d1",
            "Message" => "m1",
            "Attached" => "a1",
            "RestrictedIndex" => "i1",
            _ => return Err("unexpected fixture dependency".into()),
        };
        let referenced = access(
            project,
            owner.key().name().as_str(),
            None,
            id,
            Operation::Read,
        )?;
        instances.insert(
            Route::new(route.target.clone(), route.operation),
            vec![InstanceEvidence {
                instance: referenced.instance,
                revision: "fixture_data".into(),
                flow: None,
            }],
        );
    }
    runtime.dependencies = Arc::new(instances);
    Ok(())
}

async fn start_spice()
-> Result<(testcontainers::ContainerAsync<GenericImage>, Spice), TestError> {
    let spice_container = GenericImage::new("authzed/spicedb", "v1.56.2")
        .with_exposed_port(HTTP_PORT.tcp())
        .with_wait_for(WaitFor::message_on_stderr(
            "http server started serving",
        ))
        .with_cmd([
            "serve-testing",
            "--http-enabled",
            "--http-addr=:8443",
            "--skip-release-check=true",
        ])
        .with_mapped_port(0, HTTP_PORT.tcp())
        .with_host_config_modifier(|config| {
            for binding in config
                .port_bindings
                .iter_mut()
                .flat_map(|bindings| bindings.values_mut())
                .filter_map(Option::as_mut)
                .flatten()
            {
                binding.host_ip = Some("127.0.0.1".into());
            }
        })
        .start()
        .await?;
    let mapped = spice_container.get_host_port_ipv4(HTTP_PORT).await?;
    let provider = Spice {
        client: reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(3))
            .build()?,
        base: format!("http://127.0.0.1:{mapped}"),
    };
    provider.initialize().await?;
    Ok((spice_container, provider))
}

#[tokio::test]
#[ignore = "runs a pinned local SpiceDB via Docker; execute explicitly"]
async fn docker_spicedb_cedar_ifc_enforce_sparse_disclosure() -> TestResult {
    use galadril_ontology::authorization::Denial;

    let (spice_container, provider) = start_spice().await?;

    let project = project(96, false)?;
    let deployment = environment(&project)?;
    let compilation = compile(&project, &deployment);
    let contract = compilation
        .publishable()
        .ok_or_else(|| format!("{:?}", compilation.issues()))?;
    let artifact = compile_archive(&project, &deployment)?;
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("security.golc");
    std::fs::write(&path, artifact.bytes())?;
    let reader = ArchiveReader::open(LocalFile::open(&path)?, artifact.pin())?;

    let initial = provider.write(seed(&project)?).await?;
    let live = Runtime {
        consistency: Consistency::AtLeastAsFresh(initial),
        ..runtime(contract)
    };
    let select =
        |name, field, id| access(&project, name, field, id, Operation::Read);
    let document = select("Document", None, "d1")?;
    let title = select("Document", Some("title"), "d1")?;
    let body = select("Document", Some("body"), "d1")?;
    let sealed = select("Document", Some("sealed"), "d1")?;
    let mut public_export = access(
        &project,
        "Document",
        Some("title"),
        "d1",
        Operation::Transfer,
    )?;
    public_export.destination = "LLM".parse()?;
    parity(
        "public export before private observations",
        &reader,
        &public_export,
        &live,
        &provider,
        Decision::Allow,
    )
    .await?;
    let mut altered = artifact.bytes().to_vec();
    let body_record = b"s/company.documents|1.0.0|Document|body";
    let offset = altered
        .windows(body_record.len())
        .position(|value| value == body_record)
        .ok_or("protected block in fixture")?;
    *altered.get_mut(offset).ok_or("protected block offset")? ^= 1;
    let damaged = ArchiveReader::open(altered.as_slice(), artifact.pin())?;
    assert_eq!(
        authorize(contract, &body, &live, &provider).await,
        Decision::Allow
    );
    assert!(damaged.read(&body, &live, &provider).await.is_none());
    for (subject, subject_type, expected) in [
        ("owner", "user", Decision::Allow),
        ("reader", "user", Decision::Allow),
        ("inherited", "user", Decision::Allow),
        ("nested", "user", Decision::Allow),
        ("delegated", "user", Decision::Allow),
        ("service", "service_account", Decision::Allow),
        ("uncleared", "user", Decision::Deny(Denial::Relationship)),
        ("excluded", "user", Decision::Deny(Denial::Relationship)),
        ("blocked", "user", Decision::Deny(Denial::Relationship)),
        ("caveated", "user", Decision::Deny(Denial::Conditional)),
        ("random", "user", Decision::Deny(Denial::Relationship)),
    ] {
        parity(
            subject,
            &reader,
            &title,
            &Runtime {
                subject,
                subject_type,
                ..live.clone()
            },
            &provider,
            expected,
        )
        .await?;
    }
    let only_reader = Runtime {
        subject: "reader",
        ..live.clone()
    };
    parity(
        "protected field",
        &reader,
        &body,
        &only_reader,
        &provider,
        Decision::Deny(Denial::Relationship),
    )
    .await?;
    parity(
        "confidential body",
        &reader,
        &body,
        &live,
        &provider,
        Decision::Allow,
    )
    .await?;
    parity(
        "restricted field",
        &reader,
        &sealed,
        &live,
        &provider,
        Decision::Allow,
    )
    .await?;
    let organization = select("Organization", Some("name"), "org1")?;
    parity(
        "internal field",
        &reader,
        &organization,
        &live,
        &provider,
        Decision::Allow,
    )
    .await?;

    for (scenario, context, expected) in [
        (
            "missing MFA",
            Runtime {
                mfa: false,
                ..live.clone()
            },
            Denial::Cedar,
        ),
        (
            "outside office hours",
            Runtime {
                hour: Some(20),
                ..live.clone()
            },
            Denial::Cedar,
        ),
        (
            "blocked location",
            Runtime {
                region: "blocked",
                ..live.clone()
            },
            Denial::Cedar,
        ),
        (
            "sealed resource",
            Runtime {
                visibility: "sealed",
                ..live.clone()
            },
            Denial::Cedar,
        ),
        (
            "missing hour",
            Runtime {
                hour: None,
                ..live.clone()
            },
            Denial::CedarError,
        ),
        (
            "evaluation error despite permit",
            Runtime {
                region: "error",
                ..live.clone()
            },
            Denial::CedarError,
        ),
    ] {
        parity(
            scenario,
            &reader,
            &body,
            &context,
            &provider,
            Decision::Deny(expected),
        )
        .await?;
    }

    for (name, destination, purpose) in [
        ("insufficient clearance", "LowClearance", "Review"),
        ("compartment/redaction", "NoCompartment", "Review"),
        ("purpose", "Internal", "Analytics"),
    ] {
        let mut selected = select("Document", Some("body"), "d1")?;
        selected.destination = destination.parse()?;
        selected.purpose = purpose.parse()?;
        parity(
            name,
            &reader,
            &selected,
            &live,
            &provider,
            Decision::Deny(Denial::Flow),
        )
        .await?;
    }
    let mut external = access(
        &project,
        "Document",
        Some("body"),
        "d1",
        Operation::Transfer,
    )?;
    external.destination = "LLM".parse()?;
    parity(
        "LLM export denied",
        &reader,
        &external,
        &live,
        &provider,
        Decision::Deny(Denial::Flow),
    )
    .await?;
    let mut permissible = access(
        &project,
        "Document",
        Some("title"),
        "d1",
        Operation::Transfer,
    )?;
    permissible.destination = "LLM".parse()?;
    parity(
        "public export retains earlier private observations",
        &reader,
        &permissible,
        &live,
        &provider,
        Decision::Deny(Denial::Flow),
    )
    .await?;

    let mut foreign = select("Document", Some("title"), "d1")?;
    foreign.instance.tenant = "tenant_b".into();
    parity(
        "cross-tenant",
        &reader,
        &foreign,
        &live,
        &provider,
        Decision::Deny(Denial::Resource),
    )
    .await?;
    parity(
        "same ID in authorized second tenant",
        &reader,
        &foreign,
        &Runtime {
            tenant: "tenant_b",
            ..live.clone()
        },
        &provider,
        Decision::Allow,
    )
    .await?;

    let attached = select("Attached", None, "a1")?;
    let hidden = select("Document", None, "ungranted")?;
    let message = select("Message", None, "m1")?;
    let graph = reader
        .links(
            &attached,
            &[message, hidden],
            Direction::Direct,
            &only_reader,
            &provider,
        )
        .await
        .ok_or("direct link lookup")?;
    assert_eq!(graph.len(), 1);
    assert_eq!(
        graph.first().ok_or("message endpoint")?.target,
        select("Message", None, "m1")?.route.target
    );
    let hidden_source = select("Attached", None, "hidden")?;
    let inverse = reader
        .links(
            &document,
            &[attached, hidden_source],
            Direction::Inverse,
            &only_reader,
            &provider,
        )
        .await
        .ok_or("inverse link lookup")?;
    assert_eq!(inverse.len(), 1);

    let message_root = select("Message", None, "m1")?;
    let message_field = select("Message", Some("attachment"), "m1")?;
    let endpoints = [select("Document", None, "d1")?];
    assert!(
        reader
            .links(
                &message_root,
                &endpoints,
                Direction::Direct,
                &only_reader,
                &provider
            )
            .await
            .ok_or("message links")?
            .is_empty()
    );
    assert!(
        reader
            .links(
                &message_field,
                &endpoints,
                Direction::Direct,
                &only_reader,
                &provider
            )
            .await
            .is_none()
    );
    assert_eq!(
        reader
            .links(
                &message_field,
                &endpoints,
                Direction::Direct,
                &live,
                &provider
            )
            .await
            .ok_or("authorized member link")?
            .len(),
        1
    );
    assert!(
        reader
            .links(
                &document,
                &[message_field],
                Direction::Inverse,
                &only_reader,
                &provider
            )
            .await
            .ok_or("protected inverse links")?
            .is_empty()
    );
    let observation = select("Observation", Some("message"), "o1")?;
    assert_eq!(
        reader
            .links(
                &observation,
                &[message_root],
                Direction::Direct,
                &live,
                &provider
            )
            .await
            .ok_or("observation member link")?
            .len(),
        1
    );

    let projection = [
        select("Document", Some("title"), "d1")?,
        select("Document", Some("body"), "d1")?,
    ];
    assert!(
        reader
            .project(&projection, &only_reader, &provider)
            .await
            .is_none()
    );
    let candidates = [
        select("Document", Some("title"), "d1")?,
        select("Document", Some("body"), "d1")?,
        select("Document", Some("missing"), "d1")?,
    ];
    let visible = reader
        .discover(&candidates, &only_reader, &provider)
        .await
        .ok_or("visible symbols")?;
    assert_eq!(visible.len(), 1);
    assert_eq!(
        visible.first().ok_or("visible title")?.target,
        title.route.target
    );

    for (name, field, id) in [
        ("Document", Some("excerpt"), "d1"),
        ("RestrictedIndex", None, "i1"),
    ] {
        let mut selected = select(name, field, id)?;
        parity(
            "missing traversal evidence",
            &reader,
            &selected,
            &live,
            &provider,
            Decision::Deny(Denial::Dependency),
        )
        .await?;
        let mut complete = live.clone();
        dependencies(&project, &selected, &mut complete)?;
        parity(
            "authorized dependency graph",
            &reader,
            &selected,
            &complete,
            &provider,
            Decision::Allow,
        )
        .await?;
        parity(
            "hidden dependency blocked by MFA",
            &reader,
            &selected,
            &Runtime {
                mfa: false,
                ..complete.clone()
            },
            &provider,
            Decision::Deny(Denial::Cedar),
        )
        .await?;
        selected.destination = "LowClearance".parse()?;
        parity(
            "classification retained through implicit/derived flow",
            &reader,
            &selected,
            &complete,
            &provider,
            Decision::Deny(Denial::Flow),
        )
        .await?;
    }

    let mut changed = environment(&project)?;
    let changed_policy = changed.cedar.as_mut().ok_or("fixture cedar")?;
    changed_policy.policies =
        format!("{} forbid(principal, action, resource);", fixture::POLICIES)
            .into();
    changed_policy.revision.sha256 = [0xa5; 32];
    let new_compilation = compile(&project, &changed);
    let new_contract = new_compilation
        .publishable()
        .ok_or_else(|| format!("{:?}", new_compilation.issues()))?;
    let new_archive = compile_archive(&project, &changed)?;
    let new_reader =
        ArchiveReader::open(new_archive.bytes(), new_archive.pin())?;
    let new_runtime = Runtime {
        contract: new_contract,
        versions: new_contract.versions().clone(),
        ..live.clone()
    };
    assert!(reader.read(&title, &new_runtime, &provider).await.is_none());
    parity(
        "updated policy deny",
        &new_reader,
        &title,
        &new_runtime,
        &provider,
        Decision::Deny(Denial::Cedar),
    )
    .await?;

    let doc_id = object(&document)?;
    let revoked = provider
        .write(vec![update(
            "OPERATION_DELETE",
            tuple(
                ("company_record", &doc_id),
                "reader",
                ("user", "tenant_a/reader"),
                None,
            ),
        )])
        .await?;
    let after = Runtime {
        consistency: Consistency::AtLeastAsFresh(revoked),
        ..only_reader
    };
    parity(
        "warm cache revocation at new ZedToken",
        &reader,
        &title,
        &after,
        &provider,
        Decision::Deny(Denial::Relationship),
    )
    .await?;
    parity(
        "invalid opaque ZedToken",
        &reader,
        &title,
        &Runtime {
            consistency: Consistency::AtLeastAsFresh("invalid-token".into()),
            ..live.clone()
        },
        &provider,
        Decision::Deny(Denial::Unavailable),
    )
    .await?;

    spice_container.stop().await?;
    parity(
        "real SpiceDB stopped",
        &reader,
        &title,
        &live,
        &provider,
        Decision::Deny(Denial::Unavailable),
    )
    .await?;
    Ok(())
}

#[cfg(feature = "remote")]
#[path = "support/remote_matrix.rs"]
mod remote_matrix;

#[cfg(all(feature = "remote", feature = "export"))]
#[path = "support/export_matrix.rs"]
mod export_matrix;

#[cfg(all(feature = "remote", feature = "export"))]
#[path = "support/export_fixture.rs"]
mod export_fixture;

#[cfg(all(feature = "remote", feature = "export"))]
#[path = "support/ifc_matrix.rs"]
mod ifc_matrix;

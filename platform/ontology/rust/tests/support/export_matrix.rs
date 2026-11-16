//! Validate remote authorized schemas with real security authorities.

use std::sync::atomic::{AtomicUsize, Ordering};

use galadril_ontology::Declaration;
use galadril_ontology::archive::remote::{RemoteOptions, RemoteStore};
use galadril_ontology::archive::{ArchiveReader, compile_archive};
use galadril_ontology::authorization::{Consistency, Operation};
use galadril_ontology::export::{
    ExportError, ExportOptions, ExportRequest, ExternalMappings,
    GolValidation, GolValidator, Projection, SchemaPart, SchemaProfile,
    Selection,
};
use serde_json::{Value, json};

use super::remote_matrix::rustfs;
use super::{
    Runtime, access, environment, export_fixture as fixture, object, project,
    runtime, seed, start_spice, tuple, update,
};

struct Rules(AtomicUsize);

impl GolValidator for Rules {
    async fn validate(
        &self,
        _target: &galadril_ontology::authorization::Access,
        proposal: &Value,
    ) -> GolValidation {
        self.0.fetch_add(1, Ordering::Relaxed);
        if proposal
            .get("title")
            .and_then(Value::as_str)
            .is_some_and(|title| !title.is_empty())
        {
            GolValidation::Accepted
        } else {
            GolValidation::Rejected
        }
    }
}

fn select(
    project: &galadril_ontology::semantic::Project,
    name: &str,
    id: &str,
    fields: &[&str],
) -> Result<Selection, fixture::base::TestError> {
    Ok(Selection::new(
        access(project, name, None, id, Operation::Describe)?,
        fields
            .iter()
            .map(|name| name.parse())
            .collect::<Result<Vec<_>, _>>()?,
    )?)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "runs pinned RustFS and SpiceDB for authorized JSON contracts"]
async fn docker_export_from_rustfs_preserves_all_security_layers()
-> fixture::base::TestResult {
    let storage = rustfs::start().await?;
    let (_spice_container, spice) = start_spice().await?;
    let project = project(12, false)?;
    let mut config = environment(&project)?;
    fixture::describe_routes(&mut config);
    let archive = compile_archive(&project, &config)?;
    let checked = galadril_ontology::authorization::compile(&project, &config);
    let contract = checked
        .publishable()
        .ok_or_else(|| format!("{:?}", checked.issues()))?;
    let owner = runtime(contract);
    spice.write(seed(&project)?).await?;
    let store = RemoteStore::connect(
        storage.client.clone(),
        rustfs::BUCKET,
        "tenant_a".parse()?,
        "schema.mail".parse()?,
        RemoteOptions::default(),
    )
    .await?;
    let generation = store.publish(&archive, None).await?;
    let remote = store.open().await?;
    assert_eq!(remote.generation(), generation);
    let local = ArchiveReader::open(archive.bytes(), archive.pin())?;
    let mut projection = Projection::new(select(
        &project,
        "Document",
        "d1",
        &["title", "body", "organization", "excerpt"],
    )?);
    let organization = select(&project, "Organization", "org1", &[])?;
    let galadril_ontology::Reference::Symbol(symbol) =
        &organization.access.route.target
    else {
        return Err("invalid organization request".into());
    };
    projection.definitions.insert(symbol.clone(), organization);
    let options = ExportOptions::default();
    let mappings = ExternalMappings::default();
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    let reader_context = Runtime {
        subject: "reader",
        ..owner.clone()
    };
    let public = remote
        .export_schema(&request, &reader_context, &spice)
        .await?;
    let cold = store.metrics();
    assert!(cold.bytes < archive.bytes().len() as u64);
    let public_text = public.schema().to_string();
    for hidden in [
        "body",
        "excerpt",
        "organization",
        "company.organization",
        "MailSecret",
    ] {
        assert!(!public_text.contains(hidden), "{hidden}");
    }
    public.validate_json(&json!({"title": "public title"}))?;
    assert_eq!(
        public.validate_json(&json!({"title": "public", "body": "leak"})),
        Err(ExportError::InvalidJson)
    );
    assert_eq!(
        local
            .export_schema(&request, &reader_context, &spice)
            .await?
            .schema(),
        public.schema()
    );

    let mut clean_projection = Projection::new(select(
        &project,
        "Document",
        "d1",
        &["title", "body"],
    )?);
    let before_grant = ExportRequest {
        projection: &clean_projection,
        ..request
    };
    let before = remote
        .export_schema(&before_grant, &reader_context, &spice)
        .await?;
    let document = access(&project, "Document", None, "d1", Operation::Read)?;
    let id = object(&document)?;
    let relationship = tuple(
        ("company_record", &id),
        "body_reader",
        ("user", "tenant_a/reader"),
        None,
    );
    let grant = spice
        .write(vec![update("OPERATION_TOUCH", relationship.clone())])
        .await?;
    let granted = Runtime {
        consistency: Consistency::AtLeastAsFresh(grant),
        ..reader_context.clone()
    };
    let visible = remote
        .export_schema(&before_grant, &granted, &spice)
        .await?;
    assert!(visible.schema().to_string().contains("body"));
    assert_eq!(
        remote
            .export_schema(
                &before_grant,
                &Runtime {
                    mfa: false,
                    ..granted.clone()
                },
                &spice
            )
            .await?
            .schema(),
        before.schema()
    );
    let revoked = spice
        .write(vec![update("OPERATION_DELETE", relationship)])
        .await?;
    let revoked = Runtime {
        consistency: Consistency::AtLeastAsFresh(revoked),
        ..reader_context.clone()
    };
    let statistics = store.metrics();
    assert_eq!(
        remote
            .export_schema(&before_grant, &revoked, &spice)
            .await?
            .schema(),
        before.schema()
    );
    assert_eq!(store.metrics().requests, statistics.requests);
    println!(
        "export reads: archive_bytes={}, cold_requests={}, cold_bytes={}, warm_extra_requests=0",
        archive.bytes().len(),
        cold.requests,
        cold.bytes
    );

    for denied in [
        Runtime {
            tenant: "tenant_b",
            ..owner.clone()
        },
        Runtime {
            subject: "excluded",
            ..owner.clone()
        },
        Runtime {
            region: "blocked",
            ..owner.clone()
        },
        Runtime {
            region: "error",
            ..owner.clone()
        },
        Runtime {
            hour: None,
            ..owner.clone()
        },
    ] {
        assert!(matches!(
            remote.export_schema(&before_grant, &denied, &spice).await,
            Err(ExportError::Unavailable)
        ));
    }
    clean_projection.root.access.route.operation = Operation::DescribeTransfer;
    clean_projection.root.access.destination = "LLM".parse()?;
    clean_projection.root.fields = ["body".parse()?].into();
    let forbidden = ExportRequest {
        projection: &clean_projection,
        ..request
    };
    assert!(matches!(
        remote.export_schema(&forbidden, &owner, &spice).await,
        Err(ExportError::Unavailable)
    ));

    let typed_project = fixture::project()?;
    assert!(typed_project.modules().values().any(|module| {
        module
            .declarations()
            .iter()
            .any(|(_, value)| matches!(value, Declaration::Invariant(_)))
    }));
    let typed_environment = fixture::environment(&typed_project)?;
    let typed_checked = galadril_ontology::authorization::compile(
        &typed_project,
        &typed_environment,
    );
    let typed_contract = typed_checked
        .publishable()
        .ok_or_else(|| format!("{:?}", typed_checked.issues()))?;
    let typed_runtime = fixture::base::runtime(typed_contract);
    let typed_archive = compile_archive(&typed_project, &typed_environment)?;
    let mut grants = Vec::new();
    for name in ["Profile", "Details", "Status", "Identifier", "Validate"] {
        let instance =
            access(&typed_project, name, None, "schema", Operation::Describe)?;
        let id = object(&instance)?;
        for role in ["owner", "cleared"] {
            grants.push(update(
                "OPERATION_TOUCH",
                tuple(
                    ("company_record", &id),
                    role,
                    ("user", "tenant_a/owner"),
                    None,
                ),
            ));
        }
        for role in ["reader", "cleared"] {
            grants.push(update(
                "OPERATION_TOUCH",
                tuple(
                    ("company_record", &id),
                    role,
                    ("user", "tenant_a/reader"),
                    None,
                ),
            ));
        }
    }
    spice.write(grants).await?;
    let typed_store = RemoteStore::connect(
        storage.client.clone(),
        rustfs::BUCKET,
        "tenant_a".parse()?,
        "schema.typed".parse()?,
        RemoteOptions::default(),
    )
    .await?;
    typed_store.publish(&typed_archive, None).await?;
    let typed = typed_store.open().await?;
    let projection = fixture::projection(
        &typed_project,
        &[
            "title", "count", "maybe", "labels", "status", "created", "peer",
            "details", "secret",
        ],
    )?;
    let mappings = fixture::mappings()?;
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    let reader_role = fixture::base::Runtime {
        subject: "reader",
        ..typed_runtime.clone()
    };
    let schema = typed.export_schema(&request, &reader_role, &spice).await?;
    assert!(!schema.schema().to_string().contains("private_note"));
    assert!(!schema.schema().to_string().contains("secret"));
    let data = json!({
        "title": "Valid", "count": 2, "labels": [], "status": "Active",
        "created": "2026-11-15T10:20:30Z",
        "details": {"city": "Paris"},
        "peer": {"type": "sample.contracts::Profile@1.0.0", "id": "next"}
    });
    schema.validate_json(&data)?;
    let structured_options = ExportOptions {
        profile: SchemaProfile::OpenAiStrict,
        ..options
    };
    let structured = typed
        .export_schema(
            &ExportRequest {
                options: &structured_options,
                ..request
            },
            &reader_role,
            &spice,
        )
        .await?;
    assert_eq!(
        structured.validate_json(&data),
        Err(ExportError::InvalidJson)
    );
    let mut nullable = data;
    nullable
        .as_object_mut()
        .ok_or("object")?
        .insert("maybe".into(), Value::Null);
    structured.validate_json(&nullable)?;

    let proposal = fixture::projection(&typed_project, &["title"])?;
    let submit = ExportRequest {
        projection: &proposal,
        part: SchemaPart::Value,
        options: &structured_options,
        mappings: &mappings,
    };
    let rules = Rules(AtomicUsize::new(0));
    let empty = json!({"title": ""});
    typed
        .export_schema(&submit, &typed_runtime, &spice)
        .await?
        .validate_json(&empty)?;
    assert_eq!(
        typed
            .validate_proposal(&submit, &empty, &typed_runtime, &spice, &rules)
            .await,
        Err(ExportError::Business)
    );
    assert_eq!(rules.0.load(Ordering::Relaxed), 1);
    assert_eq!(
        typed
            .validate_proposal(
                &submit,
                &json!({"title": "ready"}),
                &reader_role,
                &spice,
                &rules
            )
            .await,
        Err(ExportError::WriteDenied)
    );
    assert_eq!(rules.0.load(Ordering::Relaxed), 1);
    typed
        .validate_proposal(
            &submit,
            &json!({"title": "ready"}),
            &typed_runtime,
            &spice,
            &rules,
        )
        .await?;

    _spice_container.stop().await?;
    assert!(matches!(
        typed.export_schema(&submit, &typed_runtime, &spice).await,
        Err(ExportError::Unavailable)
    ));
    storage.container.stop().await?;
    Ok(())
}

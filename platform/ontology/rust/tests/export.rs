//! Schema projection must not disclose the names of protected members.

use galadril_ontology::export::{ExportOptions, SchemaProfile};

#[path = "support/archive_fixture.rs"]
mod base;

#[test]
fn export_limits_are_always_bounded() {
    let options = ExportOptions {
        max_depth: 0,
        ..ExportOptions::default()
    };
    assert!(options.validate().is_err());
    assert_ne!(SchemaProfile::Standard, SchemaProfile::OpenAiStrict);
}

#[path = "support/export_fixture.rs"]
mod fixture;

use std::sync::atomic::{AtomicBool, Ordering};

use galadril_ontology::archive::{ArchiveReader, compile_archive};
use galadril_ontology::authorization::{
    Operation, PermissionCheck, PermissionOutcome, PermissionProvider,
};
use galadril_ontology::export::{
    ExportError, ExportRequest, ExternalMappings, IntegerEncoding,
    ProjectExporter, Projection, SchemaPart, Selection,
};
use serde_json::json;

struct Rebac(AtomicBool);

fn send_future<T: Send>(future: T) -> T {
    future
}

#[tokio::test]
async fn archived_descriptions_hide_denied_property_names()
-> fixture::base::TestResult {
    let project = fixture::project()?;
    let environment = fixture::environment(&project)?;
    let archive = compile_archive(&project, &environment)?;
    let reader = ArchiveReader::open(archive.bytes(), archive.pin())?;
    let exporter = ProjectExporter::new(&project, &environment)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(false));
    let projection = fixture::projection(&project, &["title", "secret"])?;
    let description = reader
        .describe(&projection, &runtime, &provider)
        .await
        .ok_or("description")?;
    assert_eq!(description.properties.len(), 1);
    assert_eq!(
        description
            .properties
            .first()
            .ok_or("property")?
            .name
            .as_str(),
        "title"
    );
    provider.0.store(true, Ordering::Relaxed);
    assert_eq!(
        reader
            .describe(&projection, &runtime, &provider)
            .await
            .ok_or("authorized description")?
            .properties
            .len(),
        2
    );
    provider.0.store(false, Ordering::Relaxed);
    assert_eq!(
        reader
            .describe(&projection, &runtime, &provider)
            .await
            .ok_or("revoked description")?
            .properties
            .len(),
        1
    );
    Ok(())
}

impl PermissionProvider for Rebac {
    async fn check(&self, request: PermissionCheck<'_>) -> PermissionOutcome {
        if request.permission.as_str() == "view_body" &&
            !self.0.load(Ordering::Relaxed)
        {
            PermissionOutcome::Deny
        } else {
            PermissionOutcome::Allow(request.consistency.clone())
        }
    }
}

#[tokio::test]
async fn schema_exports_hide_denied_metadata_and_match_archived_ir()
-> fixture::base::TestResult {
    let project = fixture::project()?;
    let environment = fixture::environment(&project)?;
    let exporter = ProjectExporter::new(&project, &environment)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(false));
    let projection = fixture::projection(
        &project,
        &[
            "title", "count", "maybe", "labels", "created", "status", "peer",
            "details", "secret", "unknown",
        ],
    )?;
    let options = ExportOptions::default();
    let mappings = fixture::mappings()?;
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    let projected =
        send_future(exporter.export_schema(&request, &runtime, &provider))
            .await?;
    let text = projected.schema().to_string();
    assert!(!text.contains("secret"));
    assert!(!text.contains("private_note"));
    assert!(!text.contains("unknown"));
    let value = json!({
        "title": "visible", "count": 255, "labels": ["étiquette"], "status": "Active",
        "created": "2026-11-15T12:00:00.123456789Z",
        "peer": {"type": "sample.contracts::Profile@1.0.0", "id": "profile_1"},
        "details": {"city": "Paris"}
    });
    projected.validate_json(&value)?;
    let mut invalid = value.clone();
    invalid
        .as_object_mut()
        .ok_or("object")?
        .insert("secret".into(), json!("hidden"));
    assert_eq!(
        projected.validate_json(&invalid),
        Err(ExportError::InvalidJson)
    );
    invalid = value.clone();
    invalid
        .as_object_mut()
        .ok_or("object")?
        .insert("count".into(), json!(256));
    assert_eq!(
        projected.validate_json(&invalid),
        Err(ExportError::InvalidJson)
    );
    invalid = value.clone();
    invalid
        .as_object_mut()
        .ok_or("object")?
        .insert("created".into(), json!("tomorrow"));
    assert_eq!(
        projected.validate_json(&invalid),
        Err(ExportError::InvalidJson)
    );

    let archive = compile_archive(&project, &environment)?;
    let reader = ArchiveReader::open(archive.bytes(), archive.pin())?;
    assert_eq!(
        reader
            .export_schema(&request, &runtime, &provider)
            .await?
            .schema(),
        projected.schema()
    );
    assert_eq!(
        exporter
            .export_schema(&request, &runtime, &provider)
            .await?
            .schema(),
        projected.schema()
    );
    let reversed = fixture::project_with_order(true)?;
    let reversed_environment = fixture::environment(&reversed)?;
    let reversed_exporter =
        ProjectExporter::new(&reversed, &reversed_environment)?;
    let reversed_runtime =
        fixture::base::runtime(reversed_exporter.contract());
    let reversed_projection = fixture::projection(
        &reversed,
        &[
            "title", "count", "maybe", "labels", "created", "status", "peer",
            "details", "secret", "unknown",
        ],
    )?;
    assert_eq!(
        reversed_exporter
            .export_schema(
                &ExportRequest {
                    projection: &reversed_projection,
                    ..request
                },
                &reversed_runtime,
                &provider
            )
            .await?
            .schema(),
        projected.schema()
    );

    provider.0.store(true, Ordering::Relaxed);
    let visible = reader.export_schema(&request, &runtime, &provider).await?;
    assert!(visible.schema().to_string().contains("private_note"));
    assert!(visible.schema().to_string().contains("secret"));
    provider.0.store(false, Ordering::Relaxed);
    assert_eq!(
        reader
            .export_schema(&request, &runtime, &provider)
            .await?
            .schema(),
        projected.schema()
    );

    let llm = ExportOptions {
        profile: SchemaProfile::OpenAiStrict,
        ..options
    };
    let structured = reader
        .export_schema(
            &ExportRequest {
                options: &llm,
                ..request
            },
            &runtime,
            &provider,
        )
        .await?;
    assert_eq!(
        structured.validate_json(&value),
        Err(ExportError::InvalidJson)
    );
    let mut nullable = value;
    nullable
        .as_object_mut()
        .ok_or("object")?
        .insert("maybe".into(), json!(null));
    structured.validate_json(&nullable)?;
    Ok(())
}

#[tokio::test]
async fn wide_numeric_ranges_require_explicit_representations()
-> fixture::base::TestResult {
    let project = fixture::project()?;
    let config = fixture::environment(&project)?;
    let exporter = ProjectExporter::new(&project, &config)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(true));
    let projection = fixture::projection(&project, &["large"])?;
    let options = ExportOptions::default();
    let mappings = ExternalMappings::default();
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    assert!(matches!(
        exporter.export_schema(&request, &runtime, &provider).await,
        Err(ExportError::Precision)
    ));
    let exact = ExportOptions {
        integers: IntegerEncoding::DecimalStrings,
        ..options
    };
    let schema = exporter
        .export_schema(
            &ExportRequest {
                options: &exact,
                ..request
            },
            &runtime,
            &provider,
        )
        .await?;
    schema.validate_json(&json!({"large": u128::MAX.to_string()}))?;
    for value in ["340282366920938463463374607431768211456", "-1", "01", "1e3"]
    {
        assert_eq!(
            schema.validate_json(&json!({"large": value})),
            Err(ExportError::InvalidJson)
        );
    }
    Ok(())
}

#[tokio::test]
async fn action_contracts_are_closed_without_executing_actions()
-> fixture::base::TestResult {
    let project = fixture::project()?;
    let config = fixture::environment(&project)?;
    let exporter = ProjectExporter::new(&project, &config)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(true));
    let mut projection = fixture::projection(&project, &[])?;
    projection.root = galadril_ontology::export::Selection::new(
        fixture::base::access(
            &project,
            "Validate",
            None,
            "schema",
            Operation::Describe,
        )?,
        ["title".parse()?, "again".parse()?, "state".parse()?],
    )?;
    let options = ExportOptions {
        profile: SchemaProfile::OpenAiStrict,
        ..ExportOptions::default()
    };
    let mappings = ExternalMappings::default();
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::ActionInput,
        options: &options,
        mappings: &mappings,
    };
    let input = exporter
        .export_schema(&request, &runtime, &provider)
        .await?;
    input.validate_json(
        &json!({"title": "ok", "again": null, "state": "Active"}),
    )?;
    assert_eq!(
        input.validate_json(&json!({"title": "ok", "state": "Active"})),
        Err(ExportError::InvalidJson)
    );
    let output = exporter
        .export_schema(
            &ExportRequest {
                part: SchemaPart::ActionOutput,
                ..request
            },
            &runtime,
            &provider,
        )
        .await?;
    output.validate_json(&json!({"result": true}))?;
    let mut denied = fixture::projection(&project, &[])?;
    denied.root = galadril_ontology::export::Selection::new(
        fixture::base::access(
            &project,
            "Validate",
            None,
            "schema",
            Operation::Describe,
        )?,
        ["title".parse()?, "again".parse()?, "state".parse()?],
    )?;
    denied.definitions.clear();
    assert!(matches!(
        exporter
            .export_schema(
                &ExportRequest {
                    projection: &denied,
                    ..request
                },
                &runtime,
                &provider
            )
            .await,
        Err(ExportError::Unavailable)
    ));
    Ok(())
}

#[cfg(feature = "outlines-compat")]
#[tokio::test]
async fn outlines_accepts_the_constrained_schema() -> fixture::base::TestResult
{
    let project = fixture::project()?;
    let config = fixture::environment(&project)?;
    let exporter = ProjectExporter::new(&project, &config)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(false));
    let projection = fixture::projection(
        &project,
        &["title", "status", "maybe", "count", "labels"],
    )?;
    let options = ExportOptions {
        profile: SchemaProfile::Outlines,
        integers: IntegerEncoding::DecimalStrings,
        ..ExportOptions::default()
    };
    let mappings = ExternalMappings::default();
    let document = exporter
        .export_schema(
            &ExportRequest {
                projection: &projection,
                part: SchemaPart::Value,
                options: &options,
                mappings: &mappings,
            },
            &runtime,
            &provider,
        )
        .await?;
    let regex = outlines_core::json_schema::regex_from_value(
        document.schema(),
        None,
        None,
    )?;
    assert!(!regex.is_empty());
    document.validate_json(&json!({"title": "Hello", "status": "Paused", "count": "12", "labels": []}))?;
    Ok(())
}

#[tokio::test]
async fn ordinary_value_grants_do_not_authorize_schema_description()
-> fixture::base::TestResult {
    use galadril_ontology::export::{Projection, Selection};

    let project = fixture::base::project(0, false)?;
    let mut config = fixture::base::environment(&project)?;
    let root = || -> Result<_, fixture::base::TestError> {
        Ok(Projection::new(Selection::new(
            fixture::base::access(
                &project,
                "Document",
                None,
                "d1",
                Operation::Describe,
            )?,
            ["title".parse()?],
        )?))
    };
    let projection = root()?;
    let mappings = ExternalMappings::default();
    let options = ExportOptions::default();
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    let exporter = ProjectExporter::new(&project, &config)?;
    let provider = Rebac(AtomicBool::new(true));
    assert!(matches!(
        exporter
            .export_schema(
                &request,
                &fixture::base::runtime(exporter.contract()),
                &provider
            )
            .await,
        Err(ExportError::Unavailable)
    ));
    fixture::describe_routes(&mut config);
    let exporter = ProjectExporter::new(&project, &config)?;
    let schema = exporter
        .export_schema(
            &request,
            &fixture::base::runtime(exporter.contract()),
            &provider,
        )
        .await?;
    schema.validate_json(&json!({"title": "visible"}))?;
    Ok(())
}

#[tokio::test]
async fn unsupported_profiles_and_missing_mappings_fail_explicitly()
-> fixture::base::TestResult {
    use galadril_ontology::export::{ExternalMapping, MappingPrecision};
    use galadril_ontology::{Version, VersionedName};

    let project = fixture::project()?;
    let config = fixture::environment(&project)?;
    let exporter = ProjectExporter::new(&project, &config)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(true));
    let mappings = ExternalMappings::default();
    let options = ExportOptions::default();
    let projection = fixture::projection(&project, &["created"])?;
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    assert!(matches!(
        exporter.export_schema(&request, &runtime, &provider).await,
        Err(ExportError::Mapping)
    ));
    let signature =
        VersionedName::new("mapping.checked".parse()?, Version::new(1, 0, 0));
    assert!(
        ExternalMapping::new(
            json!({"$ref": "https://invalid.example/schema"}),
            signature.clone(),
            MappingPrecision::Exact
        )
        .is_err()
    );
    assert!(
        ExternalMapping::new(
            json!({"type": "integer"}),
            signature.clone(),
            MappingPrecision::Exact
        )
        .is_err()
    );
    let mapping = ExternalMapping::new(
        json!({"type": "string", "format": "date-time"}),
        signature.clone(),
        MappingPrecision::Exact,
    )?;
    let mut registry = ExternalMappings::default();
    registry.register(fixture::instant()?, mapping)?;
    assert!(
        registry
            .register(
                fixture::instant()?,
                ExternalMapping::new(
                    json!({"type": "string"}),
                    signature,
                    MappingPrecision::Exact
                )?
            )
            .is_err()
    );
    let mut competing = ExternalMappings::default();
    competing.register(fixture::instant()?, ExternalMapping::new(
        json!({"type": "string", "enum": ["ok", "invalid"], "pattern": "^ok$"}),
        VersionedName::new("mapping.enum".parse()?, Version::new(1, 0, 0)),
        MappingPrecision::Exact,
    )?)?;
    let outlines = ExportOptions {
        profile: SchemaProfile::Outlines,
        ..ExportOptions::default()
    };
    assert!(matches!(
        exporter
            .export_schema(
                &ExportRequest {
                    options: &outlines,
                    mappings: &competing,
                    ..request
                },
                &runtime,
                &provider
            )
            .await,
        Err(ExportError::Profile)
    ));
    for (fields, profile, expected) in [
        (
            &["initial"][..],
            SchemaProfile::OpenAiStrict,
            ExportError::Profile,
        ),
        (
            &["count"][..],
            SchemaProfile::Outlines,
            ExportError::Profile,
        ),
        (
            &["ratio"][..],
            SchemaProfile::Standard,
            ExportError::Precision,
        ),
    ] {
        let projection = fixture::projection(&project, fields)?;
        let options = ExportOptions {
            profile,
            ..ExportOptions::default()
        };
        assert!(matches!(
            exporter.export_schema(&ExportRequest { projection: &projection, options: &options, ..request }, &runtime, &provider).await,
            Err(error) if error == expected,
        ));
    }
    Ok(())
}

#[tokio::test]
async fn property_names_are_not_profile_keywords() -> fixture::base::TestResult
{
    let project = fixture::project()?;
    let config = fixture::environment(&project)?;
    let exporter = ProjectExporter::new(&project, &config)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let projection = fixture::projection(&project, &["minimum", "status"])?;
    let provider = Rebac(AtomicBool::new(false));
    let mappings = ExternalMappings::default();
    for profile in [SchemaProfile::Outlines, SchemaProfile::OpenAiStrict] {
        let options = ExportOptions {
            profile,
            ..ExportOptions::default()
        };
        let schema = exporter
            .export_schema(
                &ExportRequest {
                    projection: &projection,
                    part: SchemaPart::Value,
                    options: &options,
                    mappings: &mappings,
                },
                &runtime,
                &provider,
            )
            .await?;
        schema.validate_json(
            &json!({"minimum": "visible", "status": "Paused"}),
        )?;
        assert!(
            schema
                .schema()
                .get("$defs")
                .and_then(|defs| defs.get("sample.contracts::Status@1.0.0"))
                .is_some()
        );
    }
    Ok(())
}

#[tokio::test]
async fn optional_aliases_and_projection_limits_are_preserved()
-> fixture::base::TestResult {
    let project = fixture::project()?;
    let config = fixture::environment(&project)?;
    let exporter = ProjectExporter::new(&project, &config)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(false));
    let projection = fixture::projection(&project, &["alias"])?;
    let options = ExportOptions::default();
    let mappings = ExternalMappings::default();
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    let schema = exporter
        .export_schema(&request, &runtime, &provider)
        .await?;
    schema.validate_json(&json!({}))?;
    schema.validate_json(&json!({"alias": null}))?;
    schema.validate_json(&json!({"alias": "name"}))?;
    let projection = fixture::projection(&project, &["title", "count"])?;
    let options = ExportOptions {
        max_properties: 1,
        ..options
    };
    assert!(matches!(
        exporter
            .export_schema(
                &ExportRequest {
                    projection: &projection,
                    options: &options,
                    ..request
                },
                &runtime,
                &provider
            )
            .await,
        Err(ExportError::Limit)
    ));
    Ok(())
}

#[tokio::test]
async fn integer_encodings_accept_exactly_the_rust_domain()
-> fixture::base::TestResult {
    let project = fixture::project()?;
    let config = fixture::environment(&project)?;
    let exporter = ProjectExporter::new(&project, &config)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let projection = fixture::projection(&project, &["count", "signed"])?;
    let provider = Rebac(AtomicBool::new(false));
    let mappings = ExternalMappings::default();
    for integers in [
        IntegerEncoding::SafeNumbers,
        IntegerEncoding::DecimalStrings,
    ] {
        let options = ExportOptions {
            integers,
            ..ExportOptions::default()
        };
        let schema = exporter
            .export_schema(
                &ExportRequest {
                    projection: &projection,
                    part: SchemaPart::Value,
                    options: &options,
                    mappings: &mappings,
                },
                &runtime,
                &provider,
            )
            .await?;
        for number in -140..=300 {
            let encode = |value: i32| match integers {
                IntegerEncoding::SafeNumbers => json!(value),
                IntegerEncoding::DecimalStrings => json!(value.to_string()),
            };
            assert_eq!(
                schema
                    .validate_json(
                        &json!({"count": encode(number), "signed": encode(0)})
                    )
                    .is_ok(),
                u8::try_from(number).is_ok()
            );
            assert_eq!(
                schema
                    .validate_json(
                        &json!({"count": encode(0), "signed": encode(number)})
                    )
                    .is_ok(),
                i8::try_from(number).is_ok()
            );
        }
    }
    Ok(())
}

struct Business(AtomicBool);

struct UnavailableWriteAuthority;

impl PermissionProvider for UnavailableWriteAuthority {
    async fn check(&self, request: PermissionCheck<'_>) -> PermissionOutcome {
        if request.permission.as_str() == "edit" {
            PermissionOutcome::Unavailable
        } else {
            PermissionOutcome::Allow(request.consistency.clone())
        }
    }
}

impl galadril_ontology::export::GolValidator for Business {
    async fn validate(
        &self,
        _target: &galadril_ontology::authorization::Access,
        _proposal: &serde_json::Value,
    ) -> galadril_ontology::export::GolValidation {
        self.0.store(true, Ordering::Relaxed);
        galadril_ontology::export::GolValidation::Accepted
    }
}

#[tokio::test]
async fn structured_assignments_need_child_write_enforcement()
-> fixture::base::TestResult {
    let project = fixture::project()?;
    let config = fixture::environment(&project)?;
    let exporter = ProjectExporter::new(&project, &config)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(false));
    let projection = fixture::projection(&project, &["details"])?;
    let options = ExportOptions::default();
    let mappings = ExternalMappings::default();
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    let candidate = json!({"details": {"city": "Paris"}});
    exporter
        .export_schema(&request, &runtime, &provider)
        .await?
        .validate_json(&candidate)?;
    let business = Business(AtomicBool::new(false));
    assert_eq!(
        exporter
            .validate_proposal(
                &request, &candidate, &runtime, &provider, &business
            )
            .await,
        Err(ExportError::WriteDenied)
    );
    assert!(!business.0.load(Ordering::Relaxed));
    Ok(())
}

#[tokio::test]
async fn a_worker_proposal_uses_the_concrete_write_access()
-> fixture::base::TestResult {
    let source = galadril_ontology::syntax::parse_source(
        "worker.gol",
        r#"ontology sample.worker version "1.0.0" {
            entity Profile {
                field title: String;
                field revision: u64;
                field embedding: List<f64>;
                field offset: Option<f64>;
                field nickname: OptionalName;
            }
            type OptionalName = Option<String>;
            access OptionalName {
                resource: "company_record";
                permission read_meta { relation: "view"; cedar: "Domain::Action::\"optionalName\""; }
            }
            access Profile {
                resource: "company_record";
                permission read_meta { relation: "view"; cedar: "Domain::Action::\"profile\""; }
                permission export_meta { relation: "export"; cedar: "Domain::Action::\"exportProfile\""; }
                permission write_profile { relation: "edit"; cedar: "Domain::Action::\"writeProfile\""; }
            }
        }"#,
    )?;
    let types = galadril_ontology::ExternalTypeRegistry::default();
    let project = galadril_ontology::semantic::analyze_project(
        &[source],
        &galadril_ontology::semantic::Environment::new(&types),
    )?;
    let environment = fixture::environment(&project)?;
    let archive = compile_archive(&project, &environment)?;
    let reader = ArchiveReader::open(archive.bytes(), archive.pin())?;
    let exporter = ProjectExporter::new(&project, &environment)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(true));
    let describe = || {
        fixture::base::access(
            &project,
            "Profile",
            None,
            "schema",
            Operation::Describe,
        )
    };
    let projection = Projection::new(Selection::new(
        describe()?,
        ["title", "revision", "embedding"]
            .map(str::parse)
            .into_iter()
            .collect::<Result<Vec<_>, _>>()?,
    )?);
    let partial =
        Projection::new(Selection::new(describe()?, ["title".parse()?])?);
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &ExportOptions {
            allow_float_rounding: true,
            integers: IntegerEncoding::DecimalStrings,
            ..ExportOptions::default()
        },
        mappings: &ExternalMappings::default(),
    };
    let write = fixture::base::access(
        &project,
        "Profile",
        None,
        "profile_1",
        Operation::Write,
    )?;
    let business = Business(AtomicBool::new(false));
    assert_eq!(
        reader
            .validate_proposal_for(
                &ExportRequest {
                    projection: &partial,
                    ..request
                },
                &json!({"title":"example"}),
                &write,
                &runtime,
                &provider,
                &business,
            )
            .await,
        Err(ExportError::InvalidJson)
    );
    assert!(!business.0.load(Ordering::Relaxed));
    let proposal = json!({
        "title": "example",
        "revision": u64::MAX.to_string(),
        "embedding": [0.25, 0.5]
    });
    reader
        .validate_proposal_for(
            &request, &proposal, &write, &runtime, &provider, &business,
        )
        .await?;
    assert!(business.0.load(Ordering::Relaxed));

    business.0.store(false, Ordering::Relaxed);
    let result = reader
        .validate_proposal_for(
            &request,
            &proposal,
            &write,
            &runtime,
            &UnavailableWriteAuthority,
            &business,
        )
        .await;
    assert_eq!(result, Err(ExportError::AuthorizationUnavailable));
    assert!(!business.0.load(Ordering::Relaxed));
    Ok(())
}

#[tokio::test]
async fn a_metadata_projection_cannot_rebind_a_write_to_another_tenant()
-> fixture::base::TestResult {
    let project = fixture::project()?;
    let environment = fixture::environment(&project)?;
    let archive = compile_archive(&project, &environment)?;
    let reader = ArchiveReader::open(archive.bytes(), archive.pin())?;
    let exporter = ProjectExporter::new(&project, &environment)?;
    let runtime = fixture::base::runtime(exporter.contract());
    let provider = Rebac(AtomicBool::new(false));
    let projection = fixture::projection(&project, &["title"])?;
    let options = ExportOptions::default();
    let mappings = ExternalMappings::default();
    let request = ExportRequest {
        projection: &projection,
        part: SchemaPart::Value,
        options: &options,
        mappings: &mappings,
    };
    let mut instance = projection.root.access.instance.clone();
    instance.tenant = "another_tenant".into();
    let write = galadril_ontology::authorization::Access {
        route: galadril_ontology::authorization::Route::new(
            projection.root.access.route.target.clone(),
            Operation::Write,
        ),
        instance,
        destination: projection.root.access.destination.clone(),
        purpose: projection.root.access.purpose.clone(),
    };
    let business = Business(AtomicBool::new(false));
    assert_eq!(
        reader
            .validate_proposal_for(
                &request,
                &json!({"title":"example"}),
                &write,
                &runtime,
                &provider,
                &business
            )
            .await,
        Err(ExportError::Unavailable)
    );
    assert!(!business.0.load(Ordering::Relaxed));
    Ok(())
}

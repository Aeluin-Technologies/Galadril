//! Source validation keeps unfinished drafts separate from publishable GOL.

use std::error::Error;

use galadril_ontology::ExternalTypeRegistry;
use galadril_ontology::semantic::Environment;
use galadril_registry::gol::{GolFile, analyze_files, validate_files};

#[tokio::test]
async fn a_new_tenant_has_an_empty_catalog_without_provisioning_storage()
-> anyhow::Result<()> {
    use galadril_registry::domain::Registry;
    use galadril_registry::storage::RevisionStore as _;

    let store = memory::MemoryStore::default();
    let registry = Registry::new(store.clone());
    assert!(
        registry
            .list_gol_ontologies("tenant_a", 10)
            .await?
            .is_empty()
    );
    assert!(!store.tenant_exists("tenant_a").await?);
    assert!(
        registry
            .list_gol_ontologies("../invalid", 10)
            .await
            .is_err()
    );
    Ok(())
}

#[tokio::test]
async fn plugin_installation_is_idempotent_and_preserves_tenant_edits()
-> anyhow::Result<()> {
    use galadril_registry::domain::Registry;
    use galadril_registry::gol_service::{GolPackageOrigin, SaveGolOntology};

    let registry = Registry::new(memory::MemoryStore::default());
    let origin = GolPackageOrigin {
        plugin_id: "fixture".into(),
        plugin_version: "2.0.0".into(),
        package_version: "1.0.0".into(),
    };
    let source = "ontology fixture.contract version \"1.0.0\" {}";
    let write = |text: &str, expected_revision, origin| SaveGolOntology {
        tenant_id: "tenant_a",
        ontology_id: "fixture.contract",
        display_name: "Fixture",
        files: vec![GolFile {
            path: "contract.gol".into(),
            text: text.into(),
        }],
        author: "alice",
        expected_revision,
        origin,
    };
    let installed = registry
        .save_gol_ontology(write(source, None, Some(origin.clone())))
        .await?;
    let retried = registry
        .save_gol_ontology(write(source, None, Some(origin.clone())))
        .await?;
    assert_eq!(
        retried.summary.source_revision,
        installed.summary.source_revision
    );
    assert_eq!(retried.summary.origin.as_ref(), Some(&origin));
    assert!(retried.summary.active_generation.is_none());
    assert!(
        registry
            .list_gol_ontologies("tenant_b", 10)
            .await?
            .is_empty()
    );

    let edited = registry
        .save_gol_ontology(write(
            "ontology fixture.contract version \"1.0.1\" {}",
            Some(&installed.summary.source_revision),
            None,
        ))
        .await?;
    assert_eq!(edited.summary.origin.as_ref(), Some(&origin));
    assert!(
        registry
            .save_gol_ontology(write(source, None, Some(origin)))
            .await
            .is_err()
    );
    let current = registry
        .gol_workspace("tenant_a", "fixture.contract", None)
        .await?;
    assert_eq!(
        current.summary.source_revision,
        edited.summary.source_revision
    );
    Ok(())
}

#[tokio::test]
async fn plugin_upgrade_is_compare_and_swap_on_unmodified_sources()
-> anyhow::Result<()> {
    use galadril_registry::domain::Registry;
    use galadril_registry::gol_service::{GolPackageOrigin, SaveGolOntology};

    let registry = Registry::new(memory::MemoryStore::default());
    let write =
        |version: &str, name, revision, packaged: bool| SaveGolOntology {
            tenant_id: "tenant_a",
            ontology_id: "fixture.contract",
            display_name: name,
            files: vec![GolFile {
                path: "contract.gol".into(),
                text: format!(
                    "ontology fixture.contract version \"{version}\" {{}}"
                ),
            }],
            author: "alice",
            expected_revision: revision,
            origin: packaged.then(|| GolPackageOrigin {
                plugin_id: "fixture".into(),
                plugin_version: version.into(),
                package_version: version.into(),
            }),
        };
    let install = registry
        .save_gol_ontology(write("1.0.0", "Initial", None, true))
        .await?;
    let renamed = registry
        .save_gol_ontology(write(
            "1.0.0",
            "Tenant name",
            Some(&install.summary.source_revision),
            false,
        ))
        .await?;
    let updated = registry
        .save_gol_ontology(write(
            "1.1.0",
            "New package title",
            Some(&renamed.summary.source_revision),
            true,
        ))
        .await?;
    assert_eq!(updated.summary.display_name, "Tenant name");
    let previous = registry
        .gol_workspace(
            "tenant_a",
            "fixture.contract",
            Some(&renamed.summary.source_revision),
        )
        .await?;
    assert_eq!(
        previous
            .summary
            .origin
            .as_ref()
            .map(|origin| origin.package_version.as_str()),
        Some("1.0.0")
    );
    let customized = registry
        .save_gol_ontology(write(
            "1.1.1",
            "Tenant name",
            Some(&updated.summary.source_revision),
            false,
        ))
        .await?;
    assert!(
        registry
            .save_gol_ontology(write(
                "1.2.0",
                "Update",
                Some(&customized.summary.source_revision),
                true
            ))
            .await
            .is_err()
    );
    Ok(())
}

#[test]
fn metadata_grants_share_the_canonical_authorization_id_encoding() {
    use galadril_registry::gol::authorization_object_id;
    assert_eq!(
        authorization_object_id("tenant/communication.mail"),
        "tenant/communication=2Email"
    );
    assert_eq!(
        authorization_object_id("tenant/already=2Eencoded"),
        "tenant/already=3D2Eencoded"
    );
    assert!(matches!(
        authorization_object_id("tenant/user_1"),
        std::borrow::Cow::Borrowed(_)
    ));
}

#[test]
fn registered_time_type_is_explicit_and_has_an_exact_json_contract()
-> Result<(), Box<dyn Error>> {
    use galadril_registry::gol::{
        registered_external_types, registered_json_mappings,
    };
    let types = registered_external_types()?;
    let temporal = types
        .iter()
        .find(|(id, _)| id.name().to_string() == "gal.time.Instant")
        .ok_or("registered temporal contract missing")?;
    assert!(!temporal.1.capabilities.is_empty());
    registered_json_mappings()?;
    Ok(())
}

#[test]
fn source_paths_are_bounded_relative_gol_files() -> Result<(), Box<dyn Error>>
{
    validate_files(&[GolFile {
        path: "communication/mail.gol".into(),
        text: "ontology communication.mail version \"1.0.0\" {}".into(),
    }])?;
    for path in [
        "/mail.gol",
        "../mail.gol",
        "a/../mail.gol",
        "a//mail.gol",
        "mail.json",
        "a\\mail.gol",
    ] {
        assert!(
            validate_files(&[GolFile {
                path: path.into(),
                text: String::new()
            }])
            .is_err()
        );
    }
    assert!(validate_files(&[]).is_err());
    assert!(
        validate_files(&[
            GolFile {
                path: "mail.gol".into(),
                text: String::new()
            },
            GolFile {
                path: "mail.gol".into(),
                text: String::new()
            }
        ])
        .is_err()
    );
    Ok(())
}

#[test]
fn malformed_drafts_return_source_located_diagnostics()
-> Result<(), Box<dyn Error>> {
    let registry = ExternalTypeRegistry::default();
    let environment = Environment::new(&registry);
    let result = analyze_files(&[GolFile {
        path: "mail.gol".into(),
        text: "ontology communication.mail version \"1.0.0\" { entity Message { field title: ; } }".into(),
    }], &environment);
    let diagnostics = result.err().ok_or("invalid draft was admitted")?;
    assert!(diagnostics.iter().any(|item| item.path == "mail.gol" &&
        item.code.starts_with("gol::") &&
        item.end >= item.start));
    Ok(())
}

#[test]
fn imports_are_checked_by_the_gol_compiler() -> Result<(), Box<dyn Error>> {
    let registry = ExternalTypeRegistry::default();
    let environment = Environment::new(&registry);
    let source = GolFile {
        path: "mail.gol".into(),
        text: "ontology communication.mail version \"1.0.0\" { entity Message { field title: String; } }".into(),
    };
    let project = analyze_files(&[source], &environment)
        .map_err(|_| "GOL compilation failed")?;
    assert_eq!(project.modules().len(), 1);
    Ok(())
}

#[tokio::test]
async fn unfinished_sources_can_be_saved_but_not_published_without_authorities()
-> Result<(), Box<dyn Error>> {
    use galadril_registry::domain::Registry;
    use galadril_registry::gol_service::SaveGolOntology;
    let registry = Registry::new(memory::MemoryStore::default());
    let saved = registry
        .save_gol_ontology(SaveGolOntology {
            tenant_id: "tenant-a",
            ontology_id: "communication.mail",
            display_name: "Mail",
            files: vec![GolFile {
                path: "mail.gol".into(),
                text: "ontology unfinished".into(),
            }],
            author: "alice",
            expected_revision: None,
            origin: None,
        })
        .await?;
    let workspace = registry
        .gol_workspace(
            "tenant-a",
            "communication.mail",
            Some(&saved.summary.source_revision),
        )
        .await?;
    assert_eq!(
        workspace.files.first().map(|file| file.text.as_str()),
        Some("ontology unfinished")
    );
    let preview = registry
        .preview_gol_dependencies(
            "tenant-a",
            "communication.mail",
            &saved.summary.source_revision,
            &[],
        )
        .await?;
    let approval = galadril_registry::gol_service::GolCompileApproval {
        plan_pin: preview.plan_pin.ok_or("missing plan pin")?,
        approved_owners: Vec::new(),
    };
    let validation = registry
        .validate_gol_ontology(
            "tenant-a",
            "communication.mail",
            &saved.summary.source_revision,
            &approval,
        )
        .await?;
    assert_eq!(validation.status, "invalid");
    assert!(!validation.diagnostics.is_empty());
    assert!(
        registry
            .publish_gol_ontology(
                "tenant-a",
                "communication.mail",
                &saved.summary.source_revision,
                None,
                "alice",
                &approval,
            )
            .await
            .is_err()
    );
    assert!(
        registry
            .gol_workspace("tenant-b", "communication.mail", None)
            .await
            .is_err()
    );
    Ok(())
}

#[path = "support/memory.rs"]
mod memory;

#[tokio::test]
async fn a_companion_module_import_blocks_deleting_its_source_package()
-> Result<(), Box<dyn Error>> {
    use galadril_registry::domain::Registry;
    use galadril_registry::gol_service::SaveGolOntology;
    let registry = Registry::new(memory::MemoryStore::default());
    registry.save_gol_ontology(SaveGolOntology {tenant_id:"tenant_a",ontology_id:"workspace.bundle",display_name:"Bundle",files:vec![GolFile {path:"bundle.gol".into(),text:"ontology workspace.bundle version \"1.0.0\" {} ontology workspace.companion version \"1.0.0\" {}".into()}],author:"alice",expected_revision:None,origin:None}).await?;
    let consumer=registry.save_gol_ontology(SaveGolOntology {tenant_id:"tenant_a",ontology_id:"workspace.consumer",display_name:"Consumer",files:vec![GolFile {path:"consumer.gol".into(),text:"ontology workspace.consumer version \"1.0.0\" { import workspace.companion version \"1.0.0\"; }".into()}],author:"alice",expected_revision:None,origin:None}).await?;
    let deleted = registry
        .delete_gol_ontology(
            "tenant_a",
            "workspace.bundle",
            &consumer.summary.source_revision,
            "alice",
        )
        .await;
    assert!(
        deleted.is_err_and(|error| error
            .to_string()
            .contains("source dependents"))
    );
    assert!(
        registry
            .gol_workspace("tenant_a", "workspace.bundle", None)
            .await
            .is_ok()
    );
    Ok(())
}

#[tokio::test]
async fn deleting_pipeline_releases_only_current_bindings_and_preserves_pinned_history()
-> Result<(), Box<dyn Error>> {
    use galadril_registry::domain::{PutPipeline, Registry};
    use galadril_registry::state::GolBindingRecord;
    use galadril_registry::storage::RevisionStore;
    let store = memory::MemoryStore::default();
    let registry = Registry::new(store.clone());
    let pipeline=registry.put_pipeline(PutPipeline {tenant_id:"tenant_a",pipeline_id:"mail",name:"Mail",owner_id:"alice",definition:serde_json::json!({"sources":[{"id":"input"}],"pipeline":[{"step":"sink","type":"sink","input_from":["input"]}]}),author:"alice",message:"Create",expected_revision_id:None,create_only:true}).await?;
    let mut snapshot = store
        .load("tenant_a", Some(&pipeline.head_revision_id))
        .await?;
    snapshot.state.bindings.insert(
        "fixture".into(),
        GolBindingRecord {
            pipeline_id: "mail".into(),
            block_id: "sink".into(),
            ontology_id: "communication.mail".into(),
            generation: "a".repeat(32),
            source_revision: "b".repeat(40),
            symbols: vec!["communication.mail::Message".into()],
            updated_at_ms: 0,
        },
    );
    let bound = store
        .commit(
            "tenant_a",
            &snapshot.revision_id,
            &snapshot.state,
            "fixture",
            "Bind",
        )
        .await?;
    registry.delete_pipeline("tenant_a", "mail", &bound).await?;
    assert!(
        registry
            .list_bindings("tenant_a", None, None, 100)
            .await?
            .is_empty()
    );
    assert_eq!(
        registry
            .list_bindings("tenant_a", None, Some(&bound), 100)
            .await?
            .len(),
        1
    );
    Ok(())
}

#[tokio::test]
async fn runtime_binding_resolves_compiled_step_from_deployed_root()
-> anyhow::Result<()> {
    use galadril_registry::domain::{PutPipeline, Registry};
    use galadril_registry::gol_service::SaveGolOntology;
    use galadril_registry::state::{GolBindingRecord, TenantState};
    use galadril_registry::storage::RevisionStore;
    use serde_json::json;

    let tenant = "tenant_a";
    let store = memory::MemoryStore::default();
    let registry = Registry::new(store.clone());
    registry.save_gol_ontology(SaveGolOntology {
        tenant_id: tenant,
        ontology_id: "communication.mail",
        display_name: "Mail",
        files: vec![GolFile {
            path: "mail.gol".into(),
            text: "ontology communication.mail version \"1.0.0\" { entity Message { field title: String; } }".into(),
        }],
        author: "alice",
        expected_revision: None,
        origin: None,
    }).await?;
    let child_definition = json!({
        "sources": [{"id": "input"}],
        "pipeline": [{"step": "sink", "type": "sink", "input_from": ["input"], "ontology_required": true}]
    });
    let child = registry
        .put_pipeline(PutPipeline {
            tenant_id: tenant,
            pipeline_id: "child",
            name: "Child",
            owner_id: "alice",
            definition: child_definition,
            author: "alice",
            message: "Create",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    let mut snapshot =
        store.load(tenant, Some(&child.head_revision_id)).await?;
    snapshot.state.bindings.insert(
        TenantState::binding_key("child", "sink"),
        GolBindingRecord {
            pipeline_id: "child".into(),
            block_id: "sink".into(),
            ontology_id: "communication.mail".into(),
            generation: "a".repeat(32),
            source_revision: "b".repeat(40),
            symbols: vec!["communication.mail::Message".into()],
            updated_at_ms: 0,
        },
    );
    let child_revision = store
        .commit(
            tenant,
            &snapshot.revision_id,
            &snapshot.state,
            "alice",
            "Bind",
        )
        .await?;
    assert!(
        registry
            .gol_binding(tenant, "child", &child_revision, "sink")
            .await
            .is_err()
    );
    assert!(
        registry
            .publish_pipeline(tenant, "child", &child_revision)
            .await
            .is_err(),
        "well-formed pins require a published archive"
    );
    assert_eq!(
        registry
            .current_pipeline(tenant, "child")
            .await?
            .head_revision_id,
        child_revision
    );
    mark_fixture_deployed(&store, tenant, "child", &child_revision).await?;
    let root = registry.put_pipeline(PutPipeline {
        tenant_id: tenant,
        pipeline_id: "root",
        name: "Root",
        owner_id: "alice",
        definition: json!({
            "sources": [{"id": "input"}],
            "pipeline": [{"step": "transfer", "type": "pipeline", "input_from": ["input"], "pipeline_ref": {
                "pipeline_id": "child", "revision_id": child_revision, "input_source": "input"
            }}]
        }),
        author: "alice",
        message: "Compose",
        expected_revision_id: None,
        create_only: true,
    }).await?;
    assert!(
        registry
            .publish_pipeline(tenant, "root", &root.head_revision_id)
            .await
            .is_err(),
        "a composed release also requires the child's archive"
    );
    mark_fixture_deployed(&store, tenant, "root", &root.head_revision_id)
        .await?;
    let binding = registry
        .gol_binding(tenant, "root", &root.head_revision_id, "__linked_1")
        .await?;
    assert_eq!(binding.pipeline_id, "child");
    assert_eq!(binding.block_id, "sink");
    assert_eq!(binding.generation, "a".repeat(32));
    assert!(
        registry
            .gol_binding(tenant, "root", &root.head_revision_id, "sink")
            .await
            .is_err()
    );
    assert!(
        registry
            .gol_binding(
                "tenant_b",
                "root",
                &root.head_revision_id,
                "__linked_1"
            )
            .await
            .is_err()
    );
    let head = store.load(tenant, None).await?.revision_id;
    let head = registry.delete_pipeline(tenant, "child", &head).await?;
    let deletion = registry
        .delete_gol_ontology(tenant, "communication.mail", &head, "alice")
        .await;
    assert!(deletion.is_err_and(|error| {
        error.to_string().contains("pipeline dependents")
    }));
    let head = registry.delete_pipeline(tenant, "root", &head).await?;
    registry
        .delete_gol_ontology(tenant, "communication.mail", &head, "alice")
        .await?;
    Ok(())
}

async fn mark_fixture_deployed(
    store: &memory::MemoryStore,
    tenant: &str,
    pipeline: &str,
    revision: &str,
) -> anyhow::Result<()> {
    use anyhow::Context as _;
    use galadril_registry::storage::RevisionStore as _;

    let mut snapshot = store.load(tenant, Some(revision)).await?;
    snapshot
        .state
        .pipelines
        .get_mut(pipeline)
        .context("fixture pipeline")?
        .published_revision_id = Some(revision.to_owned());
    store
        .commit(
            tenant,
            revision,
            &snapshot.state,
            "fixture",
            "Simulate an earlier deployment",
        )
        .await?;
    Ok(())
}

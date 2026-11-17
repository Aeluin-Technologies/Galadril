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
    registry.save_gol_ontology(SaveGolOntology {tenant_id:"tenant_a",ontology_id:"workspace.bundle",display_name:"Bundle",files:vec![GolFile {path:"bundle.gol".into(),text:"ontology workspace.bundle version \"1.0.0\" {} ontology workspace.companion version \"1.0.0\" {}".into()}],author:"alice",expected_revision:None}).await?;
    let consumer=registry.save_gol_ontology(SaveGolOntology {tenant_id:"tenant_a",ontology_id:"workspace.consumer",display_name:"Consumer",files:vec![GolFile {path:"consumer.gol".into(),text:"ontology workspace.consumer version \"1.0.0\" { import workspace.companion version \"1.0.0\"; }".into()}],author:"alice",expected_revision:None}).await?;
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

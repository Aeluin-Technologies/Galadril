//! Source RPC authority is independent of a workload's pipeline read access.

use std::sync::Arc;

use anyhow::Result;
use galadril_registry::domain::Registry;
use galadril_registry::gol::GolFile;
use galadril_registry::gol_service::SaveGolOntology;
use galadril_registry::grpc::RegistryGrpc;
use galadril_registry::proto::registry_server::Registry as RegistryRpc;
use galadril_registry::proto::{self};
use tonic::{Code, Request};

#[path = "support/memory.rs"]
mod memory;

#[tokio::test]
async fn a_loopback_workload_cannot_read_source_without_the_gateway_credential()
-> Result<()> {
    let registry = Arc::new(Registry::new(memory::MemoryStore::default()));
    registry
        .save_gol_ontology(SaveGolOntology {
            tenant_id: "tenant_a",
            ontology_id: "workspace.private",
            display_name: "Private",
            files: vec![GolFile {
                path: "private.gol".into(),
                text: "ontology workspace.private version \"1.0.0\" {}".into(),
            }],
            author: "alice",
            expected_revision: None,
            origin: None,
        })
        .await?;
    let request = || {
        Request::new(proto::GetGolWorkspaceRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "workspace.private".into(),
            source_revision: None,
        })
    };
    let disabled = RegistryGrpc::new(registry.clone());
    assert_eq!(
        disabled
            .get_gol_workspace(request())
            .await
            .err()
            .map(|error| error.code()),
        Some(Code::PermissionDenied)
    );
    let enabled = RegistryGrpc::new(registry).with_gateway_key(&[7u8; 32])?;
    assert_eq!(
        enabled
            .get_gol_workspace(request())
            .await
            .err()
            .map(|error| error.code()),
        Some(Code::PermissionDenied)
    );
    let mut wrong = request();
    wrong
        .metadata_mut()
        .insert("x-galadril-registry-key", "00".repeat(32).parse()?);
    assert_eq!(
        enabled
            .get_gol_workspace(wrong)
            .await
            .err()
            .map(|error| error.code()),
        Some(Code::PermissionDenied)
    );
    let mut duplicate = request();
    duplicate
        .metadata_mut()
        .append("x-galadril-registry-key", "07".repeat(32).parse()?);
    duplicate
        .metadata_mut()
        .append("x-galadril-registry-key", "07".repeat(32).parse()?);
    assert_eq!(
        enabled
            .get_gol_workspace(duplicate)
            .await
            .err()
            .map(|error| error.code()),
        Some(Code::PermissionDenied)
    );
    let mut admitted = request();
    admitted
        .metadata_mut()
        .insert("x-galadril-registry-key", "07".repeat(32).parse()?);
    assert_eq!(
        enabled
            .get_gol_workspace(admitted)
            .await?
            .into_inner()
            .files
            .len(),
        1
    );
    Ok(())
}

#[tokio::test]
async fn dependency_preview_does_not_open_an_unapproved_import() -> Result<()>
{
    let registry = Registry::new(memory::MemoryStore::default());
    let source=registry.save_gol_ontology(SaveGolOntology {tenant_id:"tenant_a",ontology_id:"workspace.public",display_name:"Public",files:vec![GolFile {path:"public.gol".into(),text:"ontology workspace.public version \"1.0.0\" { import workspace.secret version \"1.0.0\"; entity Public { field value: workspace.secret.Hidden; } }".into()}],author:"alice",expected_revision:None,origin:None}).await?;
    let preview = registry
        .preview_gol_dependencies(
            "tenant_a",
            "workspace.public",
            &source.summary.source_revision,
            &[],
        )
        .await?;
    assert_eq!(preview.required_owners, vec!["workspace.secret"]);
    assert!(preview.plan_pin.is_none());
    assert!(preview.dependencies.is_empty());
    Ok(())
}

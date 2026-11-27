//! Exercises Registry gRPC against real lakeFS and S3 test containers.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use aws_sdk_s3::Client as S3Client;
use aws_sdk_s3::config::{Credentials, Region};
use galadril_ontology::archive::remote::{
    Generation, RemoteOptions, RemoteStore,
};
use galadril_ontology::authorization::Instance;
use galadril_registry::domain::Registry;
use galadril_registry::gol::{
    GolAuthorityConfig, GolFile, registered_external_types,
};
use galadril_registry::gol_service::{GolPublicationConfig, SaveGolOntology};
use galadril_registry::grpc::{RegistryGrpc, registry_client_with_key};
use galadril_registry::proto::registry_server::RegistryServer;
use galadril_registry::proto::{self};
use galadril_registry::storage::{LakeFsConfig, LakeFsStore, S3Config};
use reqwest::Client as HttpClient;
use secrecy::SecretString;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use testcontainers::core::{IntoContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{GenericImage, ImageExt};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::Server;

#[path = "../src/metadata.rs"]
mod metadata;

const APPROVAL: &str =
    include_str!("../../../infrastructure/gol/authorities.json");
const SCHEMA: &str = include_str!("../../../schemas/spicedb/schema.zed");
const SPICE_TOKEN: &str = "registry-isolated-publication-test";

async fn spice_post(base: &str, path: &str, body: Value) -> Result<Value> {
    Ok(HttpClient::builder()
        .no_proxy()
        .timeout(Duration::from_secs(5))
        .build()?
        .post(format!("{base}{path}"))
        .bearer_auth(SPICE_TOKEN)
        .json(&body)
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?)
}

fn relationship(
    kind: &str,
    id: &str,
    relation: &str,
    subject_type: &str,
    subject_id: &str,
) -> Value {
    json!({"resource":{"objectType":kind,"objectId":id},"relation":relation,"subject":{"object":{"objectType":subject_type,"objectId":subject_id}}})
}

async fn check(base: &str, id: &str, permission: &str) -> Result<bool> {
    let answer=spice_post(base,"/v1/permissions/check",json!({"resource":{"objectType":"gol_resource","objectId":id},"permission":permission,"subject":{"object":{"objectType":"user","objectId":"test"}},"consistency":{"fullyConsistent":true}})).await?;
    Ok(answer.get("permissionship").and_then(Value::as_str) ==
        Some("PERMISSIONSHIP_HAS_PERMISSION"))
}

fn source_store(
    lakefs_endpoint: &str,
    s3_endpoint: &str,
) -> Result<LakeFsStore> {
    LakeFsStore::new(
        LakeFsConfig {
            endpoint: lakefs_endpoint.into(),
            access_key: "registry-access".to_owned(),
            secret_key: SecretString::from("registry-secret"),
            repository_prefix: "galadril".to_owned(),
            storage_namespace: "s3://lake/".to_owned(),
            branch: "main".to_owned(),
        },
        S3Config {
            endpoint: s3_endpoint.into(),
            region: "us-east-1".to_owned(),
            access_key: "minioadmin".to_owned(),
            secret_key: SecretString::from("minioadmin"),
        },
    )
}

async fn concurrent_source_replacements_are_isolated(
    lakefs: &str,
    s3: &str,
    revision: &str,
    source: &str,
) -> Result<()> {
    use galadril_registry::storage::RevisionStore;
    let left_store = source_store(lakefs, s3)?;
    let right_store = source_store(lakefs, s3)?;
    left_store.load("tenant_a", Some(revision)).await?;
    right_store.load("tenant_a", Some(revision)).await?;
    let left = Registry::new(left_store);
    let right = Registry::new(right_store);
    let gate = tokio::sync::Barrier::new(2);
    let left_text =
        format!("{source}\n// writer-left {}\n", "a".repeat(200_000));
    let right_text =
        format!("{source}\n// writer-right {}\n", "b".repeat(200_000));
    let write_left = async {
        gate.wait().await;
        left.save_gol_ontology(SaveGolOntology {
            tenant_id: "tenant_a",
            ontology_id: "people",
            display_name: "Left",
            files: vec![GolFile {
                path: "people.gol".into(),
                text: left_text.clone(),
            }],
            author: "test",
            expected_revision: Some(revision),
            origin: None,
        })
        .await
    };
    let write_right = async {
        gate.wait().await;
        right
            .save_gol_ontology(SaveGolOntology {
                tenant_id: "tenant_a",
                ontology_id: "people",
                display_name: "Right",
                files: vec![GolFile {
                    path: "people.gol".into(),
                    text: right_text.clone(),
                }],
                author: "test",
                expected_revision: Some(revision),
                origin: None,
            })
            .await
    };
    let (left_result, right_result) = tokio::join!(write_left, write_right);
    let expected = match (left_result, right_result) {
        (Ok(_), Err(error)) => {
            ensure!(
                error.to_string().contains("changed"),
                "unexpected losing writer error: {error}"
            );
            ("Left", left_text)
        },
        (Err(error), Ok(_)) => {
            ensure!(
                error.to_string().contains("changed"),
                "unexpected losing writer error: {error}"
            );
            ("Right", right_text)
        },
        _ => anyhow::bail!(
            "concurrent source replacements did not produce exactly one commit and one conflict"
        ),
    };
    let visible = left.gol_workspace("tenant_a", "people", None).await?;
    ensure!(
        visible.summary.display_name == expected.0 &&
            visible
                .files
                .first()
                .is_some_and(|file| file.text == expected.1),
        "source bytes and catalog from different writers were mixed"
    );
    Ok(())
}

#[tokio::test]
async fn grpc_ontology_pipeline_and_tenant_validation_use_lakefs() -> Result<()>
{
    let network = format!("galadril-registry-test-{}", std::process::id());
    let minio_name = format!("galadril-rustfs-{}", std::process::id());
    let minio = GenericImage::new(
        "rustfs/rustfs",
        "1.0.1@sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c",
    )
    .with_exposed_port(9000.tcp())
    .with_wait_for(WaitFor::seconds(5))
    .with_network(&network)
    .with_container_name(&minio_name)
    .with_cmd([
        "rustfs",
        "/data",
    ])
    .with_env_var("RUSTFS_ACCESS_KEY", "minioadmin")
    .with_env_var("RUSTFS_SECRET_KEY", "minioadmin")
    .with_env_var("RUSTFS_CONSOLE_ENABLE", "false")
    .start()
    .await
    .context("RustFS test container failed")?;
    let s3_endpoint =
        format!("http://127.0.0.1:{}", minio.get_host_port_ipv4(9000).await?);
    let s3_client = S3Client::from_conf(
        aws_sdk_s3::config::Builder::new()
            .behavior_version_latest()
            .endpoint_url(&s3_endpoint)
            .region(Region::new("us-east-1"))
            .credentials_provider(Credentials::new(
                "minioadmin",
                "minioadmin",
                None,
                None,
                "testcontainers",
            ))
            .force_path_style(true)
            .build(),
    );
    s3_client.create_bucket().bucket("lake").send().await?;
    s3_client
        .create_bucket()
        .bucket("ontology-artifacts")
        .send()
        .await?;
    let spice = GenericImage::new("authzed/spicedb", "v1.56.2")
        .with_exposed_port(50051.tcp())
        .with_exposed_port(8443.tcp())
        .with_wait_for(WaitFor::message_on_stderr(
            "http server started serving",
        ))
        .with_cmd([
            "serve-testing",
            "--http-enabled",
            "--http-addr=:8443",
            "--skip-release-check=true",
        ])
        .start()
        .await?;
    let spice_http =
        format!("http://127.0.0.1:{}", spice.get_host_port_ipv4(8443).await?);
    spice_post(&spice_http, "/v1/schema/write", json!({"schema":SCHEMA}))
        .await?;
    let owner =
        relationship("ontology", "tenant_a/people", "owner", "user", "test");
    spice_post(&spice_http,"/v1/relationships/write",json!({"updates":[
        {"operation":"OPERATION_TOUCH","relationship":relationship("tenant","tenant_a","member","user","test")},
        {"operation":"OPERATION_TOUCH","relationship":relationship("ontology","tenant_a/people","parent","tenant","tenant_a")},
        {"operation":"OPERATION_TOUCH","relationship":owner.clone()}
    ]})).await?;
    let metadata = metadata::SpiceDbMetadata::connect(
        &format!(
            "http://127.0.0.1:{}",
            spice.get_host_port_ipv4(50051).await?
        ),
        SPICE_TOKEN,
    )
    .await?;

    let lakefs = GenericImage::new("treeverse/lakefs", "1.88.0")
        .with_exposed_port(8000.tcp())
        .with_wait_for(WaitFor::seconds(5))
        .with_network(&network)
        .with_env_var("LAKEFS_DATABASE_TYPE", "local")
        .with_env_var("LAKEFS_BLOCKSTORE_TYPE", "s3")
        .with_env_var(
            "LAKEFS_BLOCKSTORE_S3_ENDPOINT",
            format!("http://{minio_name}:9000"),
        )
        .with_env_var("LAKEFS_BLOCKSTORE_S3_FORCE_PATH_STYLE", "true")
        .with_env_var("LAKEFS_BLOCKSTORE_S3_REGION", "us-east-1")
        .with_env_var(
            "LAKEFS_BLOCKSTORE_S3_CREDENTIALS_ACCESS_KEY_ID",
            "minioadmin",
        )
        .with_env_var(
            "LAKEFS_BLOCKSTORE_S3_CREDENTIALS_SECRET_ACCESS_KEY",
            "minioadmin",
        )
        .with_env_var(
            "LAKEFS_AUTH_ENCRYPT_SECRET_KEY",
            "development-only-lakefs-encryption-key",
        )
        .with_env_var("LAKEFS_INSTALLATION_USER_NAME", "registry")
        .with_env_var("LAKEFS_INSTALLATION_ACCESS_KEY_ID", "registry-access")
        .with_env_var(
            "LAKEFS_INSTALLATION_SECRET_ACCESS_KEY",
            "registry-secret",
        )
        .with_env_var("LAKEFS_STATS_ENABLED", "false")
        .with_env_var("LAKEFS_USAGE_REPORT_ENABLED", "false")
        .start()
        .await
        .context("lakeFS test container failed")?;
    let lakefs_endpoint = format!(
        "http://127.0.0.1:{}",
        lakefs.get_host_port_ipv4(8000).await?
    );
    let store = source_store(&lakefs_endpoint, &s3_endpoint)?;
    let source_probe = source_store(&lakefs_endpoint, &s3_endpoint)?;
    let repository = store.repository_for_tenant("tenant_a")?;
    let approval: GolAuthorityConfig = serde_json::from_str(APPROVAL)?;
    let publication = GolPublicationConfig::new(
        s3_client.clone(),
        "ontology-artifacts",
        approval,
        registered_external_types()?,
        Arc::new(metadata),
    )?;
    let service = RegistryGrpc::new(Arc::new(
        Registry::new(store).with_gol(publication),
    ))
    .with_gateway_key(&[9u8; 32])?;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let server = tokio::spawn(async move {
        Server::builder()
            .add_service(RegistryServer::new(service))
            .serve_with_incoming(TcpListenerStream::new(listener))
            .await
    });
    let mut client =
        registry_client_with_key(&format!("http://{address}"), &[9u8; 32])?;
    let source = r#"ontology people version "1.0.0" {
        entity Person { field name: String; }
        access Person {
            resource: "gol_resource";
            permission read_access { relation: "read"; cedar: "Gol::Action::\"read\""; }
            permission write_access { relation: "write"; cedar: "Gol::Action::\"write\""; }
            permission describe { relation: "describe"; cedar: "Gol::Action::\"describe\""; }
            permission transfer { relation: "transfer"; cedar: "Gol::Action::\"transfer\""; }
        }
    }"#;
    let workspace = client
        .save_gol_ontology(proto::SaveGolOntologyRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            display_name: "People".into(),
            files: vec![proto::GolFile {
                path: "people.gol".into(),
                text: source.into(),
            }],
            author: "test".into(),
            expected_revision: None,
            origin: None,
        })
        .await?
        .into_inner();
    let source_revision = workspace
        .summary
        .context("missing workspace summary")?
        .source_revision;
    let metadata_only = galadril_registry::storage::RevisionStore::load(
        &source_probe,
        "tenant_a",
        Some(&source_revision),
    )
    .await?;
    ensure!(
        metadata_only
            .state
            .ontologies
            .get("people")
            .is_some_and(|record| record.files.is_empty()),
        "pipeline/catalog reads eagerly opened authored GOL sources"
    );
    concurrent_source_replacements_are_isolated(
        &lakefs_endpoint,
        &s3_endpoint,
        &source_revision,
        source,
    )
    .await?;
    let source_metadata = serde_json::to_value(
        metadata_only
            .state
            .ontologies
            .get("people")
            .context("source metadata")?,
    )?;
    ensure!(
        source_metadata
            .get("source_digest")
            .and_then(Value::as_str)
            .is_some_and(|digest| digest.len() == 64),
        "source catalog does not pin immutable content"
    );
    let read = client
        .get_gol_workspace(proto::GetGolWorkspaceRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            source_revision: Some(source_revision.clone()),
        })
        .await?
        .into_inner();
    ensure!(
        read.files.first().is_some_and(
            |file| file.path == "people.gol" && file.text == source
        )
    );
    let preview = client
        .preview_gol_dependencies(proto::PreviewGolDependenciesRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            source_revision: source_revision.clone(),
            approved_owners: Vec::new(),
        })
        .await?
        .into_inner();
    let plan_pin = preview
        .plan_pin
        .context("missing complete dependency plan")?;
    let validation = client
        .validate_gol_ontology(proto::ValidateGolOntologyRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            source_revision: source_revision.clone(),
            plan_pin: plan_pin.clone(),
            approved_owners: Vec::new(),
        })
        .await?
        .into_inner();
    ensure!(validation.status == "valid", "{:?}", validation.diagnostics);
    let published = client
        .publish_gol_ontology(proto::PublishGolOntologyRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            source_revision: source_revision.clone(),
            expected_generation: None,
            author_id: "test".into(),
            plan_pin: plan_pin.clone(),
            approved_owners: Vec::new(),
        })
        .await?
        .into_inner();
    let namespace = format!("t_{:x}", Sha256::digest(b"tenant_a"));
    let remote = RemoteStore::connect_namespace(
        s3_client.clone(),
        "ontology-artifacts",
        "tenant_a",
        namespace.parse()?,
        "people".parse()?,
        RemoteOptions::default(),
    )
    .await?;
    let first: Generation = published.generation.parse()?;
    let snapshot = remote.open_generation(first).await?;
    let provenance = snapshot
        .provenance()
        .context("missing active source provenance")?;
    ensure!(provenance.source_revision.as_ref() == source_revision);
    let symbol = provenance
        .candidates
        .iter()
        .find(|symbol| symbol.key().name().as_str() == "Person")
        .context("missing Person symbol")?;
    let schema_instance = Instance {
        tenant: "tenant_a".into(),
        owner: symbol.clone(),
        id: "__schema".into(),
    }
    .object_id()
    .context("schema identity")?;
    ensure!(check(&spice_http, &schema_instance, "describe").await?);
    ensure!(
        !check(&spice_http, &schema_instance, "mailbox_owner").await?,
        "publishing must not grant mailbox ownership"
    );
    let data_instance = Instance {
        tenant: "tenant_a".into(),
        owner: symbol.clone(),
        id: "real-data".into(),
    }
    .object_id()
    .context("data identity")?;
    ensure!(
        !check(&spice_http, &data_instance, "read").await?,
        "publishing must not grant data access"
    );
    spice_post(&spice_http,"/v1/relationships/write",json!({"updates":[{"operation":"OPERATION_DELETE","relationship":owner.clone()}]})).await?;
    ensure!(
        client
            .publish_gol_ontology(proto::PublishGolOntologyRequest {
                tenant_id: "tenant_a".into(),
                ontology_id: "people".into(),
                source_revision: source_revision.clone(),
                expected_generation: Some(first.to_string()),
                author_id: "test".into(),
                plan_pin: plan_pin.clone(),
                approved_owners: Vec::new(),
            })
            .await
            .is_err(),
        "revoked publisher activated a generation"
    );
    ensure!(
        remote.active().await? == Some(first),
        "failed provisioning changed the active manifest"
    );
    spice_post(&spice_http,"/v1/relationships/write",json!({"updates":[{"operation":"OPERATION_TOUCH","relationship":owner}]})).await?;
    let head = client
        .get_gol_workspace(proto::GetGolWorkspaceRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            source_revision: None,
        })
        .await?
        .into_inner()
        .summary
        .context("missing current workspace")?;
    let draft = client
        .save_gol_ontology(proto::SaveGolOntologyRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            display_name: "People".into(),
            files: vec![proto::GolFile {
                path: "people.gol".into(),
                text: format!("{source}\n"),
            }],
            author: "test".into(),
            expected_revision: Some(head.source_revision),
            origin: None,
        })
        .await?
        .into_inner()
        .summary
        .context("missing saved draft")?;
    ensure!(
        draft.active_generation.as_deref() ==
            Some(published.generation.as_str()) &&
            draft.active_source_revision.as_deref() ==
                Some(source_revision.as_str()) &&
            draft.source_revision != source_revision,
        "saving a draft discarded the active publication baseline"
    );
    let second = client
        .publish_gol_ontology(proto::PublishGolOntologyRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            source_revision: source_revision.clone(),
            expected_generation: Some(first.to_string()),
            author_id: "test".into(),
            plan_pin: plan_pin.clone(),
            approved_owners: Vec::new(),
        })
        .await?
        .into_inner();
    ensure!(second.generation != first.to_string());
    ensure!(
        snapshot.generation() == first &&
            snapshot
                .provenance()
                .is_some_and(
                    |value| value.source_revision.as_ref() == source_revision
                ),
        "existing generation lease changed during activation"
    );
    ensure!(
        remote.open_generation(first).await.is_err(),
        "retired generation was reopened"
    );
    spice_post(&spice_http,"/v1/relationships/write",json!({"updates":[
        {"operation":"OPERATION_TOUCH","relationship":relationship("ontology","tenant_a/workspace=2Econsumer","parent","tenant","tenant_a")},
        {"operation":"OPERATION_TOUCH","relationship":relationship("ontology","tenant_a/workspace=2Econsumer","owner","user","test")}
    ]})).await?;
    let consumer=client.save_gol_ontology(proto::SaveGolOntologyRequest {tenant_id:"tenant_a".into(),ontology_id:"workspace.consumer".into(),display_name:"Consumer".into(),files:vec![proto::GolFile {path:"consumer.gol".into(),text:"ontology workspace.consumer version \"1.0.0\" { import people version \"1.0.0\"; }".into()}],author:"test".into(),expected_revision:None,origin:None}).await?.into_inner().summary.context("consumer summary")?;
    let consumer_plan = client
        .preview_gol_dependencies(proto::PreviewGolDependenciesRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "workspace.consumer".into(),
            source_revision: consumer.source_revision.clone(),
            approved_owners: vec!["people".into()],
        })
        .await?
        .into_inner()
        .plan_pin
        .context("consumer plan")?;
    client
        .publish_gol_ontology(proto::PublishGolOntologyRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "workspace.consumer".into(),
            source_revision: consumer.source_revision.clone(),
            expected_generation: None,
            author_id: "test".into(),
            plan_pin: consumer_plan,
            approved_owners: vec!["people".into()],
        })
        .await?;
    let cyclic_source=source.replacen("ontology people version \"1.0.0\" {","ontology people version \"1.0.0\" { import workspace.consumer version \"1.0.0\";",1);
    let cyclic = client
        .save_gol_ontology(proto::SaveGolOntologyRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            display_name: "People".into(),
            files: vec![proto::GolFile {
                path: "people.gol".into(),
                text: cyclic_source,
            }],
            author: "test".into(),
            expected_revision: Some(consumer.source_revision),
            origin: None,
        })
        .await?
        .into_inner()
        .summary
        .context("cyclic source summary")?;
    let cyclic_plan = client
        .preview_gol_dependencies(proto::PreviewGolDependenciesRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            source_revision: cyclic.source_revision.clone(),
            approved_owners: vec!["workspace.consumer".into()],
        })
        .await?
        .into_inner()
        .plan_pin
        .context("cyclic publication plan")?;
    let valid_cycle = client
        .validate_gol_ontology(proto::ValidateGolOntologyRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            source_revision: cyclic.source_revision.clone(),
            plan_pin: cyclic_plan.clone(),
            approved_owners: vec!["workspace.consumer".into()],
        })
        .await?
        .into_inner();
    ensure!(
        valid_cycle.status == "valid",
        "a publication cycle is not a GOL type error: {:?}",
        valid_cycle.diagnostics
    );
    let rejected = client
        .publish_gol_ontology(proto::PublishGolOntologyRequest {
            tenant_id: "tenant_a".into(),
            ontology_id: "people".into(),
            source_revision: cyclic.source_revision,
            expected_generation: Some(second.generation.clone()),
            author_id: "test".into(),
            plan_pin: cyclic_plan,
            approved_owners: vec!["workspace.consumer".into()],
        })
        .await;
    ensure!(
        rejected.is_err_and(|error| error
            .message()
            .starts_with("gol::publication_cycle")),
        "a dependency reintroduced an older root generation"
    );
    ensure!(
        remote.active().await? == Some(second.generation.parse()?),
        "publication cycle changed active generation"
    );

    let definition_json = serde_json::json!({
        "name": "daily",
        "sources": [{"id": "source"}],
        "pipeline": [{
            "step": "sink",
            "type": "sink",
            "input_from": ["source"],
            "params": {}
        }]
    })
    .to_string()
    .into_bytes();
    let pipeline = client
        .put_pipeline(proto::PutPipelineRequest {
            authoring_command_json: None,
            tenant_id: "tenant_a".to_owned(),
            pipeline_id: "daily".to_owned(),
            name: "Daily".to_owned(),
            owner_id: "owner".to_owned(),
            definition_json: definition_json.clone(),
            author_id: "test".to_owned(),
            message: "create pipeline".to_owned(),
            expected_revision_id: None,
            create_only: true,
        })
        .await?
        .into_inner();
    let read = client
        .get_pipeline(proto::GetPipelineRequest {
            tenant_id: "tenant_a".to_owned(),
            pipeline_id: "daily".to_owned(),
            revision_id: Some(pipeline.head_revision_id),
        })
        .await?
        .into_inner();
    assert_eq!(read.definition_json, definition_json);

    assert_eq!(client.discover_tenants().await?, ["tenant_a"]);
    client
        .put_tenant(proto::PutTenantRequest {
            tenant_id: "tenant_b".to_owned(),
        })
        .await?;
    assert_eq!(client.discover_tenants().await?, ["tenant_a", "tenant_b"]);
    client
        .delete_tenant(proto::DeleteTenantRequest {
            tenant_id: "tenant_b".to_owned(),
            confirmation_tenant_id: "tenant_b".to_owned(),
        })
        .await?;
    assert_eq!(client.discover_tenants().await?, ["tenant_a"]);

    let validation = client
        .validate_tenants(proto::ValidateTenantsRequest {
            tenant_ids: vec!["tenant_a".to_owned()],
        })
        .await?
        .into_inner();
    assert_eq!(validation.tenants.len(), 1);
    assert_eq!(
        validation
            .tenants
            .first()
            .context("missing tenant validation")?
            .tenant_id,
        "tenant_a"
    );
    let listed = s3_client
        .list_objects_v2()
        .bucket("lake")
        .prefix("tenant_a/_registry/tenant.json")
        .send()
        .await?;
    assert_eq!(listed.contents().len(), 1);
    let branch = HttpClient::new()
        .get(format!(
            "{lakefs_endpoint}/api/v1/repositories/{repository}/branches/main"
        ))
        .basic_auth("registry-access", Some("registry-secret"))
        .timeout(Duration::from_secs(5))
        .send()
        .await?;
    ensure!(
        branch.status().is_success(),
        "lakeFS branch was not created"
    );
    for path in ["gol/sources.json", "pipeline/state.json"] {
        let artifact = HttpClient::new()
            .get(format!(
                "{lakefs_endpoint}/api/v1/repositories/{repository}/refs/main/objects"
            ))
            .query(&[("path", path)])
            .basic_auth("registry-access", Some("registry-secret"))
            .timeout(Duration::from_secs(5))
            .send()
            .await?;
        ensure!(
            artifact.status().is_success(),
            "lakeFS logical artifact is missing: {path}"
        );
    }
    let mut managed = client.put_pipeline(proto::PutPipelineRequest {
        tenant_id: "tenant_a".to_owned(), pipeline_id: "managed".to_owned(), name: "Managed".to_owned(), owner_id: "owner".to_owned(),
        definition_json: serde_json::to_vec(&serde_json::json!({"name":"Managed","sources":[],"pipeline":[]}))?,
        author_id: "owner".to_owned(), message: "Incomplete draft".to_owned(), expected_revision_id: None, create_only: true,
        authoring_command_json: Some(serde_json::to_vec(&galadril_registry::authoring::AuthoringCommand::SaveDraft { branch: "main".to_owned() })?),
    }).await?.into_inner();
    ensure!(managed.published_revision_id.is_none());
    let saved = client
        .get_pipeline(proto::GetPipelineRequest {
            tenant_id: "tenant_a".to_owned(),
            pipeline_id: "managed".to_owned(),
            revision_id: Some(managed.head_revision_id.clone()),
        })
        .await?
        .into_inner();
    ensure!(saved.definition_json == managed.definition_json);
    let definition = serde_json::json!({"name":"Managed","sources":[{"id":"mail","topic":"raw","match_pattern":".*","schema_path":"email.avsc"}],"pipeline":[{"step":"sink","type":"sink","input_from":["mail"]}],"editor":{"positions":{"mail":{"x":12,"y":48}}}});
    let commands = [
        galadril_registry::authoring::AuthoringCommand::SaveDraft {
            branch: "main".to_owned(),
        },
        galadril_registry::authoring::AuthoringCommand::CreateBranch {
            branch: "review".to_owned(),
            from_branch: "main".to_owned(),
        },
        galadril_registry::authoring::AuthoringCommand::ProposeMerge {
            source_branch: "review".to_owned(),
        },
        galadril_registry::authoring::AuthoringCommand::Merge {
            proposal_id: "review".to_owned(),
            resolutions: Default::default(),
        },
        galadril_registry::authoring::AuthoringCommand::Approve,
        galadril_registry::authoring::AuthoringCommand::Publish,
        galadril_registry::authoring::AuthoringCommand::Deploy,
    ];
    for command in commands {
        let deploying = matches!(
            command,
            galadril_registry::authoring::AuthoringCommand::Deploy
        );
        managed = client
            .put_pipeline(proto::PutPipelineRequest {
                tenant_id: "tenant_a".to_owned(),
                pipeline_id: "managed".to_owned(),
                name: "Managed".to_owned(),
                owner_id: "owner".to_owned(),
                definition_json: serde_json::to_vec(&definition)?,
                author_id: "owner".to_owned(),
                message: "Reviewed workspace transition".to_owned(),
                expected_revision_id: Some(managed.head_revision_id),
                create_only: false,
                authoring_command_json: Some(serde_json::to_vec(&command)?),
            })
            .await?
            .into_inner();
        ensure!(deploying == managed.published_revision_id.is_some());
    }
    let state: galadril_registry::authoring::AuthoringState =
        serde_json::from_slice(&managed.authoring_json)?;
    ensure!(
        state.branches.contains_key("review") && state.proposals.is_empty()
    );
    let immutable = client
        .get_runtime_pipeline(proto::GetRuntimePipelineRequest {
            tenant_id: "tenant_a".to_owned(),
            pipeline_id: "managed".to_owned(),
            revision_id: managed
                .published_revision_id
                .context("deployment missing")?,
        })
        .await?
        .into_inner();
    let restored: serde_json::Value =
        serde_json::from_slice(&immutable.definition_json)?;
    ensure!(
        restored.pointer("/editor/positions/mail/x") ==
            Some(&serde_json::json!(12))
    );

    server.abort();
    Ok(())
}

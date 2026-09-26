//! Exercises Registry gRPC against real lakeFS and S3 test containers.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use aws_sdk_s3::Client as S3Client;
use aws_sdk_s3::config::{Credentials, Region};
use galadril_registry::domain::Registry;
use galadril_registry::grpc::{RegistryGrpc, registry_client};
use galadril_registry::proto::registry_server::RegistryServer;
use galadril_registry::proto::{self};
use galadril_registry::storage::{LakeFsConfig, LakeFsStore, S3Config};
use reqwest::Client as HttpClient;
use secrecy::SecretString;
use testcontainers::core::{IntoContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{GenericImage, ImageExt};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::Server;

#[tokio::test]
async fn grpc_ontology_pipeline_and_tenant_validation_use_lakefs() -> Result<()>
{
    let network = format!("galadril-registry-test-{}", std::process::id());
    let minio_name = format!("galadril-minio-{}", std::process::id());
    let minio = GenericImage::new(
        "quay.io/minio/minio@sha256",
        "14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e",
    )
    .with_exposed_port(9000.tcp())
    .with_wait_for(WaitFor::message_on_stderr("API:"))
    .with_network(&network)
    .with_container_name(&minio_name)
    .with_cmd(["server", "/data", "--console-address", ":9001"])
    .with_env_var("MINIO_ROOT_USER", "minioadmin")
    .with_env_var("MINIO_ROOT_PASSWORD", "minioadmin")
    .start()
    .await
    .context("MinIO test container failed")?;
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

    let lakefs = GenericImage::new("treeverse/lakefs", "1.86.0")
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
    let store = LakeFsStore::new(
        LakeFsConfig {
            endpoint: lakefs_endpoint.clone(),
            access_key: "registry-access".to_owned(),
            secret_key: SecretString::from("registry-secret"),
            repository_prefix: "galadril".to_owned(),
            storage_namespace: "s3://lake/".to_owned(),
            branch: "main".to_owned(),
        },
        S3Config {
            endpoint: s3_endpoint,
            region: "us-east-1".to_owned(),
            access_key: "minioadmin".to_owned(),
            secret_key: SecretString::from("minioadmin"),
        },
    )?;
    let repository = store.repository_for_tenant("tenant_a")?;
    let service = RegistryGrpc::new(Arc::new(Registry::new(store)));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let server = tokio::spawn(async move {
        Server::builder()
            .add_service(RegistryServer::new(service))
            .serve_with_incoming(TcpListenerStream::new(listener))
            .await
    });
    let mut client = registry_client(&format!("http://{address}"))?;
    let ontology_json = serde_json::json!({
        "version": "1",
        "resources": [{
            "resource_id": "object.person",
            "kind": "object_type",
            "display_name": "Person",
            "description": "A person",
            "references": [],
            "attributes": {}
        }]
    })
    .to_string()
    .into_bytes();
    let ontology = client
        .put_ontology(proto::PutOntologyRequest {
            tenant_id: "tenant_a".to_owned(),
            ontology_id: "people".to_owned(),
            display_name: "People".to_owned(),
            ontology_json: ontology_json.clone(),
            author: "test".to_owned(),
            message: "create ontology".to_owned(),
            expected_revision_id: None,
            create_only: true,
        })
        .await?
        .into_inner();
    let read = client
        .get_ontology(proto::GetOntologyRequest {
            tenant_id: "tenant_a".to_owned(),
            ontology_id: "people".to_owned(),
            revision_id: ontology.revision_id.clone(),
        })
        .await?
        .into_inner();
    assert_eq!(read.ontology_json, ontology_json);
    client
        .publish_ontology(proto::PublishOntologyRequest {
            tenant_id: "tenant_a".to_owned(),
            ontology_id: "people".to_owned(),
            display_name: "People".to_owned(),
            publication_id: "publication-1".to_owned(),
            revision_id: ontology.revision_id,
            metadata_json: br#"{"channel":"stable"}"#.to_vec(),
            create_only: true,
        })
        .await?;

    let definition_json = serde_json::json!({
        "name": "daily",
        "sources": [{"id": "source"}],
        "pipeline": [{
            "step": "sink",
            "type": "sink",
            "input_from": ["source"],
            "params": {"ontology_id": "people"}
        }]
    })
    .to_string()
    .into_bytes();
    let pipeline = client
        .put_pipeline(proto::PutPipelineRequest {
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
    let binding = client
        .put_binding(proto::PutBindingRequest {
            tenant_id: "tenant_a".to_owned(),
            binding: Some(proto::OntologyBinding {
                pipeline_id: "daily".to_owned(),
                block_id: "sink".to_owned(),
                ontology_id: "people".to_owned(),
                resource_ids: vec!["object.person".to_owned()],
                resource_kinds: Vec::new(),
                include_dependencies: true,
                metadata_json: br#"{"purpose":"runtime"}"#.to_vec(),
                updated_at_ms: 42,
                head_revision_id: String::new(),
            }),
            expected_revision_id: pipeline.head_revision_id,
        })
        .await?
        .into_inner();
    let read = client
        .get_pipeline(proto::GetPipelineRequest {
            tenant_id: "tenant_a".to_owned(),
            pipeline_id: "daily".to_owned(),
            revision_id: Some(binding.head_revision_id),
        })
        .await?
        .into_inner();
    assert_eq!(read.definition_json, definition_json);

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
    for path in ["ontology/state.json", "pipeline/state.json"] {
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
    server.abort();
    Ok(())
}
